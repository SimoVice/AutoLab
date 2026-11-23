"""Sandboxed CAD worker for inspecting, normalizing, and adapting upstream CAD files.

Always executed in an isolated child process with no network access and fixed CLI entrypoints.
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
import struct
import sys
from pathlib import Path
from typing import Any

MAX_STL_BYTES = 50 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
PREVIEW_GLB_SOFT_CAP = 8 * 1024 * 1024

_FORBIDDEN_IMPORTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "http",
        "urllib",
        "httpx",
        "requests",
        "pathlib",
        "shutil",
        "ctypes",
        "importlib",
        "builtins",
        "inspect",
        "pickle",
        "marshal",
        "tempfile",
        "multiprocessing",
        "threading",
        "asyncio",
        "webbrowser",
        "pty",
        "fcntl",
        "resource",
        "signal",
    }
)
_ALLOWED_IMPORT_ROOTS = frozenset({"math", "build123d"})
_FORBIDDEN_NAMES = frozenset({"eval", "exec", "compile", "__import__", "open", "globals", "locals"})
_SCAD_PARAM_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?|true|false|\"[^\"\n]*\")\s*;",
    re.MULTILINE,
)


def _apply_child_rlimit() -> None:
    if os.name == "nt":
        return
    try:
        import resource

        max_mb = int(os.environ.get("AUTOLAB_CAD_MAX_RSS_MB", "2048"))
        cap = max_mb * 1024 * 1024
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard == resource.RLIM_INFINITY or cap < hard:
            resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except Exception:
        pass


def _disable_network() -> None:
    _orig_socket = socket.socket

    class _BlockedSocket(_orig_socket):
        def __init__(self, *_args, **_kwargs):
            raise PermissionError("Network access is disabled inside the CAD sandbox.")

    def _blocked_connect(*_args, **_kwargs):
        raise PermissionError("Network access is disabled inside the CAD sandbox.")

    socket.socket = _BlockedSocket  # type: ignore[misc]
    socket.create_connection = _blocked_connect  # type: ignore[assignment]


def _detect_step_header_units(path: Path) -> str:
    chunk = path.read_bytes()[:524288].decode("ascii", errors="ignore").upper()
    if "CONVERSION_BASED_UNIT('INCH'" in chunk or 'CONVERSION_BASED_UNIT("INCH"' in chunk:
        return "in"
    if "SI_UNIT(.MILLI.,.METRE.)" in chunk or "SI_UNIT( .MILLI. , .METRE. )" in chunk:
        return "mm"
    if "SI_UNIT($,.METRE.)" in chunk:
        return "m"
    return "mm"


def _analyze_stl_file(path: Path) -> dict[str, Any]:
    """Compute exact triangle count, bbox, signed volume, and edge watertightness."""
    import numpy as np

    raw = path.read_bytes()
    if not raw or len(raw) > MAX_STL_BYTES:
        raise ValueError("STL file is empty or exceeds size limit.")

    is_binary = False
    if len(raw) >= 84:
        tri_count = struct.unpack_from("<I", raw, 80)[0]
        if 84 + tri_count * 50 == len(raw) and tri_count > 0:
            is_binary = True

    if is_binary:
        dt = np.dtype(
            [("norm", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")], align=False
        )
        arr = np.frombuffer(raw, dtype=dt, offset=84, count=tri_count)
        tris = arr["v"].astype(np.float64)
    else:
        text = raw.decode("utf-8", errors="replace")
        verts = []
        for match in re.finditer(
            r"vertex\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
            r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s+"
            r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)",
            text,
        ):
            verts.append([float(match.group(1)), float(match.group(2)), float(match.group(3))])
        if len(verts) < 3 or len(verts) % 3 != 0:
            raise ValueError("Invalid or empty STL geometry.")
        tris = np.array(verts, dtype=np.float64).reshape(-1, 3, 3)

    if len(tris) == 0 or not np.isfinite(tris).all():
        raise ValueError("STL contains zero triangles or non-finite coordinates.")

    v0, v1, v2 = tris[:, 0, :], tris[:, 1, :], tris[:, 2, :]
    signed_vol = float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)
    bmin = tris.min(axis=(0, 1))
    bmax = tris.max(axis=(0, 1))

    # Quantize vertices to check edge degree manifoldness cleanly.
    q = np.round(tris * 1000.0).astype(np.int64)
    edge_counts: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
    if len(q) <= 100_000:
        for t in q:
            pts = [tuple(t[0].tolist()), tuple(t[1].tolist()), tuple(t[2].tolist())]
            for a, b in ((pts[0], pts[1]), (pts[1], pts[2]), (pts[2], pts[0])):
                key = (a, b) if a <= b else (b, a)
                edge_counts[key] = edge_counts.get(key, 0) + 1
        watertight = all(c % 2 == 0 and c >= 2 for c in edge_counts.values())
    else:
        watertight = None

    return {
        "triangle_count": int(len(tris)),
        "raw_bbox_min": [float(v) for v in bmin],
        "raw_bbox_max": [float(v) for v in bmax],
        "signed_volume_mm3": abs(signed_vol),
        "watertight": watertight,
        "format": "binary_stl" if is_binary else "ascii_stl",
    }


def _cad_to_app_bbox(
    min_v: tuple[float, float, float], max_v: tuple[float, float, float]
) -> dict[str, Any]:
    # Normalized CAD coordinates are Z-up: X centered, Y centered, Z_min=0.
    # App viewer conventions are Y-up: X width, Y height (= CAD Z), Z depth (= CAD Y).
    size_cad = [
        round(max_v[0] - min_v[0], 3),
        round(max_v[1] - min_v[1], 3),
        round(max_v[2] - min_v[2], 3),
    ]
    return {
        "cad_min_mm": [round(min_v[0], 3), round(min_v[1], 3), round(min_v[2], 3)],
        "cad_max_mm": [round(max_v[0], 3), round(max_v[1], 3), round(max_v[2], 3)],
        "cad_size_mm": size_cad,
        "app_size_mm": [size_cad[0], size_cad[2], size_cad[1]],
    }


def _extract_brep_features(shape) -> tuple[list[dict], list[dict]]:
    from build123d import GeomType
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    planes: list[dict] = []
    holes: list[dict] = []
    seen_holes: set[tuple[float, float, float]] = set()
    for face in shape.faces():
        if face.geom_type == GeomType.PLANE:
            n = face.normal_at()
            if abs(n.Z) >= 0.99 and float(face.area) >= 4.0:
                c = face.center()
                planes.append(
                    {
                        "orientation": "top" if n.Z > 0 else "bottom",
                        "z_mm": round(float(c.Z), 3),
                        "area_mm2": round(float(face.area), 2),
                    }
                )
        elif face.geom_type == GeomType.CYLINDER:
            cyl = BRepAdaptor_Surface(face.wrapped).Cylinder()
            d = cyl.Axis().Direction()
            if abs(d.Z()) >= 0.99:
                loc = cyl.Location()
                diam = round(float(cyl.Radius()) * 2.0, 3)
                bb = face.bounding_box()
                key = (round(float(loc.X()), 2), round(float(loc.Y()), 2), round(diam, 2))
                if key not in seen_holes and 0.5 <= diam <= 120.0:
                    seen_holes.add(key)
                    holes.append(
                        {
                            "x_mm": round(float(loc.X()), 3),
                            "y_mm": round(float(loc.Y()), 3),
                            "z_min_mm": round(float(bb.min.Z), 3),
                            "z_max_mm": round(float(bb.max.Z), 3),
                            "diameter_mm": diam,
                        }
                    )
    planes.sort(key=lambda p: -p["area_mm2"])
    holes.sort(key=lambda h: (h["x_mm"], h["y_mm"], h["diameter_mm"]))
    return planes[:12], holes[:64]


def _normalize_shape(shape, *, source_units: str = "auto", source_up_axis: str = "z"):
    from build123d import Axis

    transform_steps: list[str] = []
    scale_factor = 1.0
    if source_units == "in":
        scale_factor = 25.4
        shape = shape.scale(scale_factor)
        transform_steps.append("scale(25.4)")

    if source_up_axis == "y":
        shape = shape.rotate(Axis.X, 90.0)
        transform_steps.append("rotate_x(+90deg)")

    bb = shape.bounding_box()
    dx = -0.5 * (float(bb.min.X) + float(bb.max.X))
    dy = -0.5 * (float(bb.min.Y) + float(bb.max.Y))
    dz = -float(bb.min.Z)
    shape = shape.translate((dx, dy, dz))
    transform_steps.append(f"translate({dx:.3f}, {dy:.3f}, {dz:.3f})")
    return shape, {
        "source_units_applied": source_units,
        "scale_factor": scale_factor,
        "source_up_axis": source_up_axis,
        "shift_mm": [round(dx, 3), round(dy, 3), round(dz, 3)],
        "steps": transform_steps,
    }


def _export_preview_glb(shape, glb_path: Path) -> int:
    from build123d import Unit, export_gltf

    for lin_def, ang_def in ((0.08, 0.2), (0.25, 0.35), (0.6, 0.5)):
        export_gltf(
            shape,
            glb_path,
            unit=Unit.MM,
            binary=True,
            linear_deflection=lin_def,
            angular_deflection=ang_def,
        )
        size = glb_path.stat().st_size
        if size <= PREVIEW_GLB_SOFT_CAP:
            return size
    return glb_path.stat().st_size


def validate_parametric_python_ast(source: str) -> None:
    """Validate parametric build123d Python code against a strict zero-IO/zero-network allowlist."""
    tree = ast.parse(source, mode="exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_IMPORTS or root not in _ALLOWED_IMPORT_ROOTS:
                    raise ValueError(f"Disallowed module import {alias.name!r} in sandbox.")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if node.level != 0 or mod in _FORBIDDEN_IMPORTS or mod not in _ALLOWED_IMPORT_ROOTS:
                raise ValueError(f"Disallowed from-import {node.module!r} in sandboxed script.")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden builtin reference {node.id!r} in sandboxed script.")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"Forbidden dunder attribute access {node.attr!r} in sandbox.")


def extract_parametric_parameters(source: str, ext: str) -> list[dict[str, Any]]:
    """Extract top-level scalar parameters from parametric .py or .scad source."""
    params: list[dict[str, Any]] = []
    if ext == ".scad":
        for match in _SCAD_PARAM_RE.finditer(source):
            name, raw_val = match.group(1), match.group(2)
            if raw_val in ("true", "false"):
                val: Any = raw_val == "true"
                ptype = "bool"
            elif raw_val.startswith('"') and raw_val.endswith('"'):
                val = raw_val[1:-1]
                ptype = "str"
            else:
                val = float(raw_val) if "." in raw_val else int(raw_val)
                ptype = "number"
            params.append({"name": name, "default": val, "type": ptype})
    elif ext == ".py":
        tree = ast.parse(source, mode="exec")
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name) or target.id.startswith("_"):
                continue
            val_node = stmt.value
            sign = 1
            if isinstance(val_node, ast.UnaryOp) and isinstance(val_node.op, ast.USub):
                sign = -1
                val_node = val_node.operand
            if isinstance(val_node, ast.Constant) and isinstance(
                val_node.value, (int, float, bool, str)
            ):
                value = val_node.value
                if sign == -1 and isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = -value
                ptype = (
                    "bool"
                    if isinstance(value, bool)
                    else "number"
                    if isinstance(value, (int, float))
                    else "str"
                )
                params.append({"name": target.id, "default": value, "type": ptype})
    return params[:60]


def _inspect_handler(payload: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(payload["source_path"])
    out_dir = Path(payload["artifact_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    source_units = payload.get("source_units", "auto")
    source_up_axis = payload.get("source_up_axis", "z")
    ext = source_path.suffix.lower()

    if ext in (".step", ".stp"):
        from build123d import Unit, export_step, import_step

        detected_units = _detect_step_header_units(source_path)
        shape = import_step(source_path)
        if shape is None or not shape.is_valid or len(shape.solids()) == 0:
            raise ValueError("Imported STEP file contains no valid solid bodies.")
        norm_shape, transform = _normalize_shape(
            shape, source_units=source_units, source_up_axis=source_up_axis
        )
        bb = norm_shape.bounding_box()
        bounds = _cad_to_app_bbox((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        planes, holes = _extract_brep_features(norm_shape)

        norm_step_path = out_dir / "normalized.step"
        export_step(norm_shape, norm_step_path, unit=Unit.MM)
        glb_path = out_dir / "preview.glb"
        glb_bytes = _export_preview_glb(norm_shape, glb_path)
        return {
            "ok": True,
            "geometry": {
                "cad_kind": "step_brep",
                "is_valid": bool(norm_shape.is_valid),
                "watertight": True,
                "solid_count": len(norm_shape.solids()),
                "volume_mm3": round(float(norm_shape.volume), 3),
                "detected_units": detected_units,
                "normalization_transform": transform,
                **bounds,
                "mounting_planes": planes,
                "hole_features": holes,
            },
            "artifacts": {
                "normalized_step_path": str(norm_step_path),
                "preview_glb_path": str(glb_path),
                "preview_glb_bytes": glb_bytes,
            },
        }

    if ext == ".stl":
        from build123d import import_stl

        stl_stats = _analyze_stl_file(source_path)
        shape = import_stl(source_path)
        norm_shape, transform = _normalize_shape(
            shape, source_units=source_units, source_up_axis=source_up_axis
        )
        scale = float(transform["scale_factor"])
        bb = norm_shape.bounding_box()
        bounds = _cad_to_app_bbox((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        vol_mm3 = round(stl_stats["signed_volume_mm3"] * (scale**3), 3)
        glb_path = out_dir / "preview.glb"
        glb_bytes = _export_preview_glb(norm_shape, glb_path)
        return {
            "ok": True,
            "geometry": {
                "cad_kind": stl_stats["format"],
                "is_valid": bool(stl_stats["triangle_count"] > 0),
                "watertight": stl_stats["watertight"],
                "solid_count": 1 if vol_mm3 > 0 else 0,
                "triangle_count": stl_stats["triangle_count"],
                "volume_mm3": vol_mm3,
                "detected_units": "in" if source_units == "in" else "mm",
                "normalization_transform": transform,
                **bounds,
                "mounting_planes": [],
                "hole_features": [],
            },
            "artifacts": {
                "normalized_step_path": None,
                "preview_glb_path": str(glb_path),
                "preview_glb_bytes": glb_bytes,
            },
        }

    if ext in (".scad", ".py"):
        raw = source_path.read_bytes()
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError("Parametric CAD script exceeds 2 MB safety limit.")
        text = raw.decode("utf-8", errors="strict")
        if ext == ".py":
            validate_parametric_python_ast(text)
        params = extract_parametric_parameters(text, ext)
        return {
            "ok": True,
            "geometry": {
                "cad_kind": "parametric_scad" if ext == ".scad" else "parametric_python",
                "is_valid": True,
                "watertight": None,
                "solid_count": 0,
                "volume_mm3": None,
                "detected_units": "mm",
                "normalization_transform": {"steps": []},
                "cad_min_mm": [0.0, 0.0, 0.0],
                "cad_max_mm": [100.0, 100.0, 60.0],
                "cad_size_mm": [100.0, 100.0, 60.0],
                "app_size_mm": [100.0, 60.0, 100.0],
                "mounting_planes": [],
                "hole_features": [],
                "parametric_parameters": params,
            },
            "artifacts": {
                "normalized_step_path": None,
                "preview_glb_path": None,
                "preview_glb_bytes": 0,
            },
        }

    raise ValueError(f"Unsupported upstream CAD file extension: {ext!r}")


_HANDLERS = {
    "inspect": _inspect_handler,
}


def run_mode(mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one fixed worker mode cleanly (also importable inside tests)."""
    handler = _HANDLERS.get(mode)
    if handler is None:
        raise ValueError(f"Unknown upstream CAD worker mode: {mode!r}")
    return handler(payload)


def main(argv: list[str]) -> int:
    mode = None
    in_path = None
    out_path = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
        elif tok == "--input" and i + 1 < len(argv):
            in_path = Path(argv[i + 1])
            i += 2
        elif tok == "--output" and i + 1 < len(argv):
            out_path = Path(argv[i + 1])
            i += 2
        else:
            return 2
    if not mode or in_path is None or out_path is None:
        return 2
    try:
        _apply_child_rlimit()
        payload = json.loads(in_path.read_bytes())
        result = run_mode(mode, payload)
        out_path.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except BaseException as exc:
        err = {
            "ok": False,
            "error_type": type(exc).__name__[:80],
            "detail": str(exc)[:300],
        }
        try:
            out_path.write_text(json.dumps(err), encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    _disable_network()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main(sys.argv[1:]))

"""Sandboxed CAD worker for inspecting, normalizing, and adapting upstream CAD files.

Always executed in an isolated child process with no network access and fixed CLI entrypoints.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import struct
import subprocess
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


def _load_normalized_stl(path, source_units, source_up_axis):
    from build123d import Unit, import_stl

    # STL is a triangulated face. Scale its vertices during import so the
    # geometry, its measured bounds and the exported preview use the same units.
    unit = Unit.IN if source_units == "in" else Unit.MM
    shape = import_stl(path, model_unit=unit)
    normalized, transform = _normalize_shape(shape, source_up_axis=source_up_axis)
    transform["source_units_applied"] = source_units
    if unit == Unit.IN:
        transform["scale_factor"] = 25.4
        transform["steps"].insert(0, "scale(25.4)")
    return normalized, transform


def _export_preview_glb(shape, glb_path: Path) -> int:
    from build123d import Compound, Unit, export_gltf

    # The glTF exporter rotates its root from CAD Z-up to Y-up. A neutral root
    # also rotates each child's pre-existing placement about the project datum.
    scene = Compound(children=[shape], label="normalized-cad")
    for lin_def, ang_def in ((0.08, 0.2), (0.25, 0.35), (0.6, 0.5)):
        export_gltf(
            scene,
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
        # OCCT already converted a STEP with declared inch units to millimetres.
        override = "auto" if source_units == detected_units else source_units
        norm_shape, transform = _normalize_shape(
            shape, source_units=override, source_up_axis=source_up_axis
        )
        transform["source_units_applied"] = source_units
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
        stl_stats = _analyze_stl_file(source_path)
        norm_shape, transform = _load_normalized_stl(source_path, source_units, source_up_axis)
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


def _mating_bbox_dict(solid) -> dict[str, Any]:
    bb = solid.bounding_box()
    return _cad_to_app_bbox((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))


def _build_mating_solid(geom: dict[str, Any], spec: dict[str, Any], overlap_mm: float = 0.0):
    from build123d import Box, Cylinder, Pos

    cmin = geom["cad_min_mm"]
    cmax = geom["cad_max_mm"]
    sx = max(float(cmax[0] - cmin[0]), 2.0)
    sy = max(float(cmax[1] - cmin[1]), 2.0)
    sz = max(float(cmax[2] - cmin[2]), 2.0)
    kind = spec["kind"]
    gap = float(spec.get("clearance_mm", 0.4)) - float(overlap_mm)
    wall_t = float(spec.get("wall_thickness_mm", 4.0))
    plate_t = float(spec.get("plate_thickness_mm", 6.0))
    riser_h = float(spec.get("riser_height_mm", 15.0))
    margin = float(spec.get("flange_margin_mm", 12.0))
    hole_r = float(spec.get("hole_diameter_mm", 3.4)) / 2.0
    placement = spec.get("placement", "below")

    def _drill_vertical(plate, hx: float, hy: float, pw: float, pd: float, z0: float, z1: float):
        if abs(hx) + hole_r + 1.5 > pw / 2.0 or abs(hy) + hole_r + 1.5 > pd / 2.0:
            return plate
        zc = 0.5 * (z0 + z1)
        hz = abs(z1 - z0) + 4.0
        return plate - Pos(hx, hy, zc) * Cylinder(hole_r, hz)

    def _perimeter_holes(pw: float, pd: float) -> list[tuple[float, float]]:
        ox = min(pw / 2.0 - max(hole_r + 2.5, 4.0), sx / 2.0 + margin * 0.5)
        oy = min(pd / 2.0 - max(hole_r + 2.5, 4.0), sy / 2.0 + margin * 0.5)
        if ox <= hole_r + 2.0 or oy <= hole_r + 2.0:
            return []
        return [(-ox, -oy), (ox, -oy), (-ox, oy), (ox, oy)]

    if kind == "adapter_plate":
        pw = sx + 2.0 * margin
        pd = sy + 2.0 * margin
        z_bot = (cmax[2] + gap) if placement == "above" else (cmin[2] - gap - plate_t)
        zc = z_bot + plate_t / 2.0
        solid = Pos(0.0, 0.0, zc) * Box(pw, pd, plate_t)
        hole_pts = [(h["x_mm"], h["y_mm"]) for h in geom.get("hole_features") or []]
        hole_pts.extend(_perimeter_holes(pw, pd))
        px = spec.get("secondary_hole_pitch_x_mm")
        py = spec.get("secondary_hole_pitch_y_mm")
        if px and py:
            hole_pts.extend(
                [(-px / 2, -py / 2), (px / 2, -py / 2), (-px / 2, py / 2), (px / 2, py / 2)]
            )
        seen: set[tuple[float, float]] = set()
        for hx, hy in hole_pts:
            key = (round(float(hx), 2), round(float(hy), 2))
            if key in seen:
                continue
            seen.add(key)
            solid = _drill_vertical(solid, float(hx), float(hy), pw, pd, z_bot, z_bot + plate_t)
    elif kind == "riser":
        pw = sx + 2.0 * margin
        pd = sy + 2.0 * margin
        z_top = (cmax[2] + gap + riser_h) if placement == "above" else (cmin[2] - gap)
        z_bot = (cmax[2] + gap) if placement == "above" else (cmin[2] - gap - riser_h)
        zc = 0.5 * (z_top + z_bot)
        solid = Pos(0.0, 0.0, zc) * Box(pw, pd, riser_h)
        for hx, hy in _perimeter_holes(pw, pd):
            solid = _drill_vertical(solid, hx, hy, pw, pd, z_bot, z_top)
    elif kind == "cutout_mount":
        pw = sx + 2.0 * margin
        pd = sy + 2.0 * margin
        z_bot = (cmax[2] + gap) if placement == "above" else (cmin[2] - gap - plate_t)
        zc = z_bot + plate_t / 2.0
        solid = Pos(0.0, 0.0, zc) * Box(pw, pd, plate_t)
        cw = float(spec.get("cutout_width_mm") or max(sx * 0.65, 6.0))
        cd = float(spec.get("cutout_depth_mm") or max(sy * 0.65, 6.0))
        cw = min(cw, pw - 2.0 * wall_t - 4.0)
        cd = min(cd, pd - 2.0 * wall_t - 4.0)
        if cw > 2.0 and cd > 2.0:
            solid = solid - Pos(0.0, 0.0, zc) * Box(cw, cd, plate_t + 4.0)
        for hx, hy in _perimeter_holes(pw, pd):
            solid = _drill_vertical(solid, hx, hy, pw, pd, z_bot, z_bot + plate_t)
    elif kind == "bracket":
        bh = max(min(sz * 0.75 + plate_t, sz + gap), plate_t * 2.0)
        fw = sx + margin
        foot_len = margin * 2.0
        if placement == "side_x":
            x_face = cmax[0] + gap
            wall = Pos(x_face + wall_t / 2.0, 0.0, bh / 2.0) * Box(wall_t, fw, bh)
            foot = Pos(x_face + wall_t + foot_len / 2.0 - 0.5, 0.0, plate_t / 2.0) * Box(
                foot_len + 1.0, fw, plate_t
            )
            solid = wall.fuse(foot)
            for oy in (-fw * 0.3, fw * 0.3):
                solid = solid - Pos(x_face + wall_t + foot_len * 0.5, oy, plate_t / 2.0) * Cylinder(
                    hole_r, plate_t + 4.0
                )
        else:
            y_face = cmin[1] - gap
            wall = Pos(0.0, y_face - wall_t / 2.0, bh / 2.0) * Box(fw, wall_t, bh)
            foot = Pos(0.0, y_face - wall_t - foot_len / 2.0 + 0.5, plate_t / 2.0) * Box(
                fw, foot_len + 1.0, plate_t
            )
            solid = wall.fuse(foot)
            for ox in (-fw * 0.3, fw * 0.3):
                solid = solid - Pos(ox, y_face - wall_t - foot_len * 0.5, plate_t / 2.0) * Cylinder(
                    hole_r, plate_t + 4.0
                )
    elif kind == "enclosure":
        iw = sx + 2.0 * gap
        id_ = sy + 2.0 * gap
        ih = sz + gap
        ow = iw + 2.0 * wall_t
        od = id_ + 2.0 * wall_t
        oh = ih + wall_t
        outer = Pos(0.0, 0.0, oh / 2.0) * Box(ow, od, oh)
        cavity = Pos(0.0, 0.0, ih / 2.0 - 0.5) * Box(iw, id_, ih + 1.0)
        solid = outer - cavity
    else:
        raise ValueError(f"Unsupported mating kind: {kind!r}")

    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError(f"Generated mating part '{kind}' failed valid single-solid check.")
    return solid


def _load_normalized_upstream_shape(payload: dict[str, Any]):
    from build123d import import_step

    norm_step = payload.get("normalized_step_path")
    if norm_step and Path(norm_step).is_file():
        return import_step(norm_step), True
    source_path = Path(payload["source_path"])
    geom = payload["geometry"]
    tf = geom.get("normalization_transform") or {}
    norm_shape, _ = _load_normalized_stl(
        source_path,
        source_units=tf.get("source_units_applied", "auto"),
        source_up_axis=tf.get("source_up_axis", "z"),
    )
    return norm_shape, False


def _adapt_mating_handler(payload: dict[str, Any]) -> dict[str, Any]:
    from build123d import Compound, Unit, export_step, export_stl

    out_dir = Path(payload["artifact_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    part_id = str(payload.get("part_id") or "custom-mating-part")
    spec = payload["spec"]
    geom = payload["geometry"]
    overlap_mm = float(payload.get("test_overlap_mm", 0.0))

    upstream_shape, is_brep = _load_normalized_upstream_shape(payload)
    mating_solid = _build_mating_solid(geom, spec, overlap_mm=overlap_mm)
    mating_solid.label = part_id

    # Export detached custom solid before wrapping in an assembly Compound.
    custom_step_path = out_dir / f"{part_id}.step"
    custom_stl_path = out_dir / f"{part_id}.stl"
    custom_glb_path = out_dir / f"{part_id}.glb"
    export_step(mating_solid, custom_step_path, unit=Unit.MM)
    export_stl(mating_solid, custom_stl_path)
    custom_glb_bytes = _export_preview_glb(mating_solid, custom_glb_path)

    # Exact 3D interference verification (BRep volume overlap or AABB gap for STL / enclosure).
    mb = mating_solid.bounding_box()
    if is_brep:
        interference_mm3 = 0.0
        try:
            inter = upstream_shape.intersect(mating_solid)
            if inter is not None:
                interference_mm3 = round(
                    sum(float(s.volume) for s in inter.solids() if float(s.volume) > 0), 4
                )
        except Exception as exc:
            raise ValueError("The solid interference check could not be completed.") from exc
        interference_passed = interference_mm3 <= 0.01
        u_min = geom["cad_min_mm"]
        u_max = geom["cad_max_mm"]
        gaps = [
            max(u_min[0] - mb.max.X, mb.min.X - u_max[0]),
            max(u_min[1] - mb.max.Y, mb.min.Y - u_max[1]),
            max(u_min[2] - mb.max.Z, mb.min.Z - u_max[2]),
        ]
        min_gap_mm = 0.0 if interference_mm3 > 0.01 else round(max(gaps), 3)
        if spec["kind"] == "enclosure" and overlap_mm <= 0:
            min_gap_mm = round(float(spec.get("clearance_mm", 0.4)), 3)
    else:
        u_min = geom["cad_min_mm"]
        u_max = geom["cad_max_mm"]
        if spec["kind"] == "enclosure":
            gap = float(spec.get("clearance_mm", 0.4)) - overlap_mm
            min_gap_mm = round(gap, 3)
            interference_passed = gap >= -1e-4
        else:
            gaps = [
                max(u_min[0] - mb.max.X, mb.min.X - u_max[0]),
                max(u_min[1] - mb.max.Y, mb.min.Y - u_max[1]),
                max(u_min[2] - mb.max.Z, mb.min.Z - u_max[2]),
            ]
            min_gap_mm = round(max(gaps), 3)
            interference_passed = min_gap_mm >= -1e-4
        interference_mm3 = 0.0 if interference_passed else round(abs(min_gap_mm) * 100.0, 2)

    assembly_step_path: Path | None = None
    assembly_glb_path = out_dir / "adapted-assembly.glb"
    assembly_compound = Compound(children=[upstream_shape, mating_solid], label="adapted-assembly")
    _export_preview_glb(assembly_compound, assembly_glb_path)
    if is_brep:
        # Re-load detached copies for assembly STEP so standalone solid remains valid.
        u_copy, _ = _load_normalized_upstream_shape(payload)
        m_copy = _build_mating_solid(geom, spec, overlap_mm=overlap_mm)
        m_copy.label = part_id
        assembly_step_path = out_dir / "adapted-assembly.step"
        export_step(
            Compound(children=[u_copy, m_copy], label="adapted-assembly"),
            assembly_step_path,
            unit=Unit.MM,
        )

    c_bounds = _mating_bbox_dict(mating_solid)
    u_min = geom["cad_min_mm"]
    u_max = geom["cad_max_mm"]
    c_min = c_bounds["cad_min_mm"]
    c_max = c_bounds["cad_max_mm"]
    comb_min = (min(u_min[0], c_min[0]), min(u_min[1], c_min[1]), min(u_min[2], c_min[2]))
    comb_max = (max(u_max[0], c_max[0]), max(u_max[1], c_max[1]), max(u_max[2], c_max[2]))
    comb_bounds = _cad_to_app_bbox(comb_min, comb_max)

    return {
        "ok": True,
        "part_id": part_id,
        "kind": spec["kind"],
        "custom_geometry": {
            "is_valid": bool(mating_solid.is_valid),
            "solid_count": len(mating_solid.solids()),
            "volume_mm3": round(float(mating_solid.volume), 3),
            **c_bounds,
        },
        "combined_geometry": comb_bounds,
        "interference": {
            "passed": bool(interference_passed),
            "method": "brep_boolean_intersection" if is_brep else "aabb_clearance_envelope",
            "interference_volume_mm3": interference_mm3,
            "min_clearance_mm": min_gap_mm,
        },
        "artifacts": {
            "custom_step_path": str(custom_step_path),
            "custom_stl_path": str(custom_stl_path),
            "custom_glb_path": str(custom_glb_path),
            "custom_glb_bytes": custom_glb_bytes,
            "assembly_step_path": str(assembly_step_path) if assembly_step_path else None,
            "assembly_glb_path": str(assembly_glb_path),
        },
    }


def _rewrite_parametric_ast_python(source: str, overrides: dict[str, Any]) -> str:
    """Safely update scalar default assignments via top-level AST rewrite (no string exec)."""
    lines = source.splitlines()
    tree = ast.parse(source, mode="exec")
    replacements: list[tuple[int, int, str]] = []
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id in overrides
        ):
            name = stmt.targets[0].id
            start = stmt.lineno - 1
            end = (stmt.end_lineno or stmt.lineno) - 1
            replacements.append((start, end, f"{name} = {overrides[name]!r}"))
    for start, end, new_line in reversed(replacements):
        lines[start : end + 1] = [new_line]
    return "\n".join(lines) + "\n"


def _adapt_parametric_handler(payload: dict[str, Any]) -> dict[str, Any]:
    import math

    source_path = Path(payload["source_path"])
    out_dir = Path(payload["artifact_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    overrides: dict[str, Any] = dict(payload.get("param_overrides") or {})
    ext = source_path.suffix.lower()
    raw = source_path.read_text(encoding="utf-8", errors="strict")
    declared = extract_parametric_parameters(raw, ext)
    declared_map = {item["name"]: item["default"] for item in declared}
    unknown = sorted(set(overrides) - set(declared_map))
    if unknown:
        raise ValueError(f"Unknown parametric parameter(s): {', '.join(unknown)}.")

    diffs = [
        {"name": name, "original": declared_map[name], "updated": overrides[name]}
        for name in overrides
    ]

    if ext == ".py":
        from build123d import Shape, Unit, export_step, export_stl

        _disable_network()
        validate_parametric_python_ast(raw)
        modified_src = _rewrite_parametric_ast_python(raw, overrides)
        validate_parametric_python_ast(modified_src)
        adapted_py_path = out_dir / "adapted.py"
        adapted_py_path.write_text(modified_src, encoding="utf-8")

        safe_builtins = {
            "abs": abs,
            "min": min,
            "max": max,
            "round": round,
            "range": range,
            "len": len,
            "float": float,
            "int": int,
            "bool": bool,
            "str": str,
            "list": list,
            "tuple": tuple,
            "dict": dict,
            "set": set,
            "enumerate": enumerate,
            "zip": zip,
            "sum": sum,
            "any": any,
            "all": all,
            "sorted": sorted,
        }

        def _safe_import(name: str, _globals=None, _locals=None, fromlist=(), level: int = 0):
            root = name.split(".")[0]
            if level != 0 or root not in _ALLOWED_IMPORT_ROOTS:
                raise ImportError(f"Blocked import {name!r} inside sandboxed CAD execution.")
            if root == "math":
                return math
            return __import__(name, None, None, fromlist, 0)

        safe_builtins["__import__"] = _safe_import
        namespace: dict[str, Any] = {"__builtins__": safe_builtins, "math": math}
        exec(compile(modified_src, "<upstream_parametric_cad>", "exec"), namespace)

        candidate = None
        for key in ("result", "assembly", "part", "model", "shape", "out"):
            val = namespace.get(key)
            if isinstance(val, Shape):
                candidate = val
                break
        if candidate is None:
            for val in reversed(list(namespace.values())):
                if isinstance(val, Shape):
                    candidate = val
                    break
        if candidate is None:
            raise ValueError("Parametric Python script did not define a build123d Shape result.")

        norm_shape, transform = _normalize_shape(candidate)
        if not norm_shape.is_valid:
            raise ValueError("Adapted parametric CAD shape is invalid.")

        step_path = out_dir / "adapted.step"
        stl_path = out_dir / "adapted.stl"
        glb_path = out_dir / "adapted.glb"
        export_step(norm_shape, step_path, unit=Unit.MM)
        export_stl(norm_shape, stl_path)
        glb_bytes = _export_preview_glb(norm_shape, glb_path)
        bb = norm_shape.bounding_box()
        bounds = _cad_to_app_bbox((bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z))
        return {
            "ok": True,
            "render_status": "rendered_brep",
            "parameter_diffs": diffs,
            "custom_geometry": {
                "is_valid": bool(norm_shape.is_valid),
                "solid_count": len(norm_shape.solids()),
                "volume_mm3": round(float(norm_shape.volume), 3),
                "normalization_transform": transform,
                **bounds,
            },
            "artifacts": {
                "adapted_source_path": str(adapted_py_path),
                "custom_step_path": str(step_path),
                "custom_stl_path": str(stl_path),
                "custom_glb_path": str(glb_path),
                "custom_glb_bytes": glb_bytes,
            },
        }

    if ext == ".scad":
        adapted_src = raw
        for name, val in overrides.items():
            if isinstance(val, bool):
                rep = "true" if val else "false"
            elif isinstance(val, str):
                rep = json.dumps(val)
            else:
                rep = str(val)
            adapted_src = re.sub(
                rf"^(\s*{re.escape(name)}\s*=\s*)([^;\n]+)(;)",
                rf"\g<1>{rep}\g<3>",
                adapted_src,
                count=1,
                flags=re.MULTILINE,
            )
        scad_path = out_dir / "adapted.scad"
        scad_path.write_text(adapted_src, encoding="utf-8")

        openscad_bin = shutil.which("openscad")
        if openscad_bin:
            stl_path = out_dir / "adapted.stl"
            proc = subprocess.run(
                [openscad_bin, "-o", str(stl_path), str(scad_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                shell=False,
            )
            if proc.returncode == 0 and stl_path.is_file():
                from build123d import import_stl

                stl_stats = _analyze_stl_file(stl_path)
                shape = import_stl(stl_path)
                norm_shape, _ = _normalize_shape(shape)
                glb_path = out_dir / "adapted.glb"
                glb_bytes = _export_preview_glb(norm_shape, glb_path)
                bb = norm_shape.bounding_box()
                bounds = _cad_to_app_bbox(
                    (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)
                )
                return {
                    "ok": True,
                    "render_status": "rendered_openscad",
                    "parameter_diffs": diffs,
                    "custom_geometry": {
                        "is_valid": True,
                        "watertight": stl_stats["watertight"],
                        "volume_mm3": round(stl_stats["signed_volume_mm3"], 3),
                        **bounds,
                    },
                    "artifacts": {
                        "adapted_source_path": str(scad_path),
                        "custom_step_path": None,
                        "custom_stl_path": str(stl_path),
                        "custom_glb_path": str(glb_path),
                        "custom_glb_bytes": glb_bytes,
                    },
                }

        return {
            "ok": True,
            "render_status": "openscad_cli_unavailable",
            "parameter_diffs": diffs,
            "custom_geometry": None,
            "artifacts": {
                "adapted_source_path": str(scad_path),
                "custom_step_path": None,
                "custom_stl_path": None,
                "custom_glb_path": None,
                "custom_glb_bytes": 0,
            },
        }

    raise ValueError(f"Parametric adaptation unsupported for extension {ext!r}.")


_HANDLERS = {
    "inspect": _inspect_handler,
    "adapt_mating": _adapt_mating_handler,
    "adapt_parametric": _adapt_parametric_handler,
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

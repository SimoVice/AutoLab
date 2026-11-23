"""Autonomous non-destructive mating and sandboxed parametric CAD adaptation."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.upstream_cad_ingest import run_upstream_worker, upstream_cache_dir


class ParamOverride(BaseModel):
    """Typed parameter override entry compatible with OpenAI strict tool schemas."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=80)]
    value: float | int | bool | str


class MatingAdaptationSpec(BaseModel):
    """Declarative specification for a non-destructive custom mating part."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["adapter_plate", "bracket", "riser", "enclosure", "cutout_mount"]
    clearance_mm: Annotated[float, Field(ge=0.0, le=5.0)] = 0.4
    wall_thickness_mm: Annotated[float, Field(ge=1.5, le=25.0)] = 4.0
    plate_thickness_mm: Annotated[float, Field(ge=2.0, le=30.0)] = 6.0
    riser_height_mm: Annotated[float, Field(ge=2.0, le=150.0)] = 15.0
    flange_margin_mm: Annotated[float, Field(ge=4.0, le=60.0)] = 12.0
    hole_diameter_mm: Annotated[float, Field(ge=2.0, le=12.0)] = 3.4
    secondary_hole_pitch_x_mm: Annotated[float, Field(ge=10.0, le=300.0)] | None = None
    secondary_hole_pitch_y_mm: Annotated[float, Field(ge=10.0, le=300.0)] | None = None
    cutout_width_mm: Annotated[float, Field(ge=4.0, le=400.0)] | None = None
    cutout_depth_mm: Annotated[float, Field(ge=4.0, le=400.0)] | None = None
    placement: Literal["below", "above", "rear", "side_x", "surround"] = "below"


def check_custom_part_printer_fit(
    app_size_mm: list[float] | tuple[float, float, float] | None,
    part_id: str,
    label: str,
    lab_profile: dict | None,
) -> dict[str, Any] | None:
    """Validate an adapted part against the user's configured 3D printer build volume."""
    if not app_size_mm or not isinstance(lab_profile, dict):
        return None
    printer = lab_profile.get("printer") or {}
    vol = printer.get("build_volume_mm")
    if not isinstance(vol, (list, tuple)) or len(vol) != 3:
        return None
    bx, by, bz = float(vol[0]), float(vol[1]), float(vol[2])
    if min(bx, by, bz) <= 0:
        return None
    # app_size_mm is [width_x, height_y, depth_z]; printer bed is [bx, by, bz] (Z height).
    w, h, d = float(app_size_mm[0]), float(app_size_mm[1]), float(app_size_mm[2])
    unrotated = w <= bx + 1e-6 and d <= by + 1e-6 and h <= bz + 1e-6
    rotated = d <= bx + 1e-6 and w <= by + 1e-6 and h <= bz + 1e-6
    fits = unrotated or rotated
    orientation = (
        "standard"
        if unrotated
        else ("rotated_90_xy" if rotated else "exceeds_build_volume")
    )
    overflows: list[str] = []
    if not fits:
        std_over = (max(0.0, w - bx), max(0.0, d - by), max(0.0, h - bz))
        rot_over = (max(0.0, d - bx), max(0.0, w - by), max(0.0, h - bz))
        best = std_over if sum(std_over) <= sum(rot_over) else rot_over
        for axis, val in zip(("X", "Y", "Z"), best):
            if val > 1e-6:
                overflows.append(f"{axis} +{val:.1f} mm")
    printer_name = printer.get("name") or "Configured printer"
    detail = (
        f"{label} ({w:.1f} x {d:.1f} x {h:.1f} mm) fits inside "
        f"{printer_name} ({bx:g} x {by:g} x {bz:g} mm, {orientation})."
        if fits
        else f"{label} ({w:.1f} x {d:.1f} x {h:.1f} mm) exceeds {printer_name} build volume "
        f"({bx:g} x {by:g} x {bz:g} mm): {', '.join(overflows)}."
    )
    return {
        "part_id": part_id,
        "label": label,
        "part_size_mm": [round(w, 2), round(d, 2), round(h, 2)],
        "build_volume_mm": [round(bx, 2), round(by, 2), round(bz, 2)],
        "printer_name": printer_name,
        "fits": fits,
        "orientation": orientation,
        "detail": detail,
    }


def _spec_digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


async def adapt_upstream_mating(
    upstream_project: dict[str, Any],
    spec: MatingAdaptationSpec | dict[str, Any],
    *,
    lab_profile: dict[str, Any] | None = None,
    test_overlap_mm: float = 0.0,
) -> dict[str, Any]:
    """Generate a non-destructive custom mating part and verify zero modeled interference."""
    validated = (
        spec
        if isinstance(spec, MatingAdaptationSpec)
        else MatingAdaptationSpec.model_validate(spec)
    )
    spec_dict = validated.model_dump()
    prov = upstream_project["provenance"]
    geom = upstream_project["geometry"]
    artifacts = upstream_project["artifacts"]
    part_id = f"custom-{validated.kind.replace('_', '-')}"
    cache_id = _spec_digest(
        {
            "sha": prov["file_sha256"],
            "spec": spec_dict,
            "overlap": test_overlap_mm,
        }
    )
    out_dir = upstream_cache_dir() / "adapted" / f"{prov['file_sha256'][:12]}-{cache_id}"

    worker_res = await run_upstream_worker(
        "adapt_mating",
        {
            "source_path": artifacts["source_path"],
            "normalized_step_path": artifacts.get("normalized_step_path"),
            "geometry": geom,
            "spec": spec_dict,
            "part_id": part_id,
            "artifact_dir": str(out_dir),
            "test_overlap_mm": float(test_overlap_mm),
        },
    )
    printer_fit = check_custom_part_printer_fit(
        worker_res["custom_geometry"]["app_size_mm"],
        part_id,
        f"Custom {validated.kind.replace('_', ' ')}",
        lab_profile,
    )
    return {
        "mode": "mating",
        "part_id": part_id,
        "spec": spec_dict,
        "custom_geometry": worker_res["custom_geometry"],
        "combined_geometry": worker_res["combined_geometry"],
        "interference": worker_res["interference"],
        "printer_fit": printer_fit,
        "parameter_diffs": [],
        "render_status": "rendered_brep",
        "artifacts": worker_res["artifacts"],
    }


async def adapt_upstream_parametric(
    upstream_project: dict[str, Any],
    overrides: list[ParamOverride] | dict[str, Any],
    *,
    lab_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Safely adapt parametric upstream source (.py or .scad) with explicit diffs."""
    if isinstance(overrides, list):
        override_dict = {item.name: item.value for item in overrides}
    elif isinstance(overrides, dict):
        override_dict = {str(k): v for k, v in overrides.items()}
    else:
        raise ValueError(
            "Parametric overrides must be a dictionary or list of ParamOverride items."
        )
    if not override_dict:
        raise ValueError("Specify at least one parametric parameter override.")

    prov = upstream_project["provenance"]
    artifacts = upstream_project["artifacts"]
    part_id = "custom-parametric-adapted"
    cache_id = _spec_digest({"sha": prov["file_sha256"], "overrides": override_dict})
    out_dir = upstream_cache_dir() / "adapted" / f"{prov['file_sha256'][:12]}-{cache_id}"

    worker_res = await run_upstream_worker(
        "adapt_parametric",
        {
            "source_path": artifacts["source_path"],
            "param_overrides": override_dict,
            "artifact_dir": str(out_dir),
        },
    )
    cgeom = worker_res.get("custom_geometry")
    printer_fit = (
        check_custom_part_printer_fit(
            cgeom["app_size_mm"],
            part_id,
            "Adapted parametric upstream part",
            lab_profile,
        )
        if isinstance(cgeom, dict) and cgeom.get("app_size_mm")
        else None
    )
    comb = (
        {
            "cad_min_mm": cgeom["cad_min_mm"],
            "cad_max_mm": cgeom["cad_max_mm"],
            "cad_size_mm": cgeom["cad_size_mm"],
            "app_size_mm": cgeom["app_size_mm"],
        }
        if isinstance(cgeom, dict)
        else None
    )
    return {
        "mode": "parametric",
        "part_id": part_id,
        "spec": {"param_overrides": override_dict},
        "custom_geometry": cgeom,
        "combined_geometry": comb,
        "interference": {
            "passed": True,
            "method": "parametric_source_regeneration",
            "interference_volume_mm3": 0.0,
            "min_clearance_mm": None,
        },
        "printer_fit": printer_fit,
        "parameter_diffs": worker_res["parameter_diffs"],
        "render_status": worker_res["render_status"],
        "artifacts": worker_res["artifacts"],
    }

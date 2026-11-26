"""Full 3D spatial envelope, access/ventilation clearance, and build-volume checks.

Coordinates in geometry modules follow Y-up conventions (X=width, Y=height, Z=depth).
This module translates them consistently to bench workspace axes (width_mm, depth_mm, height_mm)
and separates physical machine envelopes from required perimeter/service clearances.
"""

from __future__ import annotations

import math
from typing import Any

from app.upstream_cad import foundation_metadata
from app.workflows import current_kind

# Axis clearances documented per machine architecture (all values in mm).
OT2_BODY_MM = (630.0, 570.0, 660.0)  # width, depth, height
OT2_CLEARANCE_SPECS = (
    (
        "width_mm",
        "side_ventilation_mm",
        400.0,
        "Allow 200 mm left and 200 mm right for ventilation.",
    ),
    (
        "depth_mm",
        "rear_ventilation_mm",
        200.0,
        "Allow 200 mm behind the unit for rear ventilation.",
    ),
    (
        "depth_mm",
        "front_door_access_mm",
        200.0,
        "Allow 200 mm in front for door swing and deck access.",
    ),
    (
        "height_mm",
        "top_service_mm",
        150.0,
        "Allow 150 mm above the enclosure for top lid and pipette access.",
    ),
)

JUBILEE_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "rear_cable_access_mm",
        100.0,
        "Allow 100 mm rear space for cables, gantry belts, and airflow.",
    ),
    (
        "depth_mm",
        "front_door_access_mm",
        150.0,
        "Allow 150 mm front space for toolchanger/bed loading and door swing.",
    ),
    (
        "height_mm",
        "top_service_mm",
        100.0,
        "Allow 100 mm overhead clearance for Bowden tubes, umbilical, and toolhead servicing.",
    ),
)

BENCH_STATION_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "front_access_mm",
        100.0,
        "Allow 100 mm front operator clearance for manual handoffs and cables.",
    ),
    (
        "height_mm",
        "top_service_mm",
        100.0,
        "Allow 100 mm overhead clearance for instrument covers, camera mast, and loading.",
    ),
)

CONCEPT_DESIGN_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "front_access_mm",
        100.0,
        "Allow 100 mm front operator clearance beyond the swept motion footprint.",
    ),
    (
        "height_mm",
        "top_service_mm",
        50.0,
        "Allow 50 mm overhead clearance above the highest static/swept feature.",
    ),
)

STANDALONE_FIXTURE_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "front_access_mm",
        50.0,
        "Allow 50 mm front operator clearance for manual object insertion and removal.",
    ),
    (
        "height_mm",
        "top_service_mm",
        40.0,
        "Allow 40 mm overhead clearance above the specimen or camera arm for cabling.",
    ),
)

UPSTREAM_PROJECT_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "front_access_mm",
        100.0,
        "Allow 100 mm front operator clearance for loading and servicing upstream hardware.",
    ),
    (
        "height_mm",
        "top_service_mm",
        50.0,
        "Allow 50 mm overhead clearance above the adapted assembly for cables and motion.",
    ),
)

GENERIC_WORKSTATION_CLEARANCE_SPECS = (
    (
        "depth_mm",
        "front_access_mm",
        100.0,
        "Allow 100 mm front operator clearance for cables and deck access.",
    ),
    (
        "height_mm",
        "top_service_mm",
        100.0,
        "Allow 100 mm overhead clearance above the concept frame.",
    ),
)

GENERIC_MIN_WIDTH_MM = 400.0
GENERIC_HEIGHT_MM = 400.0


def _valid_limit(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _bounds_span_wdh(bounds: dict | None) -> tuple[float, float, float] | None:
    if not isinstance(bounds, dict):
        return None
    low = bounds.get("min_mm") if "min_mm" in bounds else bounds.get("min")
    high = bounds.get("max_mm") if "max_mm" in bounds else bounds.get("max")
    if (
        not isinstance(low, (list, tuple))
        or not isinstance(high, (list, tuple))
        or len(low) != 3
        or len(high) != 3
    ):
        return None
    return (
        float(high[0]) - float(low[0]),
        float(high[2]) - float(low[2]),
        float(high[1]) - float(low[1]),
    )


def _clearance_totals(
    specs: tuple[tuple[str, str, float, str], ...],
) -> tuple[dict[str, float], list[dict]]:
    totals = {"width_mm": 0.0, "depth_mm": 0.0, "height_mm": 0.0}
    items = []
    for axis, kind, mm, note in specs:
        totals[axis] += mm
        items.append({"axis": axis, "kind": kind, "mm": round(mm, 2), "note": note})
    return totals, items


def _resolve_subject_and_body(
    project: dict,
    *,
    platform_id: str | None = None,
    candidate_footprint: dict[str, float] | None = None,
) -> tuple[str, tuple[float, float, float], tuple[tuple[str, str, float, str], ...]]:
    design = project.get("design") or {}
    effective_platform = platform_id or (design.get("platform") or {}).get("id")

    if (
        effective_platform == "opentrons-ot2"
        or design.get("platform", {}).get("id") == "opentrons-ot2"
    ):
        return "opentrons-ot2", OT2_BODY_MM, OT2_CLEARANCE_SPECS

    if project.get("concept_design") and platform_id is None:
        concept = project["concept_design"]
        foot = concept.get("footprint_mm") or [0.0, 0.0]
        span = _bounds_span_wdh(concept.get("bounds_mm"))
        width = float(candidate_footprint.get("width_mm", foot[0])) if candidate_footprint else (
            float(foot[0])
        )
        depth = float(candidate_footprint.get("depth_mm", foot[1])) if candidate_footprint else (
            float(foot[1])
        )
        height = (
            float(candidate_footprint.get("height_mm", span[2] if span else 0.0))
            if candidate_footprint
            else float(span[2] if span else 0.0)
        )
        return "concept_design", (width, depth, height), CONCEPT_DESIGN_CLEARANCE_SPECS

    if project.get("standalone_fixture") and platform_id is None:
        fixture = project["standalone_fixture"]
        foot = fixture.get("footprint_mm") or [0.0, 0.0]
        height = float(fixture.get("height_mm", 0.0))
        width = (
            float(candidate_footprint.get("width_mm", foot[0]))
            if candidate_footprint
            else float(foot[0])
        )
        depth = (
            float(candidate_footprint.get("depth_mm", foot[1]))
            if candidate_footprint
            else float(foot[1])
        )
        if candidate_footprint and "height_mm" in candidate_footprint:
            height = float(candidate_footprint["height_mm"])
        return "standalone_fixture", (width, depth, height), STANDALONE_FIXTURE_CLEARANCE_SPECS

    if project.get("upstream_project") and platform_id is None:
        up = project["upstream_project"]
        adapt = up.get("adaptation") or {}
        comb = adapt.get("combined_geometry") or up.get("geometry") or {}
        app_size = comb.get("app_size_mm") or [0.0, 0.0, 0.0]
        width = (
            float(candidate_footprint.get("width_mm", app_size[0]))
            if candidate_footprint
            else float(app_size[0])
        )
        height = (
            float(candidate_footprint.get("height_mm", app_size[1]))
            if candidate_footprint
            else float(app_size[1])
        )
        depth = (
            float(candidate_footprint.get("depth_mm", app_size[2]))
            if candidate_footprint
            else float(app_size[2])
        )
        return "upstream_project", (width, depth, height), UPSTREAM_PROJECT_CLEARANCE_SPECS

    if project.get("bench_station") and platform_id is None:
        station = project["bench_station"]
        foot = station.get("footprint_mm") or [0.0, 0.0]
        modules = station.get("modules") or []
        height = max((float(m["size_mm"][1]) for m in modules if m.get("size_mm")), default=0.0)
        width = float(candidate_footprint.get("width_mm", foot[0])) if candidate_footprint else (
            float(foot[0])
        )
        depth = float(candidate_footprint.get("depth_mm", foot[1])) if candidate_footprint else (
            float(foot[1])
        )
        if candidate_footprint and "height_mm" in candidate_footprint:
            height = float(candidate_footprint["height_mm"])
        return "bench_station", (width, depth, height), BENCH_STATION_CLEARANCE_SPECS

    if effective_platform == "jubilee" or design.get("foundation"):
        bbox = (design.get("foundation") or foundation_metadata()).get("bbox_mm") or [
            483.143869005,
            564.358391585,
            612.687507964,
        ]
        span = None
        for source_bounds in (
            (project.get("cold_transfer_extension") or {}).get("assembly_bounds"),
            (project.get("inspection_extension") or {}).get("assembly_bounds"),
            design.get("assembly_bounds"),
        ):
            span = _bounds_span_wdh(source_bounds)
            if span is not None:
                break
        if span is None:
            span = (float(bbox[0]), float(bbox[2]), float(bbox[1]))
        if candidate_footprint:
            span = (
                float(candidate_footprint.get("width_mm", span[0])),
                float(candidate_footprint.get("depth_mm", span[1])),
                float(candidate_footprint.get("height_mm", span[2])),
            )
        return "jubilee", span, JUBILEE_CLEARANCE_SPECS

    foot = candidate_footprint or design.get("footprint") or {
        "width_mm": 600.0,
        "depth_mm": 370.0,
        "height_mm": GENERIC_HEIGHT_MM,
    }
    width = float(foot.get("width_mm", 600.0))
    width_limit = _valid_limit((project.get("summary") or {}).get("workspace_width_mm"))
    if (
        candidate_footprint is None
        and width_limit is not None
        and width_limit < GENERIC_MIN_WIDTH_MM
    ):
        width = GENERIC_MIN_WIDTH_MM
    depth = float(foot.get("depth_mm", 370.0))
    height = float(foot.get("height_mm", GENERIC_HEIGHT_MM))
    return "generic_workstation", (width, depth, height), GENERIC_WORKSTATION_CLEARANCE_SPECS


def evaluate_printable_fit(project: dict, lab_profile: dict | None) -> list[dict]:
    """Check printed/custom components against the configured 3D printer build volume."""
    if not isinstance(lab_profile, dict):
        return []
    printer = lab_profile.get("printer") or {}
    volume = printer.get("build_volume_mm")
    if not isinstance(volume, (list, tuple)) or len(volume) != 3:
        return []
    bx, by, bz = float(volume[0]), float(volume[1]), float(volume[2])
    if min(bx, by, bz) <= 0:
        return []

    candidates: list[tuple[str, str, list[float], str]] = []
    if rack := project.get("labware_rack"):
        if isinstance(rack.get("size_mm"), (list, tuple)) and len(rack["size_mm"]) == 3:
            candidates.append(
                (
                    "labware_rack",
                    "Custom tube rack",
                    [float(v) for v in rack["size_mm"]],
                    "user_measured",
                )
            )
    if plate := project.get("labware_plate"):
        if isinstance(plate.get("size_mm"), (list, tuple)) and len(plate["size_mm"]) == 3:
            candidates.append(
                (
                    "labware_plate_holder",
                    "96-well plate holder envelope",
                    [float(v) for v in plate["size_mm"]],
                    "reference",
                )
            )
    if tip_rack := project.get("tip_rack"):
        if tip_rack.get("kind") == "rim_supported_tip_rack":
            size = tip_rack.get("size_mm")
            if isinstance(size, (list, tuple)) and len(size) == 3:
                candidates.append(
                    (
                        "tip_rack_printed",
                        "Custom printed tip holder",
                        [float(v) for v in size],
                        "user_measured",
                    )
                )
    if concept := project.get("concept_design"):
        for comp in concept.get("components") or []:
            size = comp.get("size_mm")
            if comp.get("source_id") is None and isinstance(size, (list, tuple)) and len(size) == 3:
                candidates.append(
                    (
                        f"concept:{comp['key']}",
                        comp.get("name") or comp["key"],
                        [float(v) for v in size],
                        "custom_concept",
                    )
                )
    if fixture := project.get("standalone_fixture"):
        prov = (fixture.get("spec") or {}).get("dimension_provenance") or "reference"
        for part in fixture.get("parts") or []:
            size = part.get("size_mm")
            if part.get("printable") and isinstance(size, (list, tuple)) and len(size) == 3:
                candidates.append(
                    (
                        f"fixture:{part['id']}",
                        part.get("label") or part["id"],
                        [float(v) for v in size],
                        prov,
                    )
                )
    if up := project.get("upstream_project"):
        adapt = up.get("adaptation") or {}
        cgeom = adapt.get("custom_geometry") or {}
        app_size = cgeom.get("app_size_mm")
        if isinstance(app_size, (list, tuple)) and len(app_size) == 3:
            candidates.append(
                (
                    adapt.get("part_id") or "custom-upstream-part",
                    f"Adapted open-hardware part ({adapt.get('mode', 'mating')})",
                    [float(v) for v in app_size],
                    "upstream_cad_adaptation",
                )
            )

    checks = []
    printer_label = printer.get("name") or "Configured printer"
    for part_id, label, size_mm, basis in candidates:
        w, h, d = size_mm
        unrotated = w <= bx + 1e-6 and d <= by + 1e-6 and h <= bz + 1e-6
        rotated = d <= bx + 1e-6 and w <= by + 1e-6 and h <= bz + 1e-6
        fits = unrotated or rotated
        orientation = (
            "standard"
            if unrotated
            else ("rotated_90_xy" if rotated else "exceeds_build_volume")
        )
        overflow_axes = []
        if not fits:
            # Measure minimum overhang across standard and 90-deg XY orientations.
            std_over = (max(0.0, w - bx), max(0.0, d - by), max(0.0, h - bz))
            rot_over = (max(0.0, d - bx), max(0.0, w - by), max(0.0, h - bz))
            best = std_over if sum(std_over) <= sum(rot_over) else rot_over
            for axis_label, over in zip(("X", "Y", "Z"), best):
                if over > 1e-6:
                    overflow_axes.append(f"{axis_label} +{over:.1f} mm")
        if fits:
            detail = (
                f"{label} ({w:.1f} x {d:.1f} x {h:.1f} mm) fits inside "
                f"{printer_label} ({bx:g} x {by:g} x {bz:g} mm, {orientation})."
            )
        else:
            detail = (
                f"{label} ({w:.1f} x {d:.1f} x {h:.1f} mm) exceeds printer build volume "
                f"({bx:g} x {by:g} x {bz:g} mm): {', '.join(overflow_axes)}."
            )
        checks.append(
            {
                "part_id": part_id,
                "label": label,
                "basis": basis,
                "part_size_mm": [round(w, 2), round(d, 2), round(h, 2)],
                "part_axes_order": "width_x_depth_x_height",
                "build_volume_mm": [round(bx, 2), round(by, 2), round(bz, 2)],
                "printer_name": printer_label,
                "fits": fits,
                "orientation": orientation,
                "detail": detail,
            }
        )
    return checks


def evaluate_spatial_fit(
    project: dict,
    *,
    lab_profile: dict | None = None,
    candidate_footprint: dict[str, float] | None = None,
    platform_id: str | None = None,
) -> dict:
    """Evaluate 3D width/depth/height fit, perimeter clearances, and printer build volume."""
    from app.bom_engine import purchasing_route

    summary = project.get("summary") or {}
    limits = {
        axis: _valid_limit(summary.get("workspace_" + axis))
        for axis in ("width_mm", "depth_mm", "height_mm")
    }
    if (
        platform_id is None and candidate_footprint is None
        and current_kind(project) != "liquid_preparation"
        and purchasing_route(project) == "unselected"
    ):
        return {
            "status": "awaiting_geometry", "subject": "unselected",
            "axes": {
                axis: dict(body_mm=None, clearance_mm=None, required_mm=None,
                           limit_mm=limit, margin_mm=None, fits=None)
                for axis, limit in limits.items()
            },
            "clearances": [], "printable_checks": [], "violations": [],
        }
    subject, body_wdh, clearance_specs = _resolve_subject_and_body(
        project, platform_id=platform_id, candidate_footprint=candidate_footprint
    )
    mixed = None
    adapter_prints = []
    if project.get("cad_instances") and candidate_footprint is None and platform_id is None:
        from app.cad_placement import check_assembly
        from app.project_assembly import _geometry

        mixed, adapter_prints = check_assembly(project, _geometry(project), lab_profile)
        low, high = mixed["bounds_mm"]
        body_wdh = [max(native, high[i] - low[i]) for native, i in zip(body_wdh, (0, 2, 1))]
    clearances_by_axis, clearance_items = _clearance_totals(clearance_specs)
    axis_labels = {
        "width_mm": "workspace width (X)",
        "depth_mm": "workspace depth (Z)",
        "height_mm": "workspace height (Y)",
    }

    axes: dict[str, dict] = {}
    violations: list[dict] = []
    any_limit = any(v is not None for v in limits.values())

    for index, axis in enumerate(("width_mm", "depth_mm", "height_mm")):
        body = round(float(body_wdh[index]), 2)
        clearance = round(float(clearances_by_axis[axis]), 2)
        required = round(body + clearance, 2)
        limit = round(limits[axis], 2) if limits[axis] is not None else None
        if limit is None:
            margin = None
            fits = None
        else:
            margin = round(limit - required, 2)
            fits = required <= limit + 1e-6
            if not fits:
                overflow = round(required - limit, 2)
                violations.append(
                    {
                        "axis": axis,
                        "code": f"{axis.removesuffix('_mm')}_overflow",
                        "required_mm": required,
                        "limit_mm": limit,
                        "overflow_mm": overflow,
                        "message": (
                            f"Required {axis_labels[axis]} is {required:g} mm "
                            f"({body:g} mm body + {clearance:g} mm clearance), exceeding the "
                            f"{limit:g} mm limit by {overflow:g} mm."
                        ),
                    }
                )
        axes[axis] = {
            "body_mm": body,
            "clearance_mm": clearance,
            "required_mm": required,
            "limit_mm": limit,
            "margin_mm": margin,
            "fits": fits,
        }

    printable_checks = evaluate_printable_fit(project, lab_profile)
    status = "unconstrained" if not any_limit else ("infeasible" if violations else "fits")
    result: dict[str, Any] = {
        "status": status,
        "subject": subject,
        "axes": axes,
        "clearances": clearance_items,
        "printable_checks": printable_checks,
        "violations": violations,
    }
    if mixed:
        result["cad_assembly"] = mixed
        result["printable_checks"].extend(adapter_prints)
    if candidate_footprint is None and subject in (
        "jubilee",
        "opentrons-ot2",
        "generic_workstation",
    ):
        from app.kinematics_checks import check_workstation_reach_and_transit

        result["kinematics"] = check_workstation_reach_and_transit(project)
    return result


def refresh_spatial_fit(project: dict, *, lab_profile: dict | None = None) -> dict:
    """Attach deterministic spatial_fit metadata to project['design'] and sync status."""
    fit = evaluate_spatial_fit(project, lab_profile=lab_profile)
    design = project.get("design")
    if isinstance(design, dict):
        design["spatial_fit"] = fit
        if fit["status"] != "unconstrained":
            design["constraint_status"] = fit["status"]
        else:
            summary = project.get("summary") or {}
            if (
                summary.get("workspace_width_mm") is None
                and summary.get("workspace_depth_mm") is None
                and summary.get("workspace_height_mm") is None
            ):
                design["constraint_status"] = "unconstrained"
    return project


def format_spatial_violations(fit: dict) -> str:
    """Format concise human-readable spatial violation summary."""
    violations = fit.get("violations") or []
    return " ".join(v["message"] for v in violations if v.get("message"))

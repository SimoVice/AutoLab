"""Swept-volume clearance, tool reach, payload, and center-of-mass checks (Task 3.3).

All evaluators operate deterministically on compiled design dicts or project snapshots.
Swept-volume checks evaluate continuous sub-stepped intervals with Separating Axis
Theorem (SAT) oriented bounding boxes plus rotary chord sagitta margin. Mass and CoM
estimates derive from finish-based nominal material densities, explicitly tagged as
analytical estimates rather than weighed BOM totals.
"""

from __future__ import annotations

import math
from typing import Any

from app.concept_design import _mat_mul_vec, _rotation_matrix
from app.concept_interfaces import _rotate, work_interface_diagnostics

# Nominal engineering material densities in g / mm^3 (estimated by primitive finish).
FINISH_DENSITY_G_PER_MM3: dict[str, float] = {
    "frame": 0.00270,   # Aluminum 6061/6063
    "steel": 0.00785,   # Steel
    "polymer": 0.00124, # PLA/PETG
    "clear": 0.00119,   # Acrylic / PC
    "accent": 0.00270,  # Anodized aluminum
    "dark": 0.00120,    # ABS / Nylon
}
DEFAULT_DENSITY_G_PER_MM3 = 0.00124
DEFAULT_EXTRUSION_FILL_RATIO = 0.46
GRAVITY_M_S2 = 9.80665


def _identity_mat() -> tuple[tuple[float, float, float], ...]:
    return (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )


def _mat_mul(
    a: tuple[tuple[float, float, float], ...],
    b: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _axis_rotation_matrix(
    axis: str | None, angle_rad: float
) -> tuple[tuple[float, float, float], ...]:
    if axis is None or abs(angle_rad) <= 1e-12:
        return _identity_mat()
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    if axis == "x":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def _mat_cols(
    mat: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return (
        (mat[0][0], mat[1][0], mat[2][0]),
        (mat[0][1], mat[1][1], mat[2][1]),
        (mat[0][2], mat[1][2], mat[2][2]),
    )


def _dot(
    a: tuple[float, float, float] | list[float],
    b: tuple[float, float, float] | list[float],
) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length <= 1e-9:
        return None
    return (v[0] / length, v[1] / length, v[2] / length)


ObbEntry = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[tuple[float, float, float], ...],
]


def _primitive_local_obbs(primitive: dict) -> list[ObbEntry]:
    """Decompose a primitive into rest-local OBBs (brackets split into two open L-legs)."""
    sx, sy, sz = primitive["size_mm"]
    center = tuple(float(v) for v in primitive["center_mm"])
    rot = _rotation_matrix(primitive.get("rotation_deg")) or _identity_mat()
    if primitive.get("shape") != "bracket":
        return [(center, (sx / 2.0, sy / 2.0, sz / 2.0), rot)]
    t = primitive.get("wall_thickness_mm")
    if t is None:
        t = min(max(min(sx, sy) * 0.25, 2.0), 12.0, min(sx, sy) * 0.45)
    base_off = _mat_mul_vec(rot, (0.0, -sy / 2.0 + t / 2.0, 0.0))
    up_off = _mat_mul_vec(rot, (-sx / 2.0 + t / 2.0, 0.0, 0.0))
    return [
        (
            (center[0] + base_off[0], center[1] + base_off[1], center[2] + base_off[2]),
            (sx / 2.0, t / 2.0, sz / 2.0),
            rot,
        ),
        (
            (center[0] + up_off[0], center[1] + up_off[1], center[2] + up_off[2]),
            (t / 2.0, sy / 2.0, sz / 2.0),
            rot,
        ),
    ]


def _swept_obb_signed_distance(
    c0: tuple[float, float, float],
    c1: tuple[float, float, float],
    half_a: tuple[float, float, float],
    mat_a: tuple[tuple[float, float, float], ...],
    cb: tuple[float, float, float],
    half_b: tuple[float, float, float],
    mat_b: tuple[tuple[float, float, float], ...],
    sagitta_pad: float = 0.0,
) -> float:
    """Signed separation along separating axes for swept segment [c0, c1] OBB vs fixed OBB.

    Returns > 0 when strictly disjoint (minimum guaranteed clearance under separating axes),
    0 at flush contact, and < 0 when overlapping (magnitude = minimum separation depth).
    """
    ax = _mat_cols(mat_a)
    bx = _mat_cols(mat_b)
    seg = (c1[0] - c0[0], c1[1] - c0[1], c1[2] - c0[2])
    c_mid = ((c0[0] + c1[0]) * 0.5, (c0[1] + c1[1]) * 0.5, (c0[2] + c1[2]) * 0.5)
    t_vec = (cb[0] - c_mid[0], cb[1] - c_mid[1], cb[2] - c_mid[2])
    seg_half = (seg[0] * 0.5, seg[1] * 0.5, seg[2] * 0.5)

    candidates: list[tuple[float, float, float]] = [*ax, *bx]
    for a_i in ax:
        for b_j in bx:
            axis = _norm(_cross(a_i, b_j))
            if axis is not None:
                candidates.append(axis)
    seg_dir = _norm(seg)
    if seg_dir is not None:
        candidates.append(seg_dir)
        for edge_axis in (*ax, *bx):
            crossed = _norm(_cross(seg_dir, edge_axis))
            if crossed is not None:
                candidates.append(crossed)

    max_gap = -math.inf
    for axis in candidates:
        ra = (
            abs(_dot(axis, ax[0])) * half_a[0]
            + abs(_dot(axis, ax[1])) * half_a[1]
            + abs(_dot(axis, ax[2])) * half_a[2]
            + abs(_dot(axis, seg_half))
            + sagitta_pad
        )
        rb = (
            abs(_dot(axis, bx[0])) * half_b[0]
            + abs(_dot(axis, bx[1])) * half_b[1]
            + abs(_dot(axis, bx[2])) * half_b[2]
        )
        dist = abs(_dot(axis, t_vec))
        gap = dist - (ra + rb)
        if gap > max_gap:
            max_gap = gap
    return max_gap


def check_swept_volume_clearances(
    design: dict, *, min_clearance_mm: float = 2.0, substeps: int = 24
) -> dict[str, Any]:
    """Check swept motion trajectories against fixed parts using continuous-segment OBB SAT."""
    if min_clearance_mm <= 0 or substeps < 1 or substeps > 200:
        raise ValueError("min_clearance_mm must be > 0 and substeps between 1 and 200.")
    specs = {c["key"]: c for c in design["components"]}
    world_obbs: dict[str, list[tuple[int, tuple, tuple, tuple]]] = {}
    for key, comp in specs.items():
        px, py, pz = comp["position_mm"]
        entries = []
        for idx, prim in enumerate(comp["primitives"]):
            for lc, half, rot in _primitive_local_obbs(prim):
                entries.append((idx, (px + lc[0], py + lc[1], pz + lc[2]), half, rot))
        world_obbs[key] = entries

    interferences: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    seen_hits: set[tuple[int, str, int, str, int]] = set()
    seen_near: set[tuple[int, str, str]] = set()
    global_min_clearance: float | None = None
    checked_steps = 0

    for step_idx, step in enumerate(design.get("steps", [])):
        moving_key = step.get("moving_key")
        moving_keys = set(step.get("moving_component_keys") or [])
        if not moving_key or not moving_keys:
            continue
        checked_steps += 1
        root = specs[moving_key]["assembly_root_key"]
        fixed_keys = [
            k
            for k in specs
            if k not in moving_keys and specs[k]["assembly_root_key"] == root
        ]
        if not fixed_keys:
            continue
        trans = step["translation_mm"]
        rot_axis = step.get("rotation_axis")
        start_deg = float(step.get("start_rotation_deg", 0.0))
        delta_deg = float(step.get("rotation_delta_deg", step.get("rotation_deg", 0.0)))
        pivot_local = step.get("pivot_mm") or [0.0, 0.0, 0.0]
        pivot_world = tuple(
            a + b for a, b in zip(specs[moving_key]["position_mm"], pivot_local)
        )
        # Estimate chord sagitta pad per substep for rotary arcs.
        dtheta_sub = abs(math.radians(delta_deg)) / substeps if rot_axis else 0.0

        def pose_at(t: float, base_center: tuple, base_rot: tuple) -> tuple[tuple, tuple]:
            angle = math.radians(start_deg + delta_deg * t)
            rel = (
                base_center[0] - pivot_world[0],
                base_center[1] - pivot_world[1],
                base_center[2] - pivot_world[2],
            )
            rotated_rel = _rotate(rel, rot_axis, angle)
            pos = (
                pivot_world[0] + rotated_rel[0] + trans[0] * t,
                pivot_world[1] + rotated_rel[1] + trans[1] * t,
                pivot_world[2] + rotated_rel[2] + trans[2] * t,
            )
            r_t = _mat_mul(_axis_rotation_matrix(rot_axis, angle), base_rot)
            return pos, r_t

        for mkey in sorted(moving_keys):
            for m_idx, mc0, mhalf, mrot in world_obbs[mkey]:
                corner_radius = 0.0
                if rot_axis and abs(delta_deg) > 1e-7:
                    rel_pivot = math.sqrt(
                        sum((mc0[i] - pivot_world[i]) ** 2 for i in range(3))
                    )
                    corner_radius = rel_pivot + math.sqrt(sum(h * h for h in mhalf))
                sagitta_pad = (
                    corner_radius * (1.0 - math.cos(dtheta_sub * 0.5)) if dtheta_sub else 0.0
                )
                for fkey in fixed_keys:
                    for f_idx, fc, fhalf, frot in world_obbs[fkey]:
                        pair_min_gap = math.inf
                        pair_min_t = 0.0
                        for k in range(substeps):
                            t0 = k / substeps
                            t1 = (k + 1) / substeps
                            tm = 0.5 * (t0 + t1)
                            p0, _ = pose_at(t0, mc0, mrot)
                            pm, rm = pose_at(tm, mc0, mrot)
                            p1, _ = pose_at(t1, mc0, mrot)
                            # Evaluate swept linear segment [p0, p1] using orientation at midpoint.
                            gap = _swept_obb_signed_distance(
                                p0, p1, mhalf, rm, fc, fhalf, frot, sagitta_pad=sagitta_pad
                            )
                            # Clamp chord midpoint offset vs actual curved midpoint pm.
                            if dtheta_sub:
                                linear_mid = (
                                    0.5 * (p0[0] + p1[0]),
                                    0.5 * (p0[1] + p1[1]),
                                    0.5 * (p0[2] + p1[2]),
                                )
                                extra = math.sqrt(
                                    sum((pm[i] - linear_mid[i]) ** 2 for i in range(3))
                                )
                                gap_m = _swept_obb_signed_distance(
                                    pm, pm, mhalf, rm, fc, fhalf, frot, sagitta_pad=extra
                                )
                                gap = min(gap, gap_m)
                            if gap < pair_min_gap:
                                pair_min_gap = gap
                                pair_min_t = tm
                            if gap < -1e-6:
                                hit_key = (step_idx, mkey, m_idx, fkey, f_idx)
                                if hit_key not in seen_hits:
                                    seen_hits.add(hit_key)
                                    interferences.append(
                                        {
                                            "step_index": step_idx,
                                            "step_name": step["name"],
                                            "moving_key": mkey,
                                            "moving_primitive": m_idx,
                                            "fixed_key": fkey,
                                            "fixed_primitive": f_idx,
                                            "t_start": round(t0, 4),
                                            "t_end": round(t1, 4),
                                            "penetration_mm": round(-gap, 3),
                                            "detail": (
                                                f"Step {step_idx} ({step['name']}): moving "
                                                f"{mkey!r} primitive {m_idx} intersects fixed "
                                                f"{fkey!r} primitive {f_idx} during travel phase "
                                                f"t={t0:.2f}..{t1:.2f} (overlap {-gap:.2f} mm)."
                                            ),
                                        }
                                    )
                        rest_gap = _swept_obb_signed_distance(
                            mc0, mc0, mhalf, mrot, fc, fhalf, frot
                        )
                        is_flush_parent_bearing = (
                            fkey == specs[mkey].get("parent_key")
                            and -1e-4 <= rest_gap <= 1e-4
                            and pair_min_gap >= -1e-6
                        )
                        if not is_flush_parent_bearing and (
                            global_min_clearance is None or pair_min_gap < global_min_clearance
                        ):
                            global_min_clearance = pair_min_gap
                        if (
                            not is_flush_parent_bearing
                            and 0.0 <= pair_min_gap < min_clearance_mm
                        ):
                            near_key = (step_idx, mkey, fkey)
                            if near_key not in seen_near:
                                seen_near.add(near_key)
                                near_misses.append(
                                    {
                                        "step_index": step_idx,
                                        "step_name": step["name"],
                                        "moving_key": mkey,
                                        "moving_primitive": m_idx,
                                        "fixed_key": fkey,
                                        "fixed_primitive": f_idx,
                                        "t": round(pair_min_t, 4),
                                        "clearance_mm": round(pair_min_gap, 3),
                                        "detail": (
                                            f"Step {step_idx} ({step['name']}): {mkey!r} passes "
                                            f"within {pair_min_gap:.2f} mm of {fkey!r} at "
                                            f"t={pair_min_t:.2f} (< {min_clearance_mm:g} mm)."
                                        ),
                                    }
                                )

    min_clear_val = (
        round(global_min_clearance, 3) if global_min_clearance is not None else None
    )
    return {
        "passed": len(interferences) == 0,
        "min_clearance_mm": min_clear_val,
        "min_clearance_threshold_mm": min_clearance_mm,
        "checked_steps": checked_steps,
        "interferences": interferences[:24],
        "near_misses": near_misses[:24],
    }


def check_tool_reach(design: dict) -> dict[str, Any]:
    """Validate concept WorkInterface reach and linear carriage support overhangs."""
    specs = {c["key"]: c for c in design["components"]}
    interface_issues: list[dict[str, Any]] = []
    for diag in work_interface_diagnostics(design):
        if diag.get("needs_correction"):
            interface_issues.append(
                {
                    "kind": "interface_unreachable",
                    "interface_key": diag["key"],
                    "tool_key": diag["tool_key"],
                    "target_key": diag["target_key"],
                    "distance_mm": diag["distance_mm"],
                    "detail": (
                        f"Working axis {diag['key']!r} from {diag['tool_key']!r} fails to reach "
                        f"{diag['target_key']!r} within declared range "
                        f"(distance={diag['distance_mm']})."
                    ),
                }
            )

    overhangs: list[dict[str, Any]] = []
    seen_overhangs: set[tuple[int, str, str]] = set()
    for step_idx, step in enumerate(design.get("steps", [])):
        moving_key = step.get("moving_key")
        if not moving_key or moving_key not in specs:
            continue
        comp = specs[moving_key]
        parent_key = comp.get("parent_key")
        if not parent_key or parent_key not in specs:
            continue
        # Only flag if the parent itself is fixed (relative travel along stationary support).
        if parent_key in set(step.get("moving_component_keys") or []):
            continue
        parent = specs[parent_key]
        p_pos = parent["position_mm"]
        p_low = [p_pos[i] + parent["bounds_mm"]["min"][i] for i in range(3)]
        p_high = [p_pos[i] + parent["bounds_mm"]["max"][i] for i in range(3)]
        m_pos = comp["position_mm"]
        m_low = [m_pos[i] + comp["bounds_mm"]["min"][i] for i in range(3)]
        m_high = [m_pos[i] + comp["bounds_mm"]["max"][i] for i in range(3)]
        trans = step["translation_mm"]
        for axis_i, axis_name in enumerate(("X", "Y", "Z")):
            if abs(trans[axis_i]) <= 1e-6:
                continue
            # Check vertical only when supported along Y rather than thin horizontal plates.
            if axis_i == 1 and (p_high[1] - p_low[1]) < 2.0:
                continue
            span_m = max(m_high[axis_i] - m_low[axis_i], 1.0)
            swept_center = 0.5 * (m_low[axis_i] + m_high[axis_i]) + trans[axis_i]
            ext_lo = max(0.0, p_low[axis_i] - swept_center)
            ext_hi = max(0.0, swept_center - p_high[axis_i])
            overhang_mm = max(ext_lo, ext_hi)
            # Flag if swept carriage center passes >50% of its length outside parent guide span.
            if overhang_mm > 0.5 * span_m + 1e-6:
                key = (step_idx, moving_key, axis_name)
                if key not in seen_overhangs:
                    seen_overhangs.add(key)
                    overhangs.append(
                        {
                            "kind": "rail_overhang",
                            "step_index": step_idx,
                            "step_name": step["name"],
                            "moving_key": moving_key,
                            "parent_key": parent_key,
                            "axis": axis_name,
                            "overhang_mm": round(overhang_mm, 2),
                            "detail": (
                                f"Step {step_idx} ({step['name']}): {moving_key!r} center "
                                f"overhangs parent {parent_key!r} along {axis_name} by "
                                f"{overhang_mm:.1f} mm (> 50% carriage length)."
                            ),
                        }
                    )

    issues = interface_issues + overhangs
    return {
        "passed": len(issues) == 0,
        "interface_issues": interface_issues,
        "overhangs": overhangs,
        "issues": issues,
    }


def check_workstation_reach_and_transit(project: dict) -> dict[str, Any]:
    """Validate liquid workstation deck XY reach and Z-transit clearance over tallest labware."""
    design = project.get("design") or {}
    components = design.get("components") or []
    by_id = {c.get("id"): c for c in components if isinstance(c, dict) and c.get("id")}
    base = by_id.get("base")
    if base is None:
        return {"checked": False, "passed": True, "issues": [], "warnings": []}

    bx, by_c, bz = (float(v) for v in base["position"])
    bsx, bsy, bsz = (float(v) for v in base["size"])
    base_top_y = by_c + bsy / 2.0
    deck_min_x, deck_max_x = bx - bsx / 2.0, bx + bsx / 2.0
    deck_min_z, deck_max_z = bz - bsz / 2.0, bz + bsz / 2.0

    issues: list[str] = []
    warnings: list[str] = []
    out_of_reach: list[dict[str, Any]] = []

    deck_item_ids = ("source-rack", "sample-rack", "tip-rack", "tip-waste", "controller")
    tallest_top_y = base_top_y
    tallest_id = "base"

    for cid in deck_item_ids:
        comp = by_id.get(cid)
        if comp is None:
            continue
        cx, cy, cz = (float(v) for v in comp["position"])
        sx, sy, sz = (float(v) for v in comp["size"])
        c_min_x, c_max_x = cx - sx / 2.0, cx + sx / 2.0
        c_min_z, c_max_z = cz - sz / 2.0, cz + sz / 2.0
        top_y = cy + sy / 2.0
        if top_y > tallest_top_y:
            tallest_top_y = top_y
            tallest_id = cid

        if (
            c_min_x < deck_min_x - 1e-6
            or c_max_x > deck_max_x + 1e-6
            or c_min_z < deck_min_z - 1e-6
            or c_max_z > deck_max_z + 1e-6
        ):
            msg = (
                f"Deck item {cid!r} extends outside usable {bsx:g}x{bsz:g} mm platform envelope."
            )
            issues.append(msg)
            out_of_reach.append({"component_id": cid, "detail": msg})

    plate = project.get("labware_plate")
    sample_comp = by_id.get("sample-rack")
    if isinstance(plate, dict) and sample_comp is not None:
        scx, _scy, scz = (float(v) for v in sample_comp["position"])
        wells = (plate.get("derived") or {}).get("wells") or []
        bad_wells = [
            w["label"]
            for w in wells
            if not (
                deck_min_x <= scx + float(w["app_x_mm"]) <= deck_max_x
                and deck_min_z <= scz + float(w["app_z_mm"]) <= deck_max_z
            )
        ]
        if bad_wells:
            msg = f"Microplate wells {bad_wells[:4]} fall outside usable deck XY reach."
            issues.append(msg)
            out_of_reach.append(
                {"component_id": "sample-rack", "wells": bad_wells[:8], "detail": msg}
            )

    z_transit_clearance_mm: float | None = None
    pipette = by_id.get("pipette")
    if pipette is not None:
        pipette_bottom_y = float(pipette["position"][1]) - float(pipette["size"][1]) / 2.0
        z_transit_clearance_mm = round(pipette_bottom_y - tallest_top_y, 2)
        if z_transit_clearance_mm < 0.0:
            issues.append(
                f"Z-transit collision: parked pipette tip level ({pipette_bottom_y:.1f} mm) "
                f"overlaps tallest deck item {tallest_id!r} ({tallest_top_y:.1f} mm) by "
                f"{-z_transit_clearance_mm:.1f} mm."
            )
        elif z_transit_clearance_mm < 5.0:
            warnings.append(
                f"Tight Z-transit clearance ({z_transit_clearance_mm:.1f} mm) above "
                f"{tallest_id!r}; increase bed drop or gantry Z headroom."
            )

    return {
        "checked": True,
        "passed": len(issues) == 0,
        "deck_reach_mm": [round(bsx, 2), round(bsz, 2)],
        "tallest_obstacle_id": tallest_id,
        "tallest_obstacle_height_mm": round(tallest_top_y - base_top_y, 2),
        "z_transit_clearance_mm": z_transit_clearance_mm,
        "out_of_reach": out_of_reach,
        "issues": issues,
        "warnings": warnings,
    }


def _feature_removed_volume_mm3(feature: dict, size_mm: list[float]) -> float:
    face = feature.get("face", "+y")
    sx, sy, sz = size_mm
    span = sy if face in ("+y", "-y") else (sz if face in ("+z", "-z") else sx)
    kind = feature.get("kind")
    if kind == "through_hole":
        r = feature["diameter_mm"] * 0.5
        return math.pi * r * r * (feature.get("depth_mm") or span)
    if kind == "counterbore":
        r = feature["diameter_mm"] * 0.5
        rc = feature["counterbore_diameter_mm"] * 0.5
        d_cb = feature["counterbore_depth_mm"]
        return math.pi * r * r * span + math.pi * (rc * rc - r * r) * min(d_cb, span)
    if kind == "pocket":
        d_p = min(feature["depth_mm"], span)
        if feature.get("diameter_mm") is not None:
            r = feature["diameter_mm"] * 0.5
            return math.pi * r * r * d_p
        return feature["length_mm"] * feature["width_mm"] * d_p
    if kind == "slot":
        w = feature["width_mm"]
        length = feature["length_mm"]
        d_s = min(feature.get("depth_mm") or span, span)
        area = (length - w) * w + math.pi * (w * 0.5) ** 2
        return area * d_s
    if kind == "hole_pattern":
        cnt = (
            feature["count_x"] * feature["count_y"]
            if feature.get("pattern_type") == "rect"
            else feature["count"]
        )
        r = feature["diameter_mm"] * 0.5
        base_vol = math.pi * r * r * (feature.get("depth_mm") or span)
        if feature.get("counterbore_diameter_mm") and feature.get("counterbore_depth_mm"):
            rc = feature["counterbore_diameter_mm"] * 0.5
            base_vol += math.pi * (rc * rc - r * r) * min(feature["counterbore_depth_mm"], span)
        return cnt * base_vol
    return 0.0


def _primitive_volume_and_mass_g(primitive: dict) -> tuple[float, float]:
    sx, sy, sz = primitive["size_mm"]
    shape = primitive["shape"]
    if shape in ("box", "plate"):
        vol = sx * sy * sz
    elif shape == "sphere":
        vol = (4.0 / 3.0) * math.pi * (sx * 0.5) ** 3
    elif shape == "cylinder":
        ax = primitive.get("axis") or "y"
        if ax == "x":
            vol = math.pi * (sy * 0.5) ** 2 * sx
        elif ax == "z":
            vol = math.pi * (sx * 0.5) ** 2 * sz
        else:
            vol = math.pi * (sx * 0.5) ** 2 * sy
    elif shape == "extrusion":
        vol = sx * sy * sz * DEFAULT_EXTRUSION_FILL_RATIO
    elif shape == "bracket":
        t = primitive.get("wall_thickness_mm")
        if t is None:
            t = min(max(min(sx, sy) * 0.25, 2.0), 12.0, min(sx, sy) * 0.45)
        vol = (sx * t + (sy - t) * t) * sz
    else:
        vol = sx * sy * sz

    removed = sum(
        _feature_removed_volume_mm3(f, [sx, sy, sz]) for f in (primitive.get("features") or [])
    )
    vol = max(0.05 * vol, vol - removed)
    density = FINISH_DENSITY_G_PER_MM3.get(
        primitive.get("finish", "polymer"), DEFAULT_DENSITY_G_PER_MM3
    )
    return vol, vol * density


def _convex_hull_2d(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain 2D convex hull returning unique vertices in CCW order."""
    pts = sorted({(round(x, 6), round(z, 6)) for x, z in points})
    if len(pts) <= 2:
        return pts

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _point_to_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    vx, vz = b[0] - a[0], b[1] - a[1]
    wx, wz = p[0] - a[0], p[1] - a[1]
    seg_len2 = vx * vx + vz * vz
    if seg_len2 <= 1e-12:
        return math.hypot(wx, wz)
    t = max(0.0, min(1.0, (wx * vx + wz * vz) / seg_len2))
    proj_x, proj_z = a[0] + t * vx, a[1] + t * vz
    return math.hypot(p[0] - proj_x, p[1] - proj_z)


def _signed_margin_to_hull(
    point: tuple[float, float], hull: list[tuple[float, float]]
) -> float:
    if not hull:
        return -999.0
    if len(hull) == 1:
        return -math.hypot(point[0] - hull[0][0], point[1] - hull[0][1])
    if len(hull) == 2:
        return -_point_to_segment_distance(point, hull[0], hull[1])
    px, pz = point
    min_inside = math.inf
    min_outside = math.inf
    inside = True
    n = len(hull)
    for i in range(n):
        a = hull[i]
        b = hull[(i + 1) % n]
        dx, dz = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dz)
        if length <= 1e-12:
            continue
        # Leftward normal for CCW polygon points inside.
        signed_d = ((px - a[0]) * (-dz) + (pz - a[1]) * dx) / length
        if signed_d < -1e-7:
            inside = False
        if signed_d >= 0.0:
            min_inside = min(min_inside, signed_d)
        else:
            min_outside = min(min_outside, _point_to_segment_distance(point, a, b))
    return min_inside if inside else -min_outside


def check_payload_and_com(
    design: dict,
    *,
    moving_payload_limit_g: float = 2500.0,
    min_stability_margin_mm: float = 15.0,
    max_cantilever_moment_nm: float = 2.5,
) -> dict[str, Any]:
    """Estimate masses, 3D CoM, bench tip-over stability margins, and moving step payloads."""
    specs = {c["key"]: c for c in design["components"]}
    comp_masses: dict[str, float] = {}
    comp_weighted_pos: dict[str, list[float]] = {}
    ground_pts_by_root: dict[str, list[tuple[float, float]]] = {}
    total_by_root: dict[str, float] = {}
    com_accum_by_root: dict[str, list[float]] = {}

    for key, comp in specs.items():
        root = comp["assembly_root_key"]
        px, py, pz = comp["position_mm"]
        mass_sum = 0.0
        w_pos = [0.0, 0.0, 0.0]
        for prim in comp["primitives"]:
            _vol, mass_g = _primitive_volume_and_mass_g(prim)
            mass_sum += mass_g
            rot = _rotation_matrix(prim.get("rotation_deg")) or _identity_mat()
            cx, cy, cz = (
                px + prim["center_mm"][0],
                py + prim["center_mm"][1],
                pz + prim["center_mm"][2],
            )
            w_pos[0] += cx * mass_g
            w_pos[1] += cy * mass_g
            w_pos[2] += cz * mass_g

            hx, hy, hz = (v * 0.5 for v in prim["size_mm"])
            corners = [
                _mat_mul_vec(rot, (sx * hx, sy * hy, sz * hz))
                for sx in (-1.0, 1.0)
                for sy in (-1.0, 1.0)
                for sz in (-1.0, 1.0)
            ]
            low_y = min(cy + c[1] for c in corners)
            if low_y <= 1.0:
                for c in corners:
                    if cy + c[1] <= 1.0 + max(2.0, hy * 2.0):
                        ground_pts_by_root.setdefault(root, []).append((cx + c[0], cz + c[2]))

        comp_masses[key] = mass_sum
        comp_weighted_pos[key] = [
            (w_pos[i] / mass_sum if mass_sum > 0 else comp["position_mm"][i]) for i in range(3)
        ]
        total_by_root[root] = total_by_root.get(root, 0.0) + mass_sum
        acc = com_accum_by_root.setdefault(root, [0.0, 0.0, 0.0])
        for i in range(3):
            acc[i] += w_pos[i]

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    root_stability: list[dict[str, Any]] = []

    for root, total_mass in total_by_root.items():
        acc = com_accum_by_root[root]
        com = [acc[i] / total_mass if total_mass > 0 else 0.0 for i in range(3)]
        hull = _convex_hull_2d(ground_pts_by_root.get(root, []))
        margin = _signed_margin_to_hull((com[0], com[2]), hull)
        status = "stable"
        if margin < 0.0:
            status = "tip_over_risk"
            issues.append(
                {
                    "kind": "tip_over_risk",
                    "assembly_root_key": root,
                    "stability_margin_mm": round(margin, 2),
                    "detail": (
                        f"Assembly {root!r} estimated CoM ({com[0]:.1f}, {com[2]:.1f} mm) "
                        f"falls {-margin:.1f} mm outside its bench footprint (tip-over risk)."
                    ),
                }
            )
        elif margin < min_stability_margin_mm:
            status = "narrow_margin"
            warnings.append(
                {
                    "kind": "narrow_stability_margin",
                    "assembly_root_key": root,
                    "stability_margin_mm": round(margin, 2),
                    "detail": (
                        f"Assembly {root!r} stability margin is {margin:.1f} mm "
                        f"(< {min_stability_margin_mm:g} mm recommended); add base width."
                    ),
                }
            )
        root_stability.append(
            {
                "assembly_root_key": root,
                "estimated_mass_g": round(total_mass, 1),
                "com_mm": [round(v, 2) for v in com],
                "support_hull_vertices": len(hull),
                "stability_margin_mm": round(margin, 2),
                "status": status,
            }
        )

    moving_steps: list[dict[str, Any]] = []
    for step_idx, step in enumerate(design.get("steps", [])):
        moving_key = step.get("moving_key")
        moving_keys = step.get("moving_component_keys") or []
        if not moving_key or not moving_keys:
            continue
        payload_g = sum(comp_masses[k] for k in moving_keys)
        trans = step.get("translation_mm") or [0.0, 0.0, 0.0]
        anchor = specs[moving_key]["position_mm"]
        parent_key = specs[moving_key].get("parent_key")
        if parent_key and parent_key in specs:
            anchor = specs[parent_key]["position_mm"]
        # Worst-case lever arm across rest and displaced midpoint.
        sub_com = [
            sum(comp_weighted_pos[k][i] * comp_masses[k] for k in moving_keys)
            / max(payload_g, 1e-9)
            for i in range(3)
        ]
        lever_rest = math.hypot(sub_com[0] - anchor[0], sub_com[2] - anchor[2])
        lever_shifted = math.hypot(
            sub_com[0] + trans[0] - anchor[0], sub_com[2] + trans[2] - anchor[2]
        )
        max_lever_mm = max(lever_rest, lever_shifted)
        moment_nm = (payload_g / 1000.0) * GRAVITY_M_S2 * (max_lever_mm / 1000.0)
        if payload_g > moving_payload_limit_g:
            issues.append(
                {
                    "kind": "payload_exceeded",
                    "step_index": step_idx,
                    "step_name": step["name"],
                    "moving_key": moving_key,
                    "payload_g": round(payload_g, 1),
                    "limit_g": moving_payload_limit_g,
                    "detail": (
                        f"Step {step_idx} ({step['name']}) moves estimated {payload_g:.0f} g "
                        f"for {moving_key!r} (> {moving_payload_limit_g:g} g actuator limit)."
                    ),
                }
            )
        if moment_nm > max_cantilever_moment_nm:
            warnings.append(
                {
                    "kind": "high_cantilever_moment",
                    "step_index": step_idx,
                    "step_name": step["name"],
                    "moving_key": moving_key,
                    "moment_nm": round(moment_nm, 3),
                    "detail": (
                        f"Step {step_idx} ({step['name']}) creates ~{moment_nm:.2f} N*m "
                        f"cantilevered moment on {moving_key!r} (> {max_cantilever_moment_nm:g})."
                    ),
                }
            )
        moving_steps.append(
            {
                "step_index": step_idx,
                "step_name": step["name"],
                "moving_key": moving_key,
                "estimated_payload_g": round(payload_g, 1),
                "cantilever_moment_nm": round(moment_nm, 3),
            }
        )

    total_mass_g = round(sum(comp_masses.values()), 1)
    return {
        "passed": len(issues) == 0,
        "mass_basis": "estimated_from_finish_density_not_verified_bom",
        "total_estimated_mass_g": total_mass_g,
        "component_masses_g": {k: round(v, 1) for k, v in comp_masses.items()},
        "root_stability": root_stability,
        "moving_steps": moving_steps,
        "issues": issues,
        "warnings": warnings,
    }


def evaluate_concept_kinematics(
    design: dict,
    *,
    min_clearance_mm: float = 2.0,
    substeps: int = 24,
    moving_payload_limit_g: float = 2500.0,
    min_stability_margin_mm: float = 15.0,
) -> dict[str, Any]:
    """Run swept-volume clearance, tool reach, and payload/CoM checks on a compiled concept."""
    swept = check_swept_volume_clearances(
        design, min_clearance_mm=min_clearance_mm, substeps=substeps
    )
    reach = check_tool_reach(design)
    payload = check_payload_and_com(
        design,
        moving_payload_limit_g=moving_payload_limit_g,
        min_stability_margin_mm=min_stability_margin_mm,
    )
    issues = (
        [item["detail"] for item in swept["interferences"]]
        + [item["detail"] for item in reach["issues"]]
        + [item["detail"] for item in payload["issues"]]
    )
    warnings = (
        [item["detail"] for item in swept["near_misses"]]
        + [item["detail"] for item in payload["warnings"]]
    )
    return {
        "passed": swept["passed"] and reach["passed"] and payload["passed"],
        "swept_clearances": swept,
        "tool_reach": reach,
        "payload_and_com": payload,
        "issues": issues,
        "warnings": warnings,
        "limits": [
            "Swept-volume checks assume rigid travel between resting and target poses; "
            "flexure, backlash, cables, and fasteners are not simulated.",
            "Mass and center-of-mass estimates use nominal densities by primitive finish and "
            "do not replace measured part weights or structural FEA.",
        ],
    }

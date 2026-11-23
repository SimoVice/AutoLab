"""Editable STEP exports of the canonical conceptual assembly.

Coordinates are millimetres with Y up, matching the viewer and STL. The model
represents proposed primitive solids; it is not a toleranced fabrication drawing.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from app.concept_design import compile_concept

# Boolean operations grow rapidly with solid count. Bound a single request and
# never start an unbounded BRep generation as part of the standard ZIP export.
MAX_STEP_PRIMITIVES = 200
_CAD_LOCK = Lock()


class ConceptCADUnavailable(RuntimeError):
    """The optional CAD dependency is not installed."""


def concept_step(design: dict, component_key: str | None = None) -> bytes:
    """Export a named assembly in world coordinates, or one part in local coordinates."""
    canonical = compile_concept(design['proposal'], design.get('workspace_width_mm'))
    components = canonical['components']
    if component_key is not None:
        components = [component for component in components if component['key'] == component_key]
        if not components:
            raise ValueError('Unknown component model key.')
    count = sum(len(component['primitives']) for component in components)
    if count > MAX_STEP_PRIMITIVES:
        raise ValueError(f'STEP export supports up to {MAX_STEP_PRIMITIVES} primitives per file.')
    # Only geometry and component names affect this output, not user history or prices.
    data = [{'key': c['key'], 'name': c['name'], 'position_mm': c['position_mm'],
             'primitives': c['primitives']} for c in components]
    encoded = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return _render_step(encoded, component_key is None)


@lru_cache(maxsize=4)
def _render_step(encoded: str, world_coordinates: bool) -> bytes:
    # Serialize native CAD operations so concurrent downloads cannot multiply
    # their memory cost or contend in the underlying STEP writer.
    with _CAD_LOCK:
        return _write_step(encoded, world_coordinates)


def _extrusion_solid(profile: str, size_mm: list[float], axis: str):
    """Build a T-slot 2020/2040/4040 aluminum extrusion bar centered at local origin."""
    from build123d import Align, Box, Compound, Cylinder, Pos, Rot

    sx, sy, sz = size_mm
    if axis == 'z':
        wu, hv, lw = sx, sy, sz
    elif axis == 'x':
        wu, hv, lw = sz, sy, sx
    else:
        wu, hv, lw = sx, sz, sy
    align = (Align.CENTER,) * 3
    bar = Box(wu, hv, lw, align=align)
    cutters = [Cylinder(2.5, lw + 2.0, align=align)]
    cols = max(1, int(round(wu / 20.0)))
    rows = max(1, int(round(hv / 20.0)))
    hu, hv_half = wu / 2.0, hv / 2.0
    # T-slot throat (6.0 x 2.0) and undercut cavity (10.0 x 2.6) clear orthogonal slots & Ø5 bore.
    for r in range(rows):
        yc = -hv_half + (r + 0.5) * (hv / rows)
        for sign in (-1.0, 1.0):
            cutters.append(Pos(sign * (hu - 0.9), yc, 0) * Box(2.2, 6.0, lw + 2.0, align=align))
            cutters.append(Pos(sign * (hu - 3.1), yc, 0) * Box(2.6, 10.0, lw + 2.0, align=align))
    for c in range(cols):
        xc = -hu + (c + 0.5) * (wu / cols)
        for sign in (-1.0, 1.0):
            cutters.append(
                Pos(xc, sign * (hv_half - 0.9), 0) * Box(6.0, 2.2, lw + 2.0, align=align)
            )
            cutters.append(
                Pos(xc, sign * (hv_half - 3.1), 0) * Box(10.0, 2.6, lw + 2.0, align=align)
            )
    bar = bar - Compound(children=cutters)
    if axis == 'y':
        return Rot(90, 0, 0) * bar
    if axis == 'x':
        return Rot(0, 90, 0) * bar
    return bar


def _apply_edge_finish(solid, fillet_mm, chamfer_mm, min_dim):
    """Apply clamped fillet or chamfer cleanly without invalidating B-Rep topology."""
    if fillet_mm is None and chamfer_mm is None:
        return solid
    from build123d import chamfer, fillet

    radius = fillet_mm if fillet_mm is not None else chamfer_mm
    bounded = min(float(radius), max(0.2, float(min_dim) * 0.32))
    try:
        edges = solid.edges()
        if not edges:
            return solid
        candidate = fillet(edges, radius=bounded) if fillet_mm is not None else chamfer(
            edges, length=bounded
        )
        if candidate is not None and candidate.is_valid and candidate.solids():
            return candidate
    except Exception:
        pass
    return solid


def _feature_cutters(
    feature: dict,
    size_mm: list[float],
    shape_kind: str = 'box',
    wall_t: float | None = None,
):
    """Build local 3D solid cutters oriented onto the named primitive face."""
    import math

    from build123d import Align, Box, Cylinder, Pos, Rot

    face = feature.get('face', '+y')
    sx, sy, sz = size_mm
    u0, v0 = feature.get('offset_mm', [0.0, 0.0])
    align = (Align.CENTER,) * 3
    if face in ('+y', '-y'):
        span = sy
    elif face in ('+z', '-z'):
        span = sz
    else:
        span = sx

    # L-bracket step faces (+y over base leg, +x over upright leg) start inside the AABB.
    surface_inset = 0.0
    if shape_kind == 'bracket' and wall_t is not None:
        if face == '+y' and u0 > -sx / 2.0 + wall_t + 1e-6:
            surface_inset = sy - wall_t
        elif face == '+x' and v0 > -sy / 2.0 + wall_t + 1e-6:
            surface_inset = sx - wall_t

    def hole_positions():
        kind = feature['kind']
        if kind != 'hole_pattern':
            return [(u0, v0)]
        if feature['pattern_type'] == 'rect':
            cx, cy = feature['count_x'], feature['count_y']
            px, py = feature['pitch_x_mm'], feature['pitch_y_mm']
            pts = []
            for ix in range(cx):
                for iy in range(cy):
                    pts.append((
                        u0 + (ix - (cx - 1) / 2.0) * px,
                        v0 + (iy - (cy - 1) / 2.0) * py,
                    ))
            return pts
        cnt = feature['count']
        rad = feature['bolt_circle_diameter_mm'] / 2.0
        a0 = math.radians(feature.get('start_angle_deg', 0.0))
        return [
            (u0 + rad * math.cos(a0 + 2 * math.pi * i / cnt),
             v0 + rad * math.sin(a0 + 2 * math.pi * i / cnt))
            for i in range(cnt)
        ]

    # Use explicit face-aligned helper so (u, v) matches +X/+Z on ±Y, +X/+Y on ±Z, +Z/+Y on ±X.
    def place_exact(cutter, u_mm: float, v_mm: float, center_d: float):
        if face == '+y':
            return Pos(u_mm, sy / 2.0 - center_d, v_mm) * Rot(90, 0, 0) * cutter
        if face == '-y':
            return Pos(u_mm, -sy / 2.0 + center_d, v_mm) * Rot(-90, 0, 0) * cutter
        if face == '+z':
            return Pos(u_mm, v_mm, sz / 2.0 - center_d) * Rot(0, 180, 0) * cutter
        if face == '-z':
            return Pos(u_mm, v_mm, -sz / 2.0 + center_d) * cutter
        if face == '+x':
            return Pos(sx / 2.0 - center_d, v_mm, u_mm) * Rot(0, -90, 0) * cutter
        return Pos(-sx / 2.0 + center_d, v_mm, u_mm) * Rot(0, 90, 0) * cutter

    cutters = []
    kind = feature['kind']
    if kind in ('through_hole', 'counterbore', 'hole_pattern'):
        r_hole = feature['diameter_mm'] / 2.0
        cb_d = feature.get('counterbore_diameter_mm')
        cb_depth = feature.get('counterbore_depth_mm')
        through_h = (feature['depth_mm'] + 1.0) if feature.get('depth_mm') else (span + 2.0)
        through_cd = (
            surface_inset + (feature['depth_mm'] - 1.0) / 2.0
            if feature.get('depth_mm')
            else span / 2.0
        )
        for pu, pv in hole_positions():
            cutters.append(
                place_exact(Cylinder(r_hole, through_h, align=align), pu, pv, through_cd)
            )
            if cb_d is not None and cb_depth is not None:
                cb_h = cb_depth + 1.0
                cb_cd = surface_inset + (cb_depth - 1.0) / 2.0
                cutters.append(place_exact(Cylinder(cb_d / 2.0, cb_h, align=align), pu, pv, cb_cd))
    elif kind == 'pocket':
        p_depth = feature['depth_mm']
        cut_h = p_depth + 1.0
        center_d = surface_inset + (p_depth - 1.0) / 2.0
        if feature.get('diameter_mm') is not None:
            shape = Cylinder(feature['diameter_mm'] / 2.0, cut_h, align=align)
        else:
            shape = Box(feature['length_mm'], feature['width_mm'], cut_h, align=align)
        cutters.append(place_exact(shape, u0, v0, center_d))
    elif kind == 'slot':
        w = feature['width_mm']
        length = feature['length_mm']
        cut_h = (feature['depth_mm'] + 1.0) if feature.get('depth_mm') else (span + 2.0)
        center_d = (
            surface_inset + (feature['depth_mm'] - 1.0) / 2.0
            if feature.get('depth_mm')
            else span / 2.0
        )
        if length <= w + 1e-6:
            slot_core = Cylinder(w / 2.0, cut_h, align=align)
        else:
            span_len = length - w
            slot_core = Box(span_len, w, cut_h, align=align)
            slot_core = slot_core.fuse(
                Pos(-span_len / 2.0, 0, 0) * Cylinder(w / 2.0, cut_h, align=align)
            ).fuse(
                Pos(span_len / 2.0, 0, 0) * Cylinder(w / 2.0, cut_h, align=align)
            )
        angle = feature.get('slot_angle_deg', 0.0)
        if angle:
            slot_core = Rot(0, 0, angle) * slot_core
        cutters.append(place_exact(slot_core, u0, v0, center_d))
    return cutters


def _primitive_solid(primitive: dict, offset: list[float]):
    """Build one local or world-positioned build123d solid with rich features."""
    from build123d import Align, Box, Compound, Cylinder, Pos, Rot, Sphere

    sx, sy, sz = primitive['size_mm']
    x, y, z = [a + b for a, b in zip(primitive['center_mm'], offset)]
    kind = primitive['shape']
    axis = primitive.get('axis') or 'y'
    align = (Align.CENTER,) * 3
    wall_t = None
    if kind in ('box', 'plate'):
        solid = Box(sx, sy, sz, align=align)
        min_dim = min(sx, sy, sz)
    elif kind == 'cylinder':
        if axis == 'x':
            solid = Rot(0, 90, 0) * Cylinder(sy / 2.0, sx, align=align)
            min_dim = min(sx, sy)
        elif axis == 'z':
            solid = Cylinder(sx / 2.0, sz, align=align)
            min_dim = min(sx, sz)
        else:
            solid = Rot(90, 0, 0) * Cylinder(sx / 2.0, sy, align=align)
            min_dim = min(sx, sy)
    elif kind == 'sphere':
        solid = Sphere(sx / 2.0)
        min_dim = sx
    elif kind == 'extrusion':
        solid = _extrusion_solid(primitive.get('profile') or '2020', [sx, sy, sz], axis)
        min_dim = 20.0
    elif kind == 'bracket':
        wall_t = primitive.get('wall_thickness_mm')
        if wall_t is None:
            wall_t = min(max(min(sx, sy) * 0.25, 2.0), 12.0, min(sx, sy) * 0.45)
        base = Pos(0, -sy / 2.0 + wall_t / 2.0, 0) * Box(sx, wall_t, sz, align=align)
        upright = Pos(-sx / 2.0 + wall_t / 2.0, 0, 0) * Box(wall_t, sy, sz, align=align)
        solid = base.fuse(upright)
        min_dim = min(wall_t, sz)
    else:
        raise ValueError('Unsupported conceptual solid.')

    solid = _apply_edge_finish(
        solid, primitive.get('fillet_mm'), primitive.get('chamfer_mm'), min_dim
    )
    features = primitive.get('features') or []
    if features:
        all_cutters = []
        for feature in features:
            all_cutters.extend(_feature_cutters(feature, [sx, sy, sz], kind, wall_t))
        if all_cutters:
            cut_result = solid - Compound(children=all_cutters)
            if cut_result is None or not cut_result.is_valid or not cut_result.solids():
                raise ValueError('Feature boolean cut produced an invalid solid.')
            solid = cut_result
    rot = primitive.get('rotation_deg')
    if rot and any(abs(a) > 1e-7 for a in rot):
        rx, ry, rz = rot
        solid = Rot(0, 0, rz) * Rot(0, ry, 0) * Rot(rx, 0, 0) * solid
    return Pos(x, y, z) * solid


def _write_step(encoded: str, world_coordinates: bool) -> bytes:
    try:
        from build123d import Compound, Unit, export_step
    except ImportError as exc:
        raise ConceptCADUnavailable('STEP export requires the optional CAD installation.') from exc

    components = json.loads(encoded)
    groups = []
    for component in components:
        parts = []
        offset = component['position_mm'] if world_coordinates else [0, 0, 0]
        for primitive in component['primitives']:
            parts.append(_primitive_solid(primitive, offset))
        # Each component remains a named item. Fuse its intersecting/touching
        # solids when possible; leave disconnected shapes together as a compound.
        if len(parts) == 1:
            body = parts[0]
        else:
            try:
                body = parts[0].fuse(*parts[1:])
                if body is None or not body.is_valid or not body.solids():
                    raise ValueError('Invalid boolean result')
            except Exception:
                body = Compound(children=parts)
        body.label = component['name']
        groups.append(body)
    assembly = Compound(children=groups, label='AutoLab concept')
    with TemporaryDirectory(prefix='autolab-concept-step-') as temporary:
        path = Path(temporary) / 'concept.step'
        if not export_step(assembly, path, unit=Unit.MM):
            raise RuntimeError('The STEP model could not be exported.')
        return path.read_bytes()

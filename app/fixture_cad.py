"""Parametric OpenCASCADE / build123d CAD generators for standalone fixtures.

Per-part STL and STEP exports use CAD Z-up build-plate coordinates so each part sits flat at Z=0
with zero required supports. The assembled compound in assembly.step positions parts in world
CAD coordinates using app(x, y, z) -> CAD(x, z, y).
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

from app.fixture_adapter import (
    CAMERA_MOUNT_SPECS,
    FASTENER_HOLE_DIAMETERS_MM,
    FLANGE_EAR_DEPTH_MM,
    FLANGE_EAR_WIDTH_MM,
    MAST_CROSS_SECTION_MM,
    MAST_PAD_DEPTH_MM,
    SCALE_SHELF_DEPTH_MM,
)

_CAD_LOCK = Lock()
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _safe_fillet_edges(solid, edges, radius: float):
    if radius <= 0.05 or not edges:
        return solid
    for scale in (1.0, 0.5):
        r = round(radius * scale, 3)
        if r <= 0.05:
            continue
        try:
            candidate = solid.fillet(r, edges)
            if candidate is not None and candidate.is_valid and len(candidate.solids()) == 1:
                return candidate
        except Exception:
            continue
    return solid


def _safe_chamfer_edges(solid, edges, length: float):
    if length <= 0.05 or not edges:
        return solid
    for scale in (1.0, 0.5):
        d = round(length * scale, 3)
        if d <= 0.05:
            continue
        try:
            candidate = solid.chamfer(d, edges)
            if candidate is not None and candidate.is_valid and len(candidate.solids()) == 1:
                return candidate
        except Exception:
            continue
    return solid


def _bbox_dict(solid) -> dict[str, list[float]]:
    bb = solid.bounding_box()
    return {
        "min": [round(float(bb.min.X), 3), round(float(bb.min.Y), 3), round(float(bb.min.Z), 3)],
        "max": [round(float(bb.max.X), 3), round(float(bb.max.Y), 3), round(float(bb.max.Z), 3)],
        "size": [
            round(float(bb.size.X), 3),
            round(float(bb.size.Y), 3),
            round(float(bb.size.Z), 3),
        ],
    }


def _vertical_corner_edges(solid, height_mm: float):
    out = []
    for edge in solid.edges():
        try:
            p0, p1 = edge.position_at(0), edge.position_at(1)
            dz = abs(float(p1.Z) - float(p0.Z))
            dxy = ((float(p1.X) - float(p0.X)) ** 2 + (float(p1.Y) - float(p0.Y)) ** 2) ** 0.5
            if dz > height_mm * 0.7 and dxy < 0.05:
                out.append(edge)
        except Exception:
            continue
    return out


def build_locating_base_solid(fixture: dict[str, Any]):
    """Build the single-solid printable locating base (Z=0 build plate, pocket opening at +Z)."""
    from build123d import Align, Box, Cylinder, Pos

    spec = fixture["spec"]
    derived = fixture["derived"]
    floor = float(spec["floor_thickness_mm"])
    lip = float(spec["lip_height_mm"])
    wall = float(spec["wall_thickness_mm"])
    base_h = floor + lip
    core_w = float(derived["core_width_mm"])
    core_d = float(derived["core_depth_mm"])
    pocket_l = float(derived["pocket_length_mm"])
    pocket_w = float(derived["pocket_width_mm"])
    pocket_d = derived.get("pocket_diameter_mm")

    align_bottom = (Align.CENTER, Align.CENTER, Align.MIN)
    solid = Box(core_w, core_d, base_h, align=align_bottom)

    fillet_r = min(float(spec.get("fillet_mm", 0.0)), wall * 0.45)
    if fillet_r > 0.05:
        solid = _safe_fillet_edges(solid, _vertical_corner_edges(solid, base_h), fillet_r)

    # Side mounting flange tabs (1 mm overlap inside core wall to guarantee clean boolean fuse).
    if spec.get("fastener_spec", "none") != "none":
        ear_h = min(base_h, floor + 2.5)
        for sign in (-1.0, 1.0):
            ear_center_x = sign * (core_w / 2.0 + FLANGE_EAR_WIDTH_MM / 2.0 - 0.5)
            ear = Pos(ear_center_x, 0.0, 0.0) * Box(
                FLANGE_EAR_WIDTH_MM + 1.0, FLANGE_EAR_DEPTH_MM, ear_h, align=align_bottom
            )
            solid = solid.fuse(ear)

    # Co-planar 50 mm front calibration scale shelf at +Y in CAD coordinates (= +Z in app).
    if spec.get("include_scale_reference"):
        scale_w = min(core_w, float(derived["scale_reference_length_mm"]) + 16.0)
        shelf_y_center = core_d / 2.0 + (SCALE_SHELF_DEPTH_MM - 1.0) / 2.0
        shelf = Pos(0.0, shelf_y_center, 0.0) * Box(
            scale_w, SCALE_SHELF_DEPTH_MM + 1.0, base_h, align=align_bottom
        )
        solid = solid.fuse(shelf)
        # Engrave clean 10 mm tick marks along top of scale shelf (zero overhang).
        tick_span = float(derived["scale_reference_length_mm"])
        tick_count = max(2, int(round(tick_span / 10.0)) + 1)
        tick_y = core_d / 2.0 + SCALE_SHELF_DEPTH_MM / 2.0
        for idx in range(tick_count):
            tx = -tick_span / 2.0 + idx * (tick_span / (tick_count - 1))
            notch_h = 0.6
            tick = Pos(tx, tick_y, base_h - notch_h) * Box(
                0.8, 5.0, notch_h + 0.5, align=align_bottom
            )
            solid = solid - tick

    # Rear camera-mast mounting pad at -Y in CAD coordinates (= -Z in app).
    if spec.get("include_camera_mount"):
        mast_y = float(derived["mast_center_z_mm"])
        pad_rear_y = mast_y - MAST_PAD_DEPTH_MM / 2.0
        pad_front_y = -core_d / 2.0 + 1.0
        pad_len = pad_front_y - pad_rear_y
        pad_w = MAST_CROSS_SECTION_MM[0] + 12.0
        pad = Pos(0.0, (pad_rear_y + pad_front_y) / 2.0, 0.0) * Box(
            pad_w, pad_len, base_h, align=align_bottom
        )
        solid = solid.fuse(pad)

    # Cut recessed specimen pocket from Z = floor upward past Z = base_h.
    if spec["object_shape"] == "rectangular":
        pocket = Pos(0.0, 0.0, floor) * Box(
            pocket_l, pocket_w, lip + 2.0, align=align_bottom
        )
        solid = solid - pocket
        relief_r = min(1.2, max(0.5, wall * 0.35))
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                rx = sx * (pocket_l / 2.0 - 0.3)
                ry = sy * (pocket_w / 2.0 - 0.3)
                relief = Pos(rx, ry, floor) * Cylinder(
                    relief_r, lip + 2.0, align=align_bottom
                )
                solid = solid - relief
    else:
        pocket = Pos(0.0, 0.0, floor) * Cylinder(
            float(pocket_d) / 2.0, lip + 2.0, align=align_bottom
        )
        solid = solid - pocket

    # Asymmetric A1 tactile datum notch on top rim wall near (-X, +Y).
    if spec.get("include_alignment_stops"):
        notch = Pos(-(pocket_l / 2.0 + wall * 0.5), pocket_w / 2.0 - 2.0, base_h - 0.8) * Box(
            max(1.2, wall * 0.55), 5.0, 1.4, align=align_bottom
        )
        solid = solid - notch

    # Opposing finger slots along Y walls (front when no scale shelf, or left/right upper lip)
    # kept strictly inside core walls so they never intersect flange mounting holes.
    if spec.get("include_finger_cutouts"):
        cut_span = min(18.0, pocket_w * 0.45, pocket_l * 0.45)
        cut_depth = max(0.8, min(lip * 0.55, base_h - floor - 0.5))
        if cut_span >= 4.0 and cut_depth >= 0.8:
            z_cut = base_h - cut_depth
            for sign in (-1.0, 1.0):
                # Place left/right finger notches near Y = +pocket_w * 0.25 if circular/rect,
                # cleanly away from centered Y=0 bench flange ears.
                fy = min(pocket_w * 0.22, core_d * 0.28) if pocket_w > 34.0 else 0.0
                slot = Pos(sign * (pocket_l / 2.0 + wall / 2.0), fy, z_cut) * Box(
                    wall + 1.5, cut_span, cut_depth + 1.0, align=align_bottom
                )
                solid = solid - slot

    # Fastener through-holes and rear mast bolt bores.
    hole_dia = FASTENER_HOLE_DIAMETERS_MM.get(spec.get("fastener_spec", "none"))
    if hole_dia is not None:
        for hole in derived.get("mounting_holes", []):
            bore = Pos(float(hole["x_mm"]), float(hole["z_mm"]), -1.0) * Cylinder(
                hole_dia / 2.0, base_h + 3.0, align=align_bottom
            )
            solid = solid - bore
        if spec.get("include_camera_mount"):
            mast_y = float(derived["mast_center_z_mm"])
            for sx in (-9.0, 9.0):
                bore = Pos(sx, mast_y, -1.0) * Cylinder(
                    hole_dia / 2.0, base_h + 3.0, align=align_bottom
                )
                solid = solid - bore

    chamfer_len = min(float(spec.get("chamfer_mm", 0.0)), wall * 0.25, lip * 0.25)
    if chamfer_len > 0.05:
        outer_top_edges = [
            e
            for e in solid.edges()
            if abs(float(e.center().Z) - base_h) < 0.05
            and (
                abs(float(e.center().X)) > core_w * 0.45
                or abs(float(e.center().Y)) > core_d * 0.45
            )
        ]
        solid = _safe_chamfer_edges(solid, outer_top_edges[:8], chamfer_len)

    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError("Locating base failed to form a single valid CAD solid.")
    solid.label = "locating-base"
    return solid


def build_camera_mast_solid(fixture: dict[str, Any]):
    """Build the flat-printing overhead camera mast (Z=0 build plate, Y=height)."""
    from build123d import Align, Box, Cylinder, Pos

    spec = fixture["spec"]
    derived = fixture["derived"]
    mast_h = float(derived["mast_height_mm"])
    mast_w, mast_d = MAST_CROSS_SECTION_MM
    align_bottom = (Align.CENTER, Align.CENTER, Align.MIN)

    solid = Box(mast_w, mast_h, mast_d, align=align_bottom)

    # Relieve two end mounting pockets so bolts enter flush tongues inside [mast_w, mast_h, mast_d].
    pocket_len = 16.0
    tongue_z = 6.0
    for sy in (-1.0, 1.0):
        cut_y = sy * (mast_h / 2.0 - pocket_len / 2.0)
        recess = Pos(0.0, cut_y, tongue_z) * Box(
            mast_w + 2.0, pocket_len + 1.0, mast_d - tongue_z + 1.0, align=align_bottom
        )
        solid = solid - recess

    # Lighten central spine cleanly without weakening stiffness.
    spine_window_len = mast_h - 48.0
    if spine_window_len > 20.0:
        channel = Pos(0.0, 0.0, 5.0) * Box(
            12.0, spine_window_len, mast_d - 4.0, align=align_bottom
        )
        solid = solid - channel

    hole_dia = FASTENER_HOLE_DIAMETERS_MM.get(spec.get("fastener_spec", "m3_heatset_insert")) or 3.4
    # Through-holes on end tongues fit M3 bolts into base and arm (3.4 mm clearance on mast).
    bolt_dia = min(hole_dia, 3.6) if spec.get("fastener_spec") == "m3_heatset_insert" else hole_dia
    for sy in (-1.0, 1.0):
        hy = sy * (mast_h / 2.0 - 9.0)
        for sx in (-9.0, 9.0):
            bore = Pos(sx, hy, -1.0) * Cylinder(
                bolt_dia / 2.0, tongue_z + 2.0, align=align_bottom
            )
            solid = solid - bore

    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError("Camera mast failed to form a single valid CAD solid.")
    solid.label = "camera-mast"
    return solid


def build_camera_arm_solid(fixture: dict[str, Any]):
    """Build the cantilevered camera arm centered on its print envelope at Z=0."""
    from build123d import Align, Box, Cylinder, Pos

    spec = fixture["spec"]
    derived = fixture["derived"]
    cradle = CAMERA_MOUNT_SPECS[spec["camera_mount_type"]]
    arm_len = float(derived["arm_length_mm"])
    arm_w = float(derived["arm_width_mm"])
    arm_cz = float(derived["arm_center_z_mm"])
    mast_y_local = float(derived["mast_center_z_mm"]) - arm_cz
    cam_y_local = -arm_cz
    thick = 8.0
    align_bottom = (Align.CENTER, Align.CENTER, Align.MIN)

    beam_w = min(arm_w, MAST_CROSS_SECTION_MM[0] + 4.0)
    solid = Box(beam_w, arm_len, thick, align=align_bottom)
    if float(cradle["cradle_width_mm"]) > beam_w - 0.5:
        head = Pos(0.0, cam_y_local, 0.0) * Box(
            float(cradle["cradle_width_mm"]),
            float(cradle["cradle_depth_mm"]),
            thick,
            align=align_bottom,
        )
        solid = solid.fuse(head)

    # Mast attachment bolt holes.
    mast_dia = FASTENER_HOLE_DIAMETERS_MM.get(spec.get("fastener_spec", "m3_heatset_insert")) or 3.4
    for sx in (-9.0, 9.0):
        bore = Pos(sx, mast_y_local, -1.0) * Cylinder(
            mast_dia / 2.0, thick + 2.0, align=align_bottom
        )
        solid = solid - bore

    # Central optical lens bore & PCB pattern around (0, cam_y_local).
    lens_dia = float(cradle.get("lens_aperture_mm") or 0.0)
    if lens_dia > 0:
        lens_bore = Pos(0.0, cam_y_local, -1.0) * Cylinder(
            lens_dia / 2.0, thick + 2.0, align=align_bottom
        )
        solid = solid - lens_bore

    pattern = cradle["pattern"]
    pcb_dia = float(cradle["mounting_hole_diameter_mm"])
    dx = float(cradle["mounting_hole_dx_mm"]) / 2.0
    dz = float(cradle["mounting_hole_dz_mm"]) / 2.0
    if pattern == "4_corners":
        hole_pts = [
            (-dx, cam_y_local - dz),
            (dx, cam_y_local - dz),
            (-dx, cam_y_local + dz),
            (dx, cam_y_local + dz),
        ]
    elif pattern == "2_inline_x":
        hole_pts = [(-dx, cam_y_local), (dx, cam_y_local)]
    else:
        hole_pts = [(0.0, cam_y_local)]
    for hx, hy in hole_pts:
        bore = Pos(hx, hy, -1.0) * Cylinder(pcb_dia / 2.0, thick + 2.0, align=align_bottom)
        solid = solid - bore

    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError("Camera arm failed to form a single valid CAD solid.")
    solid.label = "camera-arm"
    return solid


def build_tolerance_coupon_solid(fixture: dict[str, Any]):
    """Build a 3-step FDM fit test coupon testing [-0.15, 0.00, +0.15] mm offsets."""
    from build123d import Align, Box, Pos

    spec = fixture["spec"]
    derived = fixture["derived"]
    coupon_meta = derived["tolerance_coupon"]
    coupon_part = next(p for p in fixture["parts"] if p["id"] == "tolerance-coupon")
    coupon_w, coupon_h, coupon_d = [float(v) for v in coupon_part["size_mm"]]
    floor = float(spec["floor_thickness_mm"])
    align_bottom = (Align.CENTER, Align.CENTER, Align.MIN)

    solid = Box(coupon_w, coupon_d, coupon_h, align=align_bottom)
    slot_len_y = 10.0
    for idx, opening_x in enumerate(coupon_meta["step_openings_mm"]):
        y_center = (idx - 1) * 16.0
        pocket = Pos(0.0, y_center, floor) * Box(
            float(opening_x), slot_len_y, coupon_h - floor + 1.0, align=align_bottom
        )
        solid = solid - pocket
        # Tactile index notches on +X edge (1, 2, or 3 notches identifying offset step).
        for n in range(idx + 1):
            ny = y_center + (n - idx / 2.0) * 2.2
            tick = Pos(coupon_w / 2.0, ny, coupon_h - 1.2) * Box(
                2.0, 1.0, 1.5, align=align_bottom
            )
            solid = solid - tick

    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError("Tolerance coupon failed to form a single valid CAD solid.")
    solid.label = "tolerance-coupon"
    return solid


def build_print_ready_labware_rack_solid(
    spec: dict[str, Any], *, chamfer_mm: float = 0.6, fillet_mm: float = 1.0
):
    """Build a fused print-ready tube rack adding chamfers/fillets to the canonical tube rack."""
    from app.cad_exports import _build_solid
    from app.labware_geometry import compile_labware_rack

    compiled = compile_labware_rack(spec)
    comp = {
        "id": "tube-rack",
        "kind": "rack",
        "label": "Print-ready tube rack",
        "position": [0.0, 0.0, 0.0],
        "size": compiled["size_mm"],
        "geometry": compiled["geometry"],
        "fabrication_shape": "rim_supported_tube_rack",
    }
    solid = _build_solid(comp)
    h = float(compiled["size_mm"][1])
    if fillet_mm > 0.05:
        solid = _safe_fillet_edges(solid, _vertical_corner_edges(solid, h)[:4], fillet_mm)
    if chamfer_mm > 0.05:
        top_z = h / 2.0
        top_edges = [e for e in solid.edges() if abs(float(e.center().Z) - top_z) < 0.05]
        solid = _safe_chamfer_edges(solid, top_edges[:4], chamfer_mm)
    if solid is None or not solid.is_valid or len(solid.solids()) != 1:
        raise ValueError("Print-ready tube rack failed to form a single valid solid.")
    solid.label = "tube-rack-print-ready"
    return solid


def build_print_ready_microplate_holder_solid(
    *,
    clearance_per_side_mm: float = 0.40,
    fdm_undersize_mm: float = 0.30,
    wall_thickness_mm: float = 3.5,
    floor_thickness_mm: float = 3.0,
    lip_height_mm: float = 3.5,
    chamfer_mm: float = 0.6,
    fillet_mm: float = 1.0,
    fastener_spec: str = "m3_heatset_insert",
):
    """Build a print-ready SLAS microplate holder with reliefs, stops, and M3 flange ears."""
    from app.microplate_geometry import compile_print_ready_microplate_holder

    compiled = compile_print_ready_microplate_holder(
        clearance_per_side_mm=clearance_per_side_mm,
        fdm_undersize_mm=fdm_undersize_mm,
        wall_thickness_mm=wall_thickness_mm,
        floor_thickness_mm=floor_thickness_mm,
        lip_height_mm=lip_height_mm,
        chamfer_mm=chamfer_mm,
        fillet_mm=fillet_mm,
        fastener_spec=fastener_spec,
    )
    return build_locating_base_solid(compiled)


def _export_stl_step(solid, name: str, tmp: Path) -> tuple[bytes, bytes]:
    from build123d import Unit, export_step, export_stl

    stl_path = tmp / f"{name}.stl"
    step_path = tmp / f"{name}.step"
    export_stl(solid, str(stl_path))
    export_step(solid, str(step_path), unit=Unit.MM)
    return stl_path.read_bytes(), step_path.read_bytes()


def _world_positioned_assembly(fixture: dict[str, Any]):
    """Assemble locating-base, camera-mast, and camera-arm in world coordinates (mm)."""
    from build123d import Compound, Pos, Rot

    spec = fixture["spec"]
    derived = fixture["derived"]
    floor = float(spec["floor_thickness_mm"])
    lip = float(spec["lip_height_mm"])
    base_h = floor + lip

    base = build_locating_base_solid(fixture)
    children = [base]
    if spec.get("include_camera_mount"):
        mast = build_camera_mast_solid(fixture)
        mast_h = float(derived["mast_height_mm"])
        mast_y = float(derived["mast_center_z_mm"])
        # Rotate lying mast (-Y print bed) upright (+Z world vertical) onto rear pad top.
        mast_world = Pos(0.0, mast_y, base_h + mast_h / 2.0) * (
            Rot(90, 0, 0) * Pos(0.0, 0.0, -MAST_CROSS_SECTION_MM[1] / 2.0) * mast
        )
        mast_world.label = "camera-mast"
        children.append(mast_world)

        arm = build_camera_arm_solid(fixture)
        arm_bottom_z = float(derived["camera_arm_bottom_y_mm"])
        arm_cy = float(derived["arm_center_z_mm"])
        arm_world = Pos(0.0, arm_cy, arm_bottom_z) * arm
        arm_world.label = "camera-arm"
        children.append(arm_world)

    return Compound(children=children, label=spec["title"])


def _fixture_python_source(fixture: dict[str, Any]) -> str:
    payload = json.dumps(fixture["spec"], indent=2, sort_keys=True)
    return (
        '"""Self-contained AutoLab standalone fixture reproduction script.\n'
        'Requires AutoLab / build123d==0.13.0.\n'
        '"""\n\n'
        "import json\n"
        "from pathlib import Path\n\n"
        "from app.fixture_adapter import compile_standalone_fixture\n"
        "from app.fixture_cad import build_fixture_cad_files\n\n"
        f"SPEC = json.loads({payload!r})\n\n"
        'if __name__ == "__main__":\n'
        "    compiled = compile_standalone_fixture(SPEC)\n"
        "    files = build_fixture_cad_files(compiled)\n"
        '    out = Path("autolab-fixture-cad")\n'
        "    for rel, content in files.items():\n"
        "        target = out / rel\n"
        "        target.parent.mkdir(parents=True, exist_ok=True)\n"
        "        target.write_bytes(content)\n"
        '    print(f"Exported {len(files)} fixture CAD files to {out}")\n'
    )


def build_fixture_cad_files(
    fixture: dict[str, Any], selected_parts: list[str] | None = None
) -> dict[str, bytes]:
    """Build print-ready STEP/STL CAD deliverables for requested parts of a fixture."""
    from build123d import Unit, export_step

    available_ids = [part["id"] for part in fixture["parts"]]
    target_ids = (
        [pid for pid in available_ids if pid in set(selected_parts)]
        if selected_parts is not None
        else list(available_ids)
    )
    if not target_ids:
        raise ValueError("Select at least one valid fixture part to export.")

    builders = {
        "locating-base": build_locating_base_solid,
        "camera-mast": build_camera_mast_solid,
        "camera-arm": build_camera_arm_solid,
        "tolerance-coupon": build_tolerance_coupon_solid,
    }
    structural_ids = [pid for pid in available_ids if pid != "tolerance-coupon"]
    include_assembly = all(pid in target_ids for pid in structural_ids)

    files: dict[str, bytes] = {}
    report_parts: list[dict[str, Any]] = []
    part_lookup = {part["id"]: deepcopy(part) for part in fixture["parts"]}

    with _CAD_LOCK, TemporaryDirectory(prefix="autolab-fixture-") as tmp_dir:
        tmp = Path(tmp_dir)
        for pid in target_ids:
            solid = builders[pid](fixture)
            stl_bytes, step_bytes = _export_stl_step(solid, pid, tmp)
            if not stl_bytes or not step_bytes:
                raise ValueError(f"Empty CAD output generated for '{pid}'.")
            files[f"parts/{pid}.stl"] = stl_bytes
            files[f"parts/{pid}.step"] = step_bytes
            meta = part_lookup[pid]
            report_parts.append(
                {
                    "id": pid,
                    "label": meta["label"],
                    "bbox_cad": _bbox_dict(solid),
                    "volume_mm3": round(float(solid.volume), 3),
                    "solids": len(solid.solids()),
                    "valid": bool(solid.is_valid),
                    "print_orientation": meta["print_orientation"],
                    "supports_required": meta["supports_required"],
                    "material": meta["material"],
                    "nozzle_mm": meta["nozzle_mm"],
                    "layer_height_mm": meta["layer_height_mm"],
                    "part_signature": meta["part_signature"],
                    "printer_fit": meta["printer_fit"],
                }
            )

        if include_assembly:
            assembly = _world_positioned_assembly(fixture)
            asm_path = tmp / "assembly.step"
            export_step(assembly, str(asm_path), unit=Unit.MM)
            files["cad/assembly.step"] = asm_path.read_bytes()

    report = {
        "schema_version": "1",
        "kind": "standalone_fixture_cad_report",
        "units": "mm",
        "coordinate_convention": (
            "Printable parts lie flat on build plate (Z=0 up); "
            "assembly.step uses app(x,y,z)->CAD(x,z,y)."
        ),
        "selected_parts": target_ids,
        "assembly_included": include_assembly,
        "parts": report_parts,
        "spec": fixture["spec"],
        "derived": fixture["derived"],
        "source_sha256": _SOURCE_SHA256,
    }
    files["cad/fixture-geometry-report.json"] = json.dumps(report, indent=2).encode("utf-8")
    files["cad/fixture-model.py"] = _fixture_python_source(fixture).encode("utf-8")
    return files

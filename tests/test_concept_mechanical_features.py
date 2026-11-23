"""Tests for Task 3.1 rich mechanical feature vocabulary across compilation, STL/OBJ, and STEP."""

from __future__ import annotations

import math
import struct
from collections import Counter

import pytest
from pydantic import ValidationError

from app.concept_agent import EngineeringReply
from app.concept_cad import concept_step
from app.concept_design import compile_concept, concept_obj, concept_stl
from app.model_client import _strict_schema


def _walk_schema(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema(item)


def _mechanical_proposal():
    return {
        "title": "NEMA-17 Gantry Plate & T-Slot Rail",
        "overview": "Aluminum extrusion beam with bracket and machined motor mounting plate.",
        "components": [
            {
                "key": "beam",
                "name": "Gantry 2040 beam",
                "role": "Primary horizontal linear-rail structural beam.",
                "source_id": None,
                "assembly": "Mount 2040 beam horizontally across upright posts.",
                "check": "Verify rail flatness and slot alignment.",
                "primitives": [
                    {
                        "shape": "extrusion",
                        "profile": "2040",
                        "axis": "x",
                        "size_mm": [220, 40, 20],
                        "center_mm": [0, 20, 0],
                        "finish": "frame",
                    },
                    {
                        "shape": "extrusion",
                        "profile": "2020",
                        "axis": "y",
                        "size_mm": [20, 120, 20],
                        "center_mm": [-120, 60, 0],
                        "finish": "frame",
                    },
                ],
            },
            {
                "key": "motor_plate",
                "name": "NEMA-17 Adapter Plate",
                "role": "Mount stepper motor and optical sensor arm with adjustment slots.",
                "source_id": None,
                "assembly": (
                    "Fasten plate onto beam with M5 bolts and fit stepper onto NEMA-17 pattern."
                ),
                "check": "Verify counterbores, pilot pocket, and slot travel.",
                "parent_key": "beam",
                "placement_mm": [0, 40, 0],
                "primitives": [
                    {
                        "shape": "plate",
                        "size_mm": [90, 10, 80],
                        "center_mm": [0, 5, 0],
                        "finish": "steel",
                        "fillet_mm": 1.0,
                        "features": [
                            {
                                "kind": "hole_pattern",
                                "face": "+y",
                                "offset_mm": [0, 0],
                                "pattern_type": "rect",
                                "count_x": 2,
                                "count_y": 2,
                                "pitch_x_mm": 31.0,
                                "pitch_y_mm": 31.0,
                                "diameter_mm": 3.4,
                            },
                            {
                                "kind": "hole_pattern",
                                "face": "+y",
                                "offset_mm": [0, -25],
                                "pattern_type": "circle",
                                "count": 4,
                                "bolt_circle_diameter_mm": 16.0,
                                "diameter_mm": 2.6,
                            },
                            {
                                "kind": "pocket",
                                "face": "+y",
                                "offset_mm": [0, 0],
                                "diameter_mm": 22.4,
                                "depth_mm": 2.5,
                            },
                            {
                                "kind": "slot",
                                "face": "+y",
                                "offset_mm": [30, 0],
                                "width_mm": 5.5,
                                "length_mm": 18.0,
                                "slot_angle_deg": 90.0,
                            },
                        ],
                    },
                ],
            },
            {
                "key": "gusset_bracket",
                "name": "L Corner Bracket",
                "role": "Lock plate at 90 degrees onto extrusion channel.",
                "source_id": None,
                "assembly": "Secure bracket with M5 socket cap screws in counterbores.",
                "check": "Verify screw head clearance inside counterbore.",
                "parent_key": "beam",
                "placement_mm": [80, 40, 0],
                "primitives": [
                    {
                        "shape": "bracket",
                        "size_mm": [40, 40, 20],
                        "center_mm": [0, 20, 0],
                        "wall_thickness_mm": 5.0,
                        "finish": "accent",
                        "features": [
                            {
                                "kind": "counterbore",
                                "face": "+y",
                                "offset_mm": [8, 0],
                                "diameter_mm": 5.4,
                                "counterbore_diameter_mm": 9.5,
                                "counterbore_depth_mm": 2.2,
                            },
                            {
                                "kind": "through_hole",
                                "face": "-x",
                                "offset_mm": [0, 8],
                                "diameter_mm": 5.4,
                            },
                        ],
                    },
                ],
            },
            {
                "key": "tilted_arm",
                "name": "Angled Sensor Shaft",
                "role": "Hold angled calibration bar along X axis.",
                "source_id": None,
                "assembly": "Clamp tilted shaft at 45 degrees.",
                "check": "Confirm clearance around tilted cylinder.",
                "primitives": [
                    {
                        "shape": "cylinder",
                        "axis": "x",
                        "rotation_deg": [0, 0, 45],
                        "size_mm": [60, 12, 12],
                        "center_mm": [0, 36 * math.sqrt(2) / 2, 0],
                        "finish": "steel",
                    },
                ],
            },
        ],
        "steps": [
            {
                "name": "Inspect mounting geometry",
                "instruction": "Inspect hole patterns, slots, and extrusion T-slots.",
                "component_keys": ["beam", "motor_plate", "gusset_bracket", "tilted_arm"],
                "moving_key": None,
                "translation_mm": [0, 0, 0],
            }
        ],
        "software": {
            "controller": "Select stepper controller after bench test.",
            "interfaces": ["Step/dir driver"],
            "setup": ["Verify M3 and M5 fastener clearances on prototype."],
            "checks": ["Verify linear rail travel."],
        },
        "open_questions": [],
    }


def _stl_triangles(content: bytes):
    (count,) = struct.unpack_from("<I", content, 80)
    assert len(content) == 84 + count * 50
    for i in range(count):
        vals = struct.unpack_from("<12fH", content, 84 + i * 50)
        yield vals[:3], [vals[3:6], vals[6:9], vals[9:12]]


def test_expanded_engineering_reply_strict_schema_is_openai_valid():
    schema = _strict_schema(EngineeringReply)
    for node in _walk_schema(schema):
        assert "default" not in node
        if "properties" in node:
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"])
    shape_enum = schema["$defs"]["ConceptPrimitive"]["properties"]["shape"]["enum"]
    assert {"box", "cylinder", "sphere", "extrusion", "bracket", "plate"} <= set(shape_enum)


def test_mechanical_feature_validation_rejects_invalid_combinations():
    bad_ext = _mechanical_proposal()
    bad_ext["components"][0]["primitives"][0]["size_mm"] = [220, 30, 30]
    with pytest.raises(ValidationError, match="Profile 2040"):
        compile_concept(bad_ext)

    bad_cb = _mechanical_proposal()
    bad_cb["components"][2]["primitives"][0]["features"][0]["counterbore_diameter_mm"] = 4.0
    with pytest.raises(ValidationError, match="exceed diameter_mm"):
        compile_concept(bad_cb)

    bad_both = _mechanical_proposal()
    bad_both["components"][1]["primitives"][0]["chamfer_mm"] = 1.0
    with pytest.raises(ValidationError, match="not both"):
        compile_concept(bad_both)


@pytest.mark.parametrize(
    "shape,extra,size",
    [
        ("plate", {}, [40, 10, 30]),
        ("bracket", {"wall_thickness_mm": 5.0}, [40, 40, 20]),
        ("extrusion", {"profile": "2020", "axis": "y"}, [20, 100, 20]),
        ("extrusion", {"profile": "2040", "axis": "x"}, [120, 40, 20]),
        ("extrusion", {"profile": "4040", "axis": "z"}, [40, 40, 80]),
        ("cylinder", {"axis": "x"}, [50, 16, 16]),
        ("cylinder", {"axis": "z"}, [16, 16, 50]),
    ],
)
def test_new_primitive_stl_meshes_are_watertight_two_manifold(shape, extra, size):
    p = {
        "title": "Single shape check",
        "overview": "Verify STL watertightness.",
        "components": [
            {
                "key": "part",
                "name": "Test part",
                "role": "Shape check.",
                "source_id": None,
                "assembly": "Inspect shape.",
                "check": "Verify bounds.",
                "primitives": [
                    {
                        "shape": shape,
                        "size_mm": size,
                        "center_mm": [0, size[1] / 2.0, 0],
                        "finish": "frame",
                        **extra,
                    }
                ],
            }
        ],
        "steps": [
            {
                "name": "Check",
                "instruction": "Static check.",
                "component_keys": ["part"],
                "moving_key": None,
                "translation_mm": [0, 0, 0],
            }
        ],
        "software": {
            "controller": "None.",
            "interfaces": [],
            "setup": ["Inspect."],
            "checks": ["Done."],
        },
        "open_questions": [],
    }
    design = compile_concept(p)
    stl_bytes = concept_stl(design, "part")
    edges: Counter = Counter()
    volume = 0.0
    all_pts = []
    for normal, pts in _stl_triangles(stl_bytes):
        assert math.isclose(sum(n * n for n in normal), 1.0, abs_tol=1e-5)
        all_pts.extend(pts)
        a, b, c = pts
        volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
        for v1, v2 in zip(pts, pts[1:] + pts[:1]):
            edges[tuple(sorted((v1, v2)))] += 1
    assert set(edges.values()) == {2}
    assert volume > 0
    for idx in range(3):
        span = max(pt[idx] for pt in all_pts) - min(pt[idx] for pt in all_pts)
        assert span == pytest.approx(size[idx], abs=1e-4)
    assert b"o part_1" in concept_obj(design, "part")


def test_rotated_primitive_bounds_expand_accurately():
    expected_xy = 40.0 * math.cos(math.pi / 4) + 10.0 * math.sin(math.pi / 4)
    p = {
        "title": "Rotated bar",
        "overview": "Verify rotated AABB calculation.",
        "components": [
            {
                "key": "bar",
                "name": "Rotated bar",
                "role": "Hold angled bar.",
                "source_id": None,
                "assembly": "Mount bar.",
                "check": "Check envelope.",
                "primitives": [
                    {
                        "shape": "cylinder",
                        "axis": "x",
                        "rotation_deg": [0, 0, 45],
                        "size_mm": [40, 10, 10],
                        "center_mm": [0, expected_xy / 2.0, 0],
                        "finish": "steel",
                    }
                ],
            }
        ],
        "steps": [
            {
                "name": "Static",
                "instruction": "Check.",
                "component_keys": ["bar"],
                "moving_key": None,
                "translation_mm": [0, 0, 0],
            }
        ],
        "software": {"controller": "N/A", "interfaces": [], "setup": ["Check"], "checks": ["OK"]},
        "open_questions": [],
    }
    design = compile_concept(p)
    comp = design["components"][0]
    assert comp["size_mm"][0] == pytest.approx(expected_xy, abs=1e-4)
    assert comp["size_mm"][1] == pytest.approx(expected_xy, abs=1e-4)
    assert comp["size_mm"][2] == pytest.approx(10.0, abs=1e-4)


def test_step_export_probes_real_holes_counterbores_pockets_slots_and_extrusions(tmp_path):
    build123d = pytest.importorskip("build123d")
    Align = build123d.Align
    Cylinder = build123d.Cylinder
    Pos = build123d.Pos
    Rot = build123d.Rot
    import_step = build123d.import_step

    design = compile_concept(_mechanical_proposal())

    # 1. Plate features: rect hole pattern, circular bolt pattern, pocket, slot, fillet.
    plate_step = tmp_path / "motor_plate.step"
    plate_step.write_bytes(concept_step(design, "motor_plate"))
    plate = import_step(str(plate_step))
    assert plate.is_valid
    assert plate.volume < 90 * 10 * 80

    # Probe NEMA-17 31x31 through-hole at (+15.5, 5, +15.5).
    nema_probe = (
        Pos(15.5, 5.0, 15.5) * Rot(90, 0, 0) * Cylinder(1.2, 10.0, align=(Align.CENTER,) * 3)
    )
    hit = plate & nema_probe
    assert hit is None or hit.volume == pytest.approx(0.0, abs=1e-6)

    # Probe blind pocket (depth 2.5 mm from +Y=10 -> void down to Y=7.5, solid below Y=7.5).
    pocket_void = (
        Pos(0.0, 9.0, 0.0) * Rot(90, 0, 0) * Cylinder(8.0, 1.8, align=(Align.CENTER,) * 3)
    )
    pocket_floor = (
        Pos(0.0, 4.0, 0.0) * Rot(90, 0, 0) * Cylinder(8.0, 4.0, align=(Align.CENTER,) * 3)
    )
    void_hit = plate & pocket_void
    assert void_hit is None or void_hit.volume == pytest.approx(0.0, abs=1e-6)
    assert (plate & pocket_floor).volume > 500.0

    # Probe rotated slot at (+30, 5, 0) length 18 mm along Z (slot_angle_deg=90).
    slot_probe = (
        Pos(30.0, 5.0, 6.0) * Rot(90, 0, 0) * Cylinder(1.5, 10.0, align=(Align.CENTER,) * 3)
    )
    slot_hit = plate & slot_probe
    assert slot_hit is None or slot_hit.volume == pytest.approx(0.0, abs=1e-6)

    # 2. Bracket counterbore on horizontal leg (+y face at inset step) & side wall through-hole.
    bracket_step = tmp_path / "bracket.step"
    bracket_step.write_bytes(concept_step(design, "gusset_bracket"))
    bracket = import_step(str(bracket_step))
    assert bracket.is_valid
    # Horizontal leg spans X [-20..+20], Y [0..5], Z [-10..+10]; counterbore at u=+8, v=0.
    cb_clearance = (
        Pos(8.0, 4.2, 0.0) * Rot(90, 0, 0) * Cylinder(4.3, 1.4, align=(Align.CENTER,) * 3)
    )
    cb_hit = bracket & cb_clearance
    assert cb_hit is None or cb_hit.volume == pytest.approx(0.0, abs=1e-6)
    # Shoulder floor exists between bore r=2.7 and cb r=4.75 below counterbore depth (Y=0..2.8).
    cb_seat = Pos(8.0, 1.4, 0.0) * Rot(90, 0, 0) * Cylinder(4.0, 2.0, align=(Align.CENTER,) * 3)
    assert (bracket & cb_seat).volume > 30.0

    # 3. Extrusion center bore & full assembly export.
    beam_step = tmp_path / "beam.step"
    beam_step.write_bytes(concept_step(design, "beam"))
    beam = import_step(str(beam_step))
    assert beam.is_valid
    bore_probe = (
        Pos(0.0, 20.0, 0.0) * Rot(0, 90, 0) * Cylinder(2.0, 200.0, align=(Align.CENTER,) * 3)
    )
    bore_hit = beam & bore_probe
    assert bore_hit is None or bore_hit.volume == pytest.approx(0.0, abs=1e-6)

    full_step = tmp_path / "assembly.step"
    full_step.write_bytes(concept_step(design))
    assembly = import_step(str(full_step))
    assert assembly.is_valid
    assert len(assembly.solids()) >= 4

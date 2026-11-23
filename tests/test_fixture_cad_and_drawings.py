"""OpenCASCADE STEP/STL, 2D SVG/PDF drawing, and package tests (Phase 2.2)."""

import hashlib
import io
import json
import zipfile

import pytest
from build123d import Align, Box, Cylinder, Pos, import_step

from app.domain import apply_update, new_project
from app.fixture_adapter import attach_standalone_fixture, compile_standalone_fixture
from app.fixture_cad import (
    build_fixture_cad_files,
    build_print_ready_labware_rack_solid,
    build_print_ready_microplate_holder_solid,
)
from app.fixture_drawings import (
    build_fixture_package_zip,
    render_bench_build_sheet_html,
    render_bench_build_sheet_md,
    render_e13_checklist_md,
    render_fixture_drawing_pdf,
    render_fixture_drawing_svg,
    render_labware_drawing_pdf,
    render_labware_drawing_svg,
)
from app.lab_profile import PRINTER_PRESETS, default_lab_profile
from app.labware_geometry import compile_labware_rack, labware_print_sheet
from app.microplate_geometry import PROFILE_ID, compile_microplate


def _bambu_mini() -> dict:
    mini = next(p for p in PRINTER_PRESETS if p["id"] == "bambu-a1-mini")
    prof = default_lab_profile()
    prof["printer"] = {
        "name": mini["name"],
        "build_volume_mm": list(mini["build_volume_mm"]),
        "nozzle_mm": mini["nozzle_mm"],
        "materials": list(mini["materials"]),
    }
    return prof


def test_reopened_imaging_rig_steps_are_single_valid_solids_with_clear_pocket(tmp_path):
    profile = _bambu_mini()
    fixture = compile_standalone_fixture(
        {
            "title": "SLAS 96 imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        },
        lab_profile=profile,
    )
    files = build_fixture_cad_files(fixture)
    expected_keys = {
        "parts/locating-base.stl",
        "parts/locating-base.step",
        "parts/camera-mast.stl",
        "parts/camera-mast.step",
        "parts/camera-arm.stl",
        "parts/camera-arm.step",
        "parts/tolerance-coupon.stl",
        "parts/tolerance-coupon.step",
        "cad/assembly.step",
        "cad/fixture-geometry-report.json",
        "cad/fixture-model.py",
    }
    assert expected_keys.issubset(files)

    report = json.loads(files["cad/fixture-geometry-report.json"])
    assert len(report["parts"]) == 4

    for pid in ("locating-base", "camera-mast", "camera-arm", "tolerance-coupon"):
        step_path = tmp_path / f"{pid}.step"
        step_path.write_bytes(files[f"parts/{pid}.step"])
        solid = import_step(str(step_path))
        assert solid.is_valid
        assert len(solid.solids()) == 1
        meta = next(p for p in fixture["parts"] if p["id"] == pid)
        bb = solid.bounding_box().size
        # App Y-up [w, h, d] maps to print-bed CAD Z-up [w, d, h].
        assert [bb.X, bb.Y, bb.Z] == pytest.approx(
            [meta["size_mm"][0], meta["size_mm"][2], meta["size_mm"][1]], abs=0.05
        )

    # Verify nominal plate fits inside locating-base pocket with zero collision above floor.
    base_solid = import_step(str(tmp_path / "locating-base.step"))
    gauge = Pos(0.0, 0.0, 3.5 + 2.0) * Box(
        127.76 + 0.6, 85.48 + 0.6, 3.8, align=(Align.CENTER,) * 3
    )
    overlap = base_solid & gauge
    assert overlap is None or overlap.volume == pytest.approx(0.0, abs=1e-6)

    asm_path = tmp_path / "assembly.step"
    asm_path.write_bytes(files["cad/assembly.step"])
    asm = import_step(str(asm_path))
    assert asm.is_valid
    assert len(asm.solids()) == 3

    partial = build_fixture_cad_files(fixture, selected_parts=["locating-base"])
    assert "cad/assembly.step" not in partial
    assert "parts/camera-mast.step" not in partial


def test_circular_petri_holder_step_clearance_and_print_ready_labware(tmp_path):
    circular = compile_standalone_fixture(
        {
            "title": "90mm Petri locating base",
            "fixture_kind": "locating_base",
            "object_reference_id": "petri_dish_90mm",
        },
        lab_profile=_bambu_mini(),
    )
    files = build_fixture_cad_files(circular, selected_parts=["locating-base"])
    assert "parts/tolerance-coupon.step" not in files
    assert "parts/camera-mast.step" not in files

    path = tmp_path / "petri.step"
    path.write_bytes(files["parts/locating-base.step"])
    solid = import_step(str(path))
    assert solid.is_valid and len(solid.solids()) == 1
    gauge = Pos(0.0, 0.0, 3.5 + 2.0) * Cylinder(
        (90.0 + 0.7) / 2.0, 3.8, align=(Align.CENTER,) * 3
    )
    overlap = solid & gauge
    assert overlap is None or overlap.volume == pytest.approx(0.0, abs=1e-6)

    # Print-ready upgrades produce single valid solids while leaving legacy defaults untouched.
    rack_spec = dict(
        body_diameter_max_mm=11,
        rim_diameter_min_mm=14,
        cap_diameter_max_mm=15,
        below_rim_height_mm=40,
        count=6,
        columns=3,
    )
    legacy_rack = compile_labware_rack(rack_spec)
    assert legacy_rack["derived"]["bore_diameter_mm"] == pytest.approx(12.1)
    sheet = labware_print_sheet(rack_spec, lab_profile=_bambu_mini())
    assert sheet["printer_fit"]["fits"] is True
    rack_solid = build_print_ready_labware_rack_solid(rack_spec)
    assert rack_solid.is_valid and len(rack_solid.solids()) == 1

    plate_solid = build_print_ready_microplate_holder_solid()
    assert plate_solid.is_valid and len(plate_solid.solids()) == 1
    assert compile_microplate(PROFILE_ID)["size_mm"] == pytest.approx([135.36, 16.72, 93.08])


def test_dimensioned_svg_pdf_selective_zip_and_labware_drawings():
    base = apply_update(new_project(), {"workflow_kind": "general"})
    project = attach_standalone_fixture(
        base,
        {
            "title": "Plate imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        },
        lab_profile=_bambu_mini(),
    )
    fixture = project["standalone_fixture"]

    svg_text = render_fixture_drawing_svg(fixture, project["revision"]).decode("utf-8")
    assert 'viewBox="0 0 842 595"' in svg_text
    assert "TOP VIEW" in svg_text and "FRONT ELEVATION" in svg_text
    assert "128.66" in svg_text
    assert "Nominal ref" in svg_text

    pdf = render_fixture_drawing_pdf(fixture, project["revision"])
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page ") == 2
    assert b"E13 REPEATABILITY TRIAL LOG" in pdf

    html_sheet = render_bench_build_sheet_html(
        fixture, project, project["revision"]
    ).decode("utf-8")
    assert "tolerance-coupon.stl" in html_sheet
    md_sheet = render_bench_build_sheet_md(fixture, project["revision"]).decode("utf-8")
    assert "128.66 x 86.38 mm" in md_sheet
    e13_md = render_e13_checklist_md(fixture, project["revision"]).decode("utf-8")
    assert "| 10 |" in e13_md

    raw_zip = build_fixture_package_zip(project, selected_parts=["locating-base"])
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        names = set(zf.namelist())
        assert "parts/locating-base.stl" in names
        assert "parts/locating-base.step" in names
        assert "parts/camera-mast.stl" not in names
        assert "drawing.svg" in names
        assert "drawing.pdf" in names
        assert "e13-bench-checklist.md" in names
        manifest = json.loads(zf.read("manifest.json"))
        for entry in manifest["files"]:
            assert hashlib.sha256(zf.read(entry["name"])).hexdigest() == entry["sha256"]

    # Labware SVG + PDF helper check
    plate = compile_microplate(PROFILE_ID)
    plate_project = {
        "revision": 3,
        "labware_plate": {
            "profile": plate["profile"],
            "derived": plate["derived"],
            "size_mm": plate["size_mm"],
        },
    }
    assert b"129.36 x 87.08" in render_labware_drawing_svg(plate_project)
    assert render_labware_drawing_pdf(plate_project).startswith(b"%PDF-1.4")

"""Unit and persistence tests for the standalone fixture adapter (Phase 2.1)."""

import json

import pytest

from app.domain import apply_update, new_project
from app.fixture_adapter import (
    attach_standalone_fixture,
    compile_standalone_fixture,
    list_fixture_references,
    normalize_fixture_spec,
)
from app.lab_profile import PRINTER_PRESETS, default_lab_profile
from app.project_storage import decode_snapshot, make_snapshot, restore_snapshot


def _bambu_mini_profile() -> dict:
    mini = next(p for p in PRINTER_PRESETS if p["id"] == "bambu-a1-mini")
    profile = default_lab_profile()
    profile["printer"] = {
        "name": mini["name"],
        "build_volume_mm": list(mini["build_volume_mm"]),
        "nozzle_mm": mini["nozzle_mm"],
        "materials": list(mini["materials"]),
    }
    return profile


def test_named_references_and_provenance_inference():
    refs = {r["id"]: r for r in list_fixture_references()}
    expected_ids = {
        "slas_96_well_plate",
        "petri_dish_90mm",
        "microscope_slide_75x25",
        "cuvette_12_5mm",
    }
    assert expected_ids == set(refs)

    spec_ref = normalize_fixture_spec(
        {
            "title": "Plate imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        }
    )
    assert spec_ref.object_shape == "rectangular"
    assert spec_ref.object_length_mm == pytest.approx(127.76)
    assert spec_ref.object_width_mm == pytest.approx(85.48)
    assert spec_ref.dimension_provenance == "reference"
    assert spec_ref.include_scale_reference is True
    assert spec_ref.include_camera_mount is True

    spec_custom = normalize_fixture_spec(
        {
            "title": "Measured slide holder",
            "fixture_kind": "locating_base",
            "object_label": "Coated glass slide",
            "object_shape": "rectangular",
            "object_length_mm": 76.2,
            "object_width_mm": 25.6,
            "object_height_mm": 1.2,
        }
    )
    assert spec_custom.dimension_provenance == "user_measured"


def test_imaging_rig_fits_bambu_mini_and_keeps_camera_signatures_isolated():
    profile = _bambu_mini_profile()
    nominal = compile_standalone_fixture(
        {
            "title": "96-well imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        },
        lab_profile=profile,
    )
    assert nominal["derived"]["pocket_length_mm"] == pytest.approx(128.66)
    assert nominal["derived"]["pocket_width_mm"] == pytest.approx(86.38)
    assert nominal["printer_fit_ok"] is True
    assert [p["id"] for p in nominal["parts"]] == [
        "locating-base",
        "camera-mast",
        "camera-arm",
        "tolerance-coupon",
    ]
    assert all(p["printer_fit"]["fits"] for p in nominal["parts"])

    # Adjusting FDM pocket allowance changes base & coupon signatures only.
    tighter = compile_standalone_fixture(
        {
            "title": "96-well imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
            "fdm_undersize_mm": 0.50,
        },
        lab_profile=profile,
    )
    nom_sigs = nominal["part_signatures"]
    tight_sigs = tighter["part_signatures"]
    assert tight_sigs["locating-base"] != nom_sigs["locating-base"]
    assert tight_sigs["tolerance-coupon"] != nom_sigs["tolerance-coupon"]
    assert tight_sigs["camera-mast"] == nom_sigs["camera-mast"]
    assert tight_sigs["camera-arm"] == nom_sigs["camera-arm"]


def test_circular_petri_holder_and_bed_overflow_and_bench_conflict():
    profile = _bambu_mini_profile()
    circular = compile_standalone_fixture(
        {
            "title": "90mm Petri imaging stand",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "petri_dish_90mm",
            "camera_working_distance_mm": 120.0,
        },
        lab_profile=profile,
    )
    assert circular["derived"]["pocket_diameter_mm"] == pytest.approx(90.9)
    assert circular["printer_fit_ok"] is True

    oversized = compile_standalone_fixture(
        {
            "title": "Tall macro rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
            "camera_working_distance_mm": 220.0,
        },
        lab_profile=profile,
    )
    assert oversized["printer_fit_ok"] is False
    mast = next(p for p in oversized["parts"] if p["id"] == "camera-mast")
    assert mast["printer_fit"]["fits"] is False
    assert "Exceeds" in mast["printer_fit"]["detail"]

    narrow_bench = apply_update(
        new_project(),
        {"workflow_kind": "general", "workspace_width_mm": 110, "workspace_depth_mm": 120},
    )
    updated = attach_standalone_fixture(
        narrow_bench,
        {
            "title": "Plate rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        },
        lab_profile=profile,
    )
    assert updated["design"]["constraint_status"] == "infeasible"
    assert updated["design"]["spatial_fit"]["subject"] == "standalone_fixture"
    assert updated["workflow"]["label"] == "Standalone printable fixture"


def test_attach_rejects_liquid_project_and_roundtrips_snapshot():
    liquid = apply_update(new_project(), {"workflow_kind": "liquid_preparation"})
    with pytest.raises(ValueError, match="new general project"):
        attach_standalone_fixture(
            liquid,
            {"title": "Plate rig", "object_reference_id": "slas_96_well_plate"},
        )

    base = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "workspace_width_mm": 300,
            "workspace_depth_mm": 260,
            "workspace_height_mm": 280,
        },
    )
    attached = attach_standalone_fixture(
        base,
        {
            "title": "Slide rig",
            "fixture_kind": "imaging_rig",
            "object_label": "Measured slide",
            "object_shape": "rectangular",
            "object_length_mm": 76.0,
            "object_width_mm": 25.4,
            "object_height_mm": 1.1,
        },
        lab_profile=_bambu_mini_profile(),
    )
    len_prov = attached["dimension_provenance"]["fixture_object_length_mm"]["provenance"]
    assert len_prov == "user_measured"
    assert attached["design"]["constraint_status"] == "fits"

    snap = make_snapshot(attached, [{"role": "user", "content": "Design slide fixture"}])
    restored, hist = restore_snapshot(decode_snapshot(json.dumps(snap).encode("utf-8")))
    assert restored["standalone_fixture"]["spec"] == attached["standalone_fixture"]["spec"]
    assert (
        restored["standalone_fixture"]["part_signatures"]
        == attached["standalone_fixture"]["part_signatures"]
    )
    assert restored["dimension_provenance"] == attached["dimension_provenance"]
    assert restored["design"]["constraint_status"] == "fits"
    assert len(hist) == 1

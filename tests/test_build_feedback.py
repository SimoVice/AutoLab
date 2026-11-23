"""Tests for the physical build feedback loop and selective reprint diff (Phase 2.3)."""

import json

import pytest

from app.build_feedback import append_build_log, apply_build_log_revision, compute_reprint_diff
from app.domain import apply_update, new_project
from app.fixture_adapter import attach_standalone_fixture
from app.lab_profile import PRINTER_PRESETS, default_lab_profile
from app.project_storage import decode_snapshot, make_snapshot, restore_snapshot


def _mini_profile() -> dict:
    mini = next(p for p in PRINTER_PRESETS if p["id"] == "bambu-a1-mini")
    prof = default_lab_profile()
    prof["printer"] = {
        "name": mini["name"],
        "build_volume_mm": list(mini["build_volume_mm"]),
        "nozzle_mm": mini["nozzle_mm"],
        "materials": list(mini["materials"]),
    }
    return prof


def _imaging_rig_project() -> dict:
    base = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "workspace_width_mm": 300,
            "workspace_depth_mm": 260,
            "workspace_height_mm": 280,
        },
    )
    return attach_standalone_fixture(
        base,
        {
            "title": "96-well imaging rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
        },
        lab_profile=_mini_profile(),
    )


def test_caliper_build_log_revision_reprints_only_changed_parts_and_survives_snapshot():
    project = _imaging_rig_project()
    assert project["standalone_fixture"]["derived"]["pocket_length_mm"] == pytest.approx(128.66)

    logged, entry = append_build_log(
        project,
        {
            "parts_printed": ["locating-base", "camera-mast", "camera-arm", "tolerance-coupon"],
            "printer_name": "Bambu Lab A1 mini",
            "material": "PLA",
            "layer_height_mm": 0.2,
            "fit_result": "too_tight",
            "measured_features": [
                {
                    "feature": "pocket_length",
                    "axis": "length",
                    "nominal_mm": 128.66,
                    "measured_mm": 128.35,
                }
            ],
            "notes": "Plate binds slightly on length axis; mast and arm fit bolts smoothly.",
        },
    )
    assert entry["id"] == "log-01-r2"
    assert entry["measured_features"][0]["error_mm"] == pytest.approx(-0.31)
    assert logged["build_feedback"]["has_untested_changes"] is False

    revised, summary = apply_build_log_revision(logged, entry["id"], lab_profile=_mini_profile())
    assert summary["new_revision"] == 3
    assert revised["standalone_fixture"]["spec"]["fdm_undersize_mm"] == pytest.approx(0.51)
    assert revised["standalone_fixture"]["derived"]["pocket_length_mm"] == pytest.approx(128.97)
    assert set(summary["parts_to_reprint"]) == {"locating-base", "tolerance-coupon"}
    assert set(summary["parts_unchanged"]) == {"camera-mast", "camera-arm"}
    assert "reuse existing print" in revised["build_feedback"]["parts_unchanged"][0]["reason"]
    assert len(revised["build_logs"]) == 1
    assert revised["build_logs"][0]["applied_revision"] == 3

    snap = make_snapshot(revised, [])
    restored, _ = restore_snapshot(decode_snapshot(json.dumps(snap).encode("utf-8")))
    assert len(restored["build_logs"]) == 1
    assert restored["build_logs"][0]["id"] == "log-01-r2"
    assert restored["standalone_fixture"]["spec"]["fdm_undersize_mm"] == pytest.approx(0.51)


def test_e13_10_placement_summary_pass_and_fail_thresholds():
    project = _imaging_rig_project()
    trials_pass = [
        {
            "trial": i,
            "seated_ok": True,
            "orientation_match": True,
            "displacement_mm": 0.45 if i < 10 else 0.90,
        }
        for i in range(1, 11)
    ]
    logged_pass, entry_pass = append_build_log(
        project,
        {
            "parts_printed": ["locating-base"],
            "fit_result": "pass",
            "placement_trials": trials_pass,
        },
    )
    assert entry_pass["e13_summary"]["passes_1mm_target"] is True
    assert entry_pass["e13_summary"]["max_displacement_mm"] == pytest.approx(0.90)

    trials_fail = list(trials_pass)
    trials_fail[4] = {
        "trial": 5,
        "seated_ok": True,
        "orientation_match": True,
        "displacement_mm": 1.35,
    }
    _, entry_fail = append_build_log(
        logged_pass,
        {
            "parts_printed": ["locating-base"],
            "fit_result": "pass",
            "placement_trials": trials_fail,
        },
    )
    assert entry_fail["e13_summary"]["passes_1mm_target"] is False
    assert entry_fail["e13_summary"]["max_displacement_mm"] == pytest.approx(1.35)


def test_qualitative_too_loose_fallback_and_empty_diff():
    project = _imaging_rig_project()
    assert compute_reprint_diff(project)["latest_log_id"] is None

    logged, entry = append_build_log(
        project,
        {"parts_printed": ["locating-base"], "fit_result": "too_loose"},
    )
    revised, _ = apply_build_log_revision(logged, entry["id"])
    assert revised["standalone_fixture"]["spec"]["clearance_per_side_mm"] == pytest.approx(0.25)

    with pytest.raises(ValueError, match="Unknown build log"):
        apply_build_log_revision(logged, "missing-log")

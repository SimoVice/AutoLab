"""Tests for full 3D X/Y/Z spatial checks, access clearances, and build-volume evaluation."""

from __future__ import annotations

import json

import pytest
from evolution_fixtures import inspection_proposal, layout_project
from foundation_fixtures import liquid_project, source_proposal
from test_commercial_design import proposal as ot2_proposal
from test_concept_design import proposal as concept_proposal
from test_microplates import select as select_plate

from app.bench_station import StationProposal, compile_station
from app.cold_transfer import apply_cold_transfer
from app.commercial_design import adopt_commercial, check_commercial_candidate
from app.concept_design import compile_concept
from app.design_spec import apply_candidate
from app.domain import apply_update, new_project
from app.foundation_design import adopt_foundation
from app.inspection import apply_extension, check_extension
from app.lab_profile import apply_profile_to_project
from app.model_client import ExtractedTurn
from app.project_storage import decode_snapshot, make_snapshot, restore_snapshot
from app.spatial_fit import evaluate_printable_fit, evaluate_spatial_fit


def _source_with_limits(**limits):
    base = adopt_foundation(apply_update(liquid_project(), limits))
    return apply_candidate(base, source_proposal(), {"model": "offline"})


def test_general_brief_keeps_workspace_limits_until_geometry_exists():
    project = apply_update(new_project(), {
        "workflow_kind": "general", "brief_objective": "Position an instrument above a sample.",
        "workspace_width_mm": 260, "workspace_height_mm": 150,
    })
    for state in (project, restore_snapshot(make_snapshot(project, []))[0]):
        fit = state["design"]["spatial_fit"]
        assert fit["subject"] == "unselected"
        assert fit["status"] == state["design"]["constraint_status"] == "awaiting_geometry"
        assert fit["axes"]["width_mm"]["limit_mm"] == 260
        assert fit["axes"]["height_mm"]["limit_mm"] == 150
        assert all(a["required_mm"] is None and a["fits"] is None for a in fit["axes"].values())
        assert "kinematics" not in fit
        assert not fit["violations"] and not fit["printable_checks"]
    platform = evaluate_spatial_fit(project, platform_id="opentrons-ot2")
    assert platform["subject"] == "opentrons-ot2"
    assert platform["status"] == "infeasible"


def test_ot2_3d_clearances_catch_depth_and_height_overflow():
    tight = apply_update(
        layout_project(),
        {
            "workspace_width_mm": 1200.0,
            "workspace_depth_mm": 700.0,
            "workspace_height_mm": 750.0,
        },
    )
    ot2 = adopt_commercial(tight)
    fit = ot2["design"]["spatial_fit"]
    assert fit["subject"] == "opentrons-ot2"
    assert fit["status"] == "infeasible"
    assert ot2["design"]["constraint_status"] == "infeasible"
    assert fit["axes"]["width_mm"]["required_mm"] == 1030.0
    assert fit["axes"]["width_mm"]["fits"] is True
    assert fit["axes"]["depth_mm"]["required_mm"] == 970.0
    assert fit["axes"]["depth_mm"]["fits"] is False
    assert fit["axes"]["depth_mm"]["margin_mm"] == -270.0
    assert fit["axes"]["height_mm"]["required_mm"] == 810.0
    assert fit["axes"]["height_mm"]["fits"] is False
    assert fit["axes"]["height_mm"]["margin_mm"] == -60.0

    by_axis = {v["axis"]: v for v in fit["violations"]}
    assert by_axis["depth_mm"]["overflow_mm"] == 270.0
    assert by_axis["height_mm"]["overflow_mm"] == 60.0

    failures = {
        c["id"] for c in check_commercial_candidate(ot2, ot2_proposal()) if not c["passed"]
    }
    assert {"workspace_depth", "workspace_height"} <= failures
    assert "workspace_width" not in failures

    roomy = adopt_commercial(
        apply_update(
            layout_project(),
            {
                "workspace_width_mm": 1030.0,
                "workspace_depth_mm": 970.0,
                "workspace_height_mm": 810.0,
            },
        )
    )
    assert roomy["design"]["spatial_fit"]["status"] == "fits"
    assert roomy["design"]["constraint_status"] == "fits"


def test_jubilee_base_inspection_and_cold_transfer_3d_checks():
    # Preserves base width (~483.14 mm) without adding extra X side clearance.
    base = adopt_foundation(layout_project())
    fit_base = evaluate_spatial_fit(base)
    assert fit_base["axes"]["width_mm"]["clearance_mm"] == 0.0
    assert fit_base["axes"]["depth_mm"]["clearance_mm"] == 250.0
    assert fit_base["axes"]["height_mm"]["clearance_mm"] == 100.0

    exact_width = base["design"]["foundation"]["bbox_mm"][0]
    narrow_fit = apply_update(base, {"workspace_width_mm": exact_width})
    assert narrow_fit["design"]["constraint_status"] == "fits"

    # Depth / height constraints fail when bench is shallower than frame + 250 mm clearance.
    shallow = adopt_foundation(
        apply_update(
            liquid_project(),
            {
                "workspace_width_mm": 700.0,
                "workspace_depth_mm": 700.0,
                "workspace_height_mm": 700.0,
            },
        )
    )
    assert shallow["design"]["constraint_status"] == "infeasible"
    assert shallow["design"]["spatial_fit"]["axes"]["depth_mm"]["fits"] is False
    with pytest.raises(ValueError, match="Required depth"):
        apply_candidate(shallow, source_proposal(), {"model": "offline"})

    # Base Jubilee fits in 1100 x 920 x 720, whereas inspection extension overflows depth/height.
    bounded = _source_with_limits(
        workspace_width_mm=1100.0, workspace_depth_mm=920.0, workspace_height_mm=720.0
    )
    with pytest.raises(ValueError, match="depth|height"):
        check_extension(bounded, inspection_proposal())

    roomy = _source_with_limits(
        workspace_width_mm=1200.0, workspace_depth_mm=1400.0, workspace_height_mm=1200.0
    )
    with_insp = apply_extension(roomy, inspection_proposal(), {"model": "offline"})
    assert with_insp["design"]["spatial_fit"]["status"] == "fits"
    checks = {c["id"]: c for c in with_insp["inspection_extension"]["checks"]}
    assert checks["workspace_depth"]["passed"] is True
    assert checks["workspace_height"]["passed"] is True

    # Base Jubilee + plate fits in 1300 x 950 x 780, while cold-transfer gantry overflows height.
    plate = select_plate(
        _source_with_limits(
            final_volume="200 uL",
            workspace_width_mm=1300.0,
            workspace_depth_mm=950.0,
            workspace_height_mm=780.0,
        )
    )
    with pytest.raises(ValueError, match="height"):
        apply_cold_transfer(plate)


def test_bench_station_and_concept_design_3d_limits():
    proposal = StationProposal(operations=["weigh", "inspect"])
    # Total depth = 420 mm + 100 mm front access = 520 mm; height = 360 + 100 = 460 mm.
    ok_station = compile_station(
        proposal, workspace_width_mm=900.0, workspace_depth_mm=550.0, workspace_height_mm=500.0
    )
    proj = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "workspace_width_mm": 900.0,
            "workspace_depth_mm": 550.0,
            "workspace_height_mm": 500.0,
        },
    )
    proj["bench_station"] = ok_station
    fit = evaluate_spatial_fit(proj)
    assert fit["subject"] == "bench_station"
    assert fit["status"] == "fits"

    with pytest.raises(ValueError, match="Workspace depth"):
        compile_station(proposal, workspace_width_mm=900.0, workspace_depth_mm=500.0)
    with pytest.raises(ValueError, match="Workspace height"):
        compile_station(proposal, workspace_width_mm=900.0, workspace_height_mm=440.0)

    c_prop = concept_proposal()
    c_ok = compile_concept(
        c_prop, workspace_width_mm=400.0, workspace_depth_mm=400.0, workspace_height_mm=300.0
    )
    proj["concept_design"] = c_ok
    proj.pop("bench_station")
    fit_c = evaluate_spatial_fit(proj)
    assert fit_c["subject"] == "concept_design"
    assert fit_c["status"] == "fits"

    with pytest.raises(ValueError, match="Workspace depth"):
        compile_concept(c_prop, workspace_width_mm=400.0, workspace_depth_mm=150.0)
    with pytest.raises(ValueError, match="Workspace height"):
        compile_concept(c_prop, workspace_width_mm=400.0, workspace_height_mm=80.0)


def test_printable_part_check_against_bambu_a1_mini():
    proj = apply_update(new_project(), {"workflow_kind": "general"})
    proj["concept_design"] = compile_concept(concept_proposal(), workspace_width_mm=400.0)
    a1_mini = {"printer": {"name": "Bambu Lab A1 mini", "build_volume_mm": [180.0, 180.0, 180.0]}}

    # Default turntable cylinder 120x20x120 fits cleanly on 180x180x180.
    checks = evaluate_printable_fit(proj, a1_mini)
    assert len(checks) == 1
    assert checks[0]["fits"] is True

    # Rectangular 130 x 170 part fits only after 90-deg XY rotation on 180 x 140 bed.
    asym_printer = {"printer": {"name": "Asymmetric bed", "build_volume_mm": [180.0, 140.0, 180.0]}}
    proj["labware_rack"] = {"size_mm": [130.0, 50.0, 170.0]}
    rot_checks = evaluate_printable_fit(proj, asym_printer)
    rack_check = next(c for c in rot_checks if c["part_id"] == "labware_rack")
    assert rack_check["fits"] is True
    assert rack_check["orientation"] == "rotated_90_xy"

    proj["labware_rack"] = {"size_mm": [195.0, 50.0, 100.0]}
    fail_checks = evaluate_printable_fit(proj, a1_mini)
    assert next(c for c in fail_checks if c["part_id"] == "labware_rack")["fits"] is False


def test_3d_limits_snapshot_roundtrip_and_legacy_snapshot_compat():
    proj = _source_with_limits(
        workspace_width_mm=800.0, workspace_depth_mm=950.0, workspace_height_mm=850.0
    )
    snap = make_snapshot(proj, [])
    restored, _ = restore_snapshot(decode_snapshot(json.dumps(snap).encode()))
    assert restored["summary"]["workspace_width_mm"] == 800.0
    assert restored["summary"]["workspace_depth_mm"] == 950.0
    assert restored["summary"]["workspace_height_mm"] == 850.0
    assert restored["design"] == proj["design"]

    # Legacy snapshots omitting workspace_depth_mm and workspace_height_mm still restore cleanly.
    legacy = json.loads(json.dumps(snap))
    legacy["inputs"]["summary"].pop("workspace_depth_mm")
    legacy["inputs"]["summary"].pop("workspace_height_mm")
    legacy_restored, _ = restore_snapshot(legacy)
    assert legacy_restored["summary"]["workspace_depth_mm"] is None
    assert legacy_restored["summary"]["workspace_height_mm"] is None

    # Applying lab profile applies 3D dimensions too.
    applied = apply_profile_to_project(
        new_project(),
        {"bench": {"width_mm": 650.0, "depth_mm": 520.0, "height_mm": 750.0}},
    )
    assert applied["summary"]["workspace_width_mm"] == 650.0
    assert applied["summary"]["workspace_depth_mm"] == 520.0
    assert applied["summary"]["workspace_height_mm"] == 750.0


def test_extracted_turn_validates_3d_workspace_units():
    msg = "Bench is 60 cm wide, 50 cm deep, and 800 mm high."
    turn = ExtractedTurn(
        assistant_message="Recorded all three bench dimensions.",
        workspace_width_mm=600.0,
        workspace_depth_mm=500.0,
        workspace_height_mm=800.0,
        source_workspace_width_mm="60 cm",
        source_workspace_depth_mm="50 cm",
        source_workspace_height_mm="800 mm",
    )
    turn.verify_sources(msg)
    assert turn.patch() == {
        "workspace_width_mm": 600.0,
        "workspace_depth_mm": 500.0,
        "workspace_height_mm": 800.0,
    }

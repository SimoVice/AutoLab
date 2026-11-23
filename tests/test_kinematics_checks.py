"""Tests for Task 3.3 swept-volume clearance, tool reach, and payload/CoM checks."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import httpx
import pytest
from test_foundation_storage import source_project

import app.api as api
from app.agent_tools import ToolContext, execute_tool
from app.concept_design import compile_concept
from app.domain import apply_update, new_project
from app.kinematics_checks import (
    check_payload_and_com,
    check_swept_volume_clearances,
    check_tool_reach,
    check_workstation_reach_and_transit,
    evaluate_concept_kinematics,
)
from app.labware_registry import clear_custom_labware_registry
from app.microplates import LabwareSelection, select_plate
from app.spatial_fit import evaluate_spatial_fit


def _base_gantry_proposal():
    return {
        "title": "Balanced Optical Scanner",
        "overview": "Gantry rail moving a light sensor head cleanly over specimen.",
        "components": [
            {
                "key": "frame",
                "name": "Base Rail Frame",
                "role": "Wide stationary aluminum base supporting rail and sample.",
                "source_id": None,
                "assembly": "Bolt base to bench.",
                "check": "Check leveling.",
                "primitives": [
                    {
                        "shape": "box",
                        "size_mm": [260, 10, 120],
                        "center_mm": [0, 5, 0],
                        "finish": "frame",
                    },
                    {
                        "shape": "extrusion",
                        "profile": "2020",
                        "axis": "x",
                        "size_mm": [240, 20, 20],
                        "center_mm": [0, 80, -35],
                        "finish": "frame",
                    },
                ],
            },
            {
                "key": "carriage",
                "name": "Scanner Carriage",
                "role": "Moves along the top rail.",
                "source_id": None,
                "assembly": "Mount carriage on rail.",
                "check": "Slide X=0..140.",
                "parent_key": "frame",
                "placement_mm": [-70, 90, -35],
                "primitives": [
                    {
                        "shape": "box",
                        "size_mm": [30, 15, 24],
                        "center_mm": [0, 7.5, 0],
                        "finish": "polymer",
                    }
                ],
            },
            {
                "key": "sample",
                "name": "Target Specimen",
                "role": "Target located away from moving carriage path.",
                "source_id": None,
                "assembly": "Place on front zone of base.",
                "check": "Verify clearance.",
                "parent_key": "frame",
                "placement_mm": [0, 10, 35],
                "primitives": [
                    {
                        "shape": "box",
                        "size_mm": [40, 20, 40],
                        "center_mm": [0, 10, 0],
                        "finish": "polymer",
                    }
                ],
            },
        ],
        "steps": [
            {
                "name": "Scan X",
                "instruction": "Translate carriage across X=140 mm.",
                "component_keys": ["frame", "carriage", "sample"],
                "moving_key": "carriage",
                "translation_mm": [140, 0, 0],
            }
        ],
        "software": {
            "controller": "Offline step/dir plan.",
            "interfaces": ["X stepper"],
            "setup": ["Home carriage."],
            "checks": ["Verify full stroke."],
        },
        "open_questions": [],
    }


def test_clean_balanced_concept_passes_all_kinematics_checks():
    design = compile_concept(_base_gantry_proposal())
    report = evaluate_concept_kinematics(design)
    assert report["passed"] is True
    assert report["swept_clearances"]["passed"] is True
    assert report["swept_clearances"]["min_clearance_mm"] >= 2.0
    assert report["tool_reach"]["passed"] is True
    assert report["payload_and_com"]["passed"] is True
    assert report["payload_and_com"]["root_stability"][0]["stability_margin_mm"] > 15.0


def test_swept_volume_catches_mid_path_collision_missed_by_start_end_poses():
    design = compile_concept(_base_gantry_proposal())
    # Move sample right into X=0, Z=-35 (midway at t=0.5 between rest X=-70 and end X=+70),
    # while carriage start [-85..-55] and end [+55..+85] both avoid the 2 mm post at [-1..+1]!
    thin_post = next(c for c in design["components"] if c["key"] == "sample")
    base_pos = next(c for c in design["components"] if c["key"] == "frame")["position_mm"]
    thin_post["position_mm"] = [base_pos[0], 10.0, base_pos[2] - 35.0]
    thin_post["primitives"][0]["size_mm"] = [2.0, 120.0, 10.0]
    thin_post["primitives"][0]["center_mm"] = [0.0, 60.0, 0.0]

    swept = check_swept_volume_clearances(design, substeps=24)
    assert swept["passed"] is False
    assert swept["interferences"]
    hit = swept["interferences"][0]
    assert hit["moving_key"] == "carriage"
    assert hit["fixed_key"] == "sample"
    assert 0.35 <= hit["t_start"] <= 0.65
    assert hit["penetration_mm"] > 0


def test_swept_volume_flags_near_miss_and_rotary_arc_collision():
    design = compile_concept(_base_gantry_proposal())
    sample = next(c for c in design["components"] if c["key"] == "sample")
    base_pos = next(c for c in design["components"] if c["key"] == "frame")["position_mm"]
    # Carriage spans Z=-35 +/- 12 -> [-47..-23]; put obstacle front face at Z=-22.0 -> 1.0 mm gap!
    sample["position_mm"] = [base_pos[0], 10.0, base_pos[2] - 12.0]
    sample["primitives"][0]["size_mm"] = [20.0, 100.0, 20.0]
    sample["primitives"][0]["center_mm"] = [0.0, 50.0, 0.0]
    swept = check_swept_volume_clearances(design, min_clearance_mm=2.0)
    assert swept["passed"] is True
    assert swept["near_misses"]
    assert swept["near_misses"][0]["clearance_mm"] == pytest.approx(1.0, abs=0.05)

    # Now mutate step to rotate 90 deg around Y through an arm hitting Z=+5 obstacle.
    design["steps"][0]["translation_mm"] = [0.0, 0.0, 0.0]
    design["steps"][0]["rotation_deg"] = 90.0
    design["steps"][0]["rotation_delta_deg"] = 90.0
    design["steps"][0]["rotation_axis"] = "y"
    design["steps"][0]["pivot_mm"] = [50.0, 0.0, 0.0]
    sample["position_mm"] = [base_pos[0] - 20.0, 80.0, base_pos[2] - 80.0]
    sample["primitives"][0]["size_mm"] = [10.0, 40.0, 10.0]
    sample["primitives"][0]["center_mm"] = [0.0, 20.0, 0.0]
    rot_swept = check_swept_volume_clearances(design, substeps=24)
    assert rot_swept["passed"] is False


def test_tool_reach_flags_rail_overhang_and_workstation_z_transit():
    design = compile_concept(_base_gantry_proposal())
    # Push X travel far past the 240 mm rail span -> carriage center overhangs >50% carriage width.
    design["steps"][0]["translation_mm"] = [300.0, 0.0, 0.0]
    reach = check_tool_reach(design)
    assert reach["passed"] is False
    assert any(issue["kind"] == "rail_overhang" for issue in reach["issues"])

    clear_custom_labware_registry()
    proj = apply_update(source_project(), {"final_volume": "100 uL"})
    selected, _ = select_plate(
        proj,
        LabwareSelection(
            profile_id="nest_96_wellplate_2ml_deep",
            source="Switch to NEST 96 Deep Well Plate 2 mL",
        ),
        "Switch to NEST 96 Deep Well Plate 2 mL",
    )
    ok_ws = check_workstation_reach_and_transit(selected)
    assert ok_ws["passed"] is True
    assert ok_ws["tallest_obstacle_height_mm"] >= 46.0
    fit = evaluate_spatial_fit(selected)
    assert "kinematics" in fit and fit["kinematics"]["passed"] is True

    # Force pipette too low so Z-transit collides with the deck obstacle.
    low_pipette = deepcopy(selected)
    pip = next(c for c in low_pipette["design"]["components"] if c["id"] == "pipette")
    pip["position"][1] = 280.0
    bad_ws = check_workstation_reach_and_transit(low_pipette)
    assert bad_ws["passed"] is False
    assert bad_ws["z_transit_clearance_mm"] < 0


def test_payload_com_detects_tip_over_and_excessive_moving_mass():
    # Cantilevered heavy steel block sitting off the edge of a tiny foot -> negative CoM margin.
    p = {
        "title": "Cantilevered Rig",
        "overview": "Intentional off-center heavy steel overhang.",
        "components": [
            {
                "key": "foot",
                "name": "Small Base",
                "role": "Small mounting foot.",
                "source_id": None,
                "assembly": "Place on bench.",
                "check": "Check tip stability.",
                "primitives": [
                    {
                        "shape": "box",
                        "size_mm": [40, 10, 40],
                        "center_mm": [0, 5, 0],
                        "finish": "polymer",
                    }
                ],
            },
            {
                "key": "heavy_head",
                "name": "Heavy Cantilever Block",
                "role": "Cantilevered steel block.",
                "source_id": None,
                "assembly": "Attach off-center.",
                "check": "Evaluate CoM.",
                "parent_key": "foot",
                "placement_mm": [100, 10, 0],
                "primitives": [
                    {
                        "shape": "box",
                        "size_mm": [120, 40, 80],
                        "center_mm": [0, 20, 0],
                        "finish": "steel",
                    }
                ],
            },
        ],
        "steps": [
            {
                "name": "Lift Heavy Block",
                "instruction": "Move heavy steel head up 10 mm.",
                "component_keys": ["foot", "heavy_head"],
                "moving_key": "heavy_head",
                "translation_mm": [0, 10, 0],
            }
        ],
        "software": {"controller": "N/A", "interfaces": [], "setup": ["Inspect"], "checks": ["OK"]},
        "open_questions": [],
    }
    design = compile_concept(p)
    payload = check_payload_and_com(design)
    assert payload["passed"] is False
    kinds = {issue["kind"] for issue in payload["issues"]}
    assert {"tip_over_risk", "payload_exceeded"} <= kinds
    assert payload["root_stability"][0]["stability_margin_mm"] < 0


def test_kinematics_http_endpoint_and_agent_check_spatial_fit_tool():
    proj = apply_update(new_project(), {"workflow_kind": "general"})
    proj["concept_design"] = compile_concept(_base_gantry_proposal())
    api._sessions[proj["id"]] = api.ProjectSession(project=deepcopy(proj))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/projects/{proj['id']}/concept/kinematics",
                params={"revision": proj["revision"]},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["passed"] is True
            assert body["payload_and_com"]["total_estimated_mass_g"] > 0

        ctx = ToolContext(project=deepcopy(proj), user_message="Check spatial fit and kinematics.")
        tool_res = await execute_tool("check_spatial_fit", {}, ctx)
        assert tool_res.ok is True
        assert tool_res.data["concept_kinematics"]["passed"] is True

    try:
        asyncio.run(run())
    finally:
        api._sessions.pop(proj["id"], None)

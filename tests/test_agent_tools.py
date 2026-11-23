"""Tests for the typed OpenAI domain tool schemas and atomic execution engine."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from foundation_fixtures import liquid_project, source_proposal
from test_concept_design import proposal as concept_proposal
from test_foundation_storage import source_project

from app import api
from app.concept_design import compile_concept
from app.design_spec import apply_candidate
from app.domain import apply_update, new_project
from app.foundation_design import adopt_foundation


def _walk_schemas(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schemas(item)


def test_all_13_openai_tool_schemas_are_strict_and_complete():
    from app.agent_tools import openai_tool_schemas

    schemas = openai_tool_schemas(new_project())
    names = [entry["function"]["name"] for entry in schemas]
    assert names == [
        "inspect_project_state",
        "check_spatial_fit",
        "lookup_reviewed_references",
        "update_project_brief",
        "update_liquid_recipe",
        "select_platform",
        "develop_workstation_layout",
        "configure_labware",
        "manage_extension",
        "develop_bench_station",
        "develop_custom_equipment",
        "approve_machine",
        "update_lab_profile",
    ]
    for entry in schemas:
        assert entry["type"] == "function"
        fn = entry["function"]
        assert fn["strict"] is True
        params = fn["parameters"]
        assert params["type"] == "object"
        for node in _walk_schemas(params):
            assert "default" not in node
            if "properties" in node:
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])


def test_invalid_arguments_and_unknown_tool_never_mutate_context():
    from app.agent_tools import ToolContext, execute_tool

    proj = new_project()
    before = deepcopy(proj)
    ctx = ToolContext(project=proj, user_message="Inspect")

    unknown = asyncio.run(execute_tool("nonexistent_tool", {}, ctx))
    assert unknown.ok is False and unknown.recoverable is True
    assert ctx.project == before and ctx.mutated is False

    bad_args = asyncio.run(execute_tool("update_project_brief", {"unexpected": 1}, ctx))
    assert bad_args.ok is False and bad_args.recoverable is True
    assert ctx.project == before and ctx.mutated is False


def test_brief_spatial_fit_platform_selection_and_ot2_depth_overflow():
    from app.agent_tools import ToolContext, execute_tool

    ctx = ToolContext(
        project=new_project(),
        user_message="Pipette dilutions on a 1200 x 700 x 750 mm bench.",
        lab_profile={"printer": {"name": "A1 mini", "build_volume_mm": [180.0, 180.0, 180.0]}},
    )

    state_res = asyncio.run(execute_tool("inspect_project_state", {}, ctx))
    assert state_res.ok is True and state_res.mutated is False

    refs_res = asyncio.run(
        execute_tool("lookup_reviewed_references", {"reference_id": "jubilee-hardware"}, ctx)
    )
    assert refs_res.ok is True
    assert len(ctx.recommendations) == 1

    brief_res = asyncio.run(
        execute_tool(
            "update_project_brief",
            {
                "workflow_kind": "liquid_preparation",
                "process": "Direct serum dilution",
                "materials": "2 mL tubes",
                "equipment": "micropipette",
                "workspace_width_mm": 1200.0,
                "workspace_depth_mm": 700.0,
                "workspace_height_mm": 750.0,
            },
            ctx,
        )
    )
    assert brief_res.ok is True and brief_res.mutated is True
    assert ctx.project["summary"]["workspace_depth_mm"] == 700.0
    assert ctx.project["dimension_provenance"]["bench_depth_mm"]["provenance"] == "user_measured"

    # OT-2 fails depth (970 > 700) and height (810 > 750) recoverably without mutating project.
    snap_before = deepcopy(ctx.project)
    ot2_fail = asyncio.run(execute_tool("select_platform", {"platform_id": "opentrons-ot2"}, ctx))
    assert ot2_fail.ok is False and ot2_fail.recoverable is True
    assert ctx.project == snap_before
    assert any("970" in d for d in ot2_fail.diagnostics)

    # Widen/deepen/raise bench to fit OT-2 and retry platform selection.
    asyncio.run(
        execute_tool(
            "update_project_brief",
            {"workspace_depth_mm": 1000.0, "workspace_height_mm": 850.0},
            ctx,
        )
    )
    ot2_ok = asyncio.run(execute_tool("select_platform", {"platform_id": "opentrons-ot2"}, ctx))
    assert ot2_ok.ok is True and ot2_ok.mutated is True
    assert ctx.project["design"]["platform"]["id"] == "opentrons-ot2"


def test_workstation_layout_labware_extensions_recipe_and_approval(monkeypatch):
    from app.agent_tools import ToolContext, execute_tool

    # 1) Missing context asks human question instead of failing.
    no_ctx = ToolContext(
        project=apply_update(new_project(), {"workflow_kind": "liquid_preparation"}),
        user_message="Build layout",
    )
    no_ctx.project = adopt_foundation(no_ctx.project)
    from app.engineering import EngineeringUpdate, apply_engineering_update

    no_ctx.project = apply_engineering_update(
        no_ctx.project,
        EngineeringUpdate(platform_choice="jubilee", choice_source="Use Jubilee"),
        "Use Jubilee",
    )
    ask_res = asyncio.run(
        execute_tool("develop_workstation_layout", {"instruction": "Arrange parts"}, no_ctx)
    )
    assert ask_res.ok is True and ask_res.requires_human_input is True and ask_res.question

    # 2) Layout with mocked develop_design hook, labware, plate switch, cold transfer, approval.
    proj = apply_update(
        liquid_project(),
        {"workspace_width_mm": 1200.0, "workspace_depth_mm": 1300.0, "workspace_height_mm": 1200.0},
    )
    proj = adopt_foundation(proj)
    ctx = ToolContext(
        project=proj,
        user_message="Arrange deck on Jubilee",
        lab_profile={"printer": {"name": "A1 mini", "build_volume_mm": [180.0, 180.0, 180.0]}},
    )

    async def fake_develop_design(base, instruction):
        built = apply_candidate(base, source_proposal(), {"model": "mock"})
        return built, {"status": "accepted", "message": "Accepted layout."}

    monkeypatch.setattr(api, "develop_design", fake_develop_design)
    layout_res = asyncio.run(
        execute_tool("develop_workstation_layout", {"instruction": "Place parts on deck"}, ctx)
    )
    assert layout_res.ok is True and ctx.project.get("engineering_design")

    # Partial tube rack dimensions trigger pause for human input cleanly.
    partial_rack = asyncio.run(
        execute_tool(
            "configure_labware",
            {
                "action": "configure_tube_rack",
                "count": 12,
                "columns": 4,
                "body_diameter_max_mm": 10.8,
            },
            ctx,
        )
    )
    assert partial_rack.ok is True and partial_rack.requires_human_input is True

    full_rack = asyncio.run(
        execute_tool(
            "configure_labware",
            {
                "action": "configure_tube_rack",
                "rim_diameter_min_mm": 13.2,
                "cap_diameter_max_mm": 13.6,
                "below_rim_height_mm": 38.0,
            },
            ctx,
        )
    )
    assert full_rack.ok is True and ctx.project.get("labware_rack")
    assert ctx.project["dimension_provenance"]["tube_rim_diameter_min_mm"]["provenance"] == (
        "user_measured"
    )

    # Check printable volume evaluation includes custom tube rack.
    fit_res = asyncio.run(execute_tool("check_spatial_fit", {"include_printable_parts": True}, ctx))
    assert len(fit_res.data["spatial_fit"]["printable_checks"]) >= 1

    # Select Corning 96-well microplate & set volume so cold transfer fits (<360 uL).
    recipe_ok = asyncio.run(
        execute_tool("update_liquid_recipe", {"final_volume": "200 uL"}, ctx)
    )
    assert recipe_ok.ok is True
    plate_res = asyncio.run(
        execute_tool("configure_labware", {"action": "select_microplate"}, ctx)
    )
    assert plate_res.ok is True and ctx.project.get("labware_plate")

    # Tip rack requiring a minor shift pauses for human confirmation, then applies when confirmed.
    tip_pause = asyncio.run(
        execute_tool(
            "configure_labware",
            {"action": "configure_tip_rack", "tip_profile_id": "tipone_96_tiprack_200ul"},
            ctx,
        )
    )
    assert tip_pause.ok is True and tip_pause.requires_human_input is True
    tip_res = asyncio.run(
        execute_tool(
            "configure_labware",
            {
                "action": "configure_tip_rack",
                "tip_profile_id": "tipone_96_tiprack_200ul",
                "confirm_tip_rack_shift": True,
            },
            ctx,
        )
    )
    assert tip_res.ok is True and ctx.project.get("tip_rack")

    # Add and remove cold transfer extension.
    cold_add = asyncio.run(
        execute_tool("manage_extension", {"extension": "cold_transfer", "operation": "add"}, ctx)
    )
    assert cold_add.ok is True and ctx.project.get("cold_transfer_extension")
    cold_rm = asyncio.run(
        execute_tool(
            "manage_extension", {"extension": "cold_transfer", "operation": "remove"}, ctx
        )
    )
    assert cold_rm.ok is True and not ctx.project.get("cold_transfer_extension")

    # Machine approval requires explicit confirmation flag.
    no_confirm = asyncio.run(
        execute_tool("approve_machine", {"explicit_user_confirmation": False}, ctx)
    )
    assert no_confirm.ok is False
    approved = asyncio.run(
        execute_tool("approve_machine", {"explicit_user_confirmation": True}, ctx)
    )
    assert approved.ok is True and ctx.project["design_workflow"]["machine_approved"] is True


def test_bench_station_custom_equipment_and_lab_profile(monkeypatch, tmp_path):
    from app import concept_agent
    from app.agent_tools import ToolContext, execute_tool

    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.delenv("AUTOLAB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AUTOLAB_LIBRARY_DIRECTORY", raising=False)

    ctx = ToolContext(project=new_project(), user_message="Configure lab and bench station")
    prof_res = asyncio.run(
        execute_tool(
            "update_lab_profile",
            {
                "lab_name": "Bench Lab",
                "printer_name": "Bambu Lab A1 mini",
                "printer_build_volume_mm": [180.0, 180.0, 180.0],
                "bench_width_mm": 900.0,
                "bench_depth_mm": 450.0,
                "bench_height_mm": 500.0,
                "measuring_tools": ["Digital caliper"],
            },
            ctx,
        )
    )
    assert prof_res.ok is True
    assert ctx.project["summary"]["workspace_width_mm"] == 900.0

    # Weigh + inspect requires 420 + 100 = 520 mm depth -> fails on 450 mm depth safely.
    before = deepcopy(ctx.project)
    too_shallow = asyncio.run(
        execute_tool("develop_bench_station", {"operations": ["weigh", "inspect"]}, ctx)
    )
    assert too_shallow.ok is False and too_shallow.recoverable is True
    assert ctx.project == before

    # Relax bench depth -> bench station compiles cleanly.
    asyncio.run(
        execute_tool("update_project_brief", {"workspace_depth_mm": 600.0}, ctx)
    )
    station_ok = asyncio.run(
        execute_tool("develop_bench_station", {"operations": ["weigh", "inspect"]}, ctx)
    )
    assert station_ok.ok is True and ctx.project["bench_station"]["proposal"]["operations"] == [
        "weigh",
        "inspect",
    ]

    # Custom equipment execution on fresh general project via mocked develop_concept.
    concept_ctx = ToolContext(project=new_project(), user_message="Design rotary optical jig")

    async def fake_develop_concept(candidate, message, history=()):
        compiled = compile_concept(
            concept_proposal(), candidate["summary"].get("workspace_width_mm")
        )
        return compiled, f"{compiled['title']} is ready to review.", [{"total_tokens": 12}]

    monkeypatch.setattr(concept_agent, "develop_concept", fake_develop_concept)
    concept_res = asyncio.run(
        execute_tool(
            "develop_custom_equipment", {"instruction": "Build rotary optical jig"}, concept_ctx
        )
    )
    assert concept_res.ok is True and concept_ctx.project.get("concept_design")
    assert source_project()["revision"] >= 1

"""Physical identities and review boundaries shared across project types."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_concept_interfaces import fixture as optical_fixture
from test_custom_project_routes import project_fixture

from app.agent_tools import ToolContext, execute_tool
from app.concept_design import compile_concept
from app.design_workflow import approve_machine
from app.domain import new_project
from app.fixture_adapter import attach_standalone_fixture
from app.project_assembly import assembly_binding, physical_assembly
from app.project_storage import make_snapshot, restore_snapshot


def assert_references(assembly):
    frames = assembly["frames"]
    entities = {kind: {e["id"] for e in assembly[kind]} for kind in
                ("instances", "interfaces", "connections", "requirements", "evidence")}
    for kind, ids in entities.items():
        assert len(ids) == len(assembly[kind])
        for item in assembly[kind]:
            assert set(item.get("evidence_ids", [])) <= entities["evidence"]
    for item in assembly["instances"]:
        assert item["frame_id"] in frames and item["geometry"]["frame_id"] in frames
        if item["placement"]:
            assert item["placement"]["frame_id"] in frames
    for frame in frames.values():
        assert frame["unit"] == "mm"
        assert frame.get("parent") is None or frame["parent"] in frames
    for port in assembly["interfaces"]:
        assert port["owner_id"] in entities["instances"] | entities["requirements"]
    for joint in assembly["connections"]:
        assert {joint["from_interface"], joint["to_interface"]} <= entities["interfaces"]
        assert joint["from_interface"] != joint["to_interface"]
    for row in assembly["requirements"]:
        assert set(row["instance_ids"]) <= entities["instances"]


def test_concept_nested_frames_and_working_axis_follow_the_compiler():
    project = project_fixture()
    project["concept_design"] = compile_concept(optical_fixture())
    assembly = physical_assembly(project)
    assert_references(assembly)
    by_key = {p["native_id"]: p for p in assembly["instances"]}
    compiled = {p["key"]: p for p in project["concept_design"]["components"]}
    for key in ("card", "camera"):
        part, native = by_key[key], compiled[key]
        parent = by_key[native["parent_key"]]
        assert part["geometry"]["frame_id"] == part["frame_id"]
        assert part["placement"]["frame_id"] == parent["frame_id"]
        assert part["placement"]["translation_mm"] == native["placement_mm"]
        assert part["placement"]["world_translation_mm"] == native["position_mm"]
    axis, = [joint for joint in assembly["connections"] if joint["kind"] == "working_axis"]
    ports = {p["id"]: p for p in assembly["interfaces"]}
    assert ports[axis["from_interface"]]["details"]["frame_id"] == by_key["camera"]["frame_id"]
    assert ports[axis["to_interface"]]["details"]["frame_id"] == by_key["card"]["frame_id"]
    assert ports[axis["from_interface"]]["details"]["axis"] == [0, -1, 0]
    assert axis["details"]["step_name"] == "Index card"
    project["concept_design"]["components"][0]["name"] = "Renamed support"
    assert assembly_binding(project)["geometry_sha256"] == assembly["geometry_sha256"]


def test_fixture_evidence_unplaced_coupon_and_purchasing_interfaces(monkeypatch):
    project = attach_standalone_fixture(new_project(), {
        "title": "Plate imaging support", "fixture_kind": "imaging_rig",
        "object_reference_id": "slas_96_well_plate", "include_camera_mount": True,
    }, lab_profile={})
    def forbid_cache(*args):
        pytest.fail("A structural assembly must be reproducible without supplier or FX caches.")
    monkeypatch.setattr("app.bom_engine.cached_official_quotes", forbid_cache)
    monkeypatch.setattr("app.bom_engine.cached_fx_rates", forbid_cache)
    assembly = physical_assembly(project)
    assert_references(assembly)
    parts = {p["native_id"]: p for p in assembly["instances"]}
    assert parts["tolerance-coupon"]["placement"] is None
    assert assembly["frames"][parts["tolerance-coupon"]["frame_id"]]["parent"] is None
    refs = {item["id"]: item for item in assembly["evidence"]}
    evidence = [refs[k] for k in parts["reference-object"]["evidence_ids"]]
    assert any(item["details"].get("revision") == project["standalone_fixture"]["reference"][
        "revision"] for item in evidence)
    holes = [p for p in assembly["interfaces"] if p["kind"] == "mounting_hole"]
    assert len(holes) == 2 and all(p["owner_id"] == parts["locating-base"]["id"] for p in holes)
    electrical = {p["id"] for p in assembly["interfaces"]
                  if p["kind"] == "electrical_specification"}
    assert electrical
    assert all(j["from_interface"] not in electrical and j["to_interface"] not in electrical
               for j in assembly["connections"])
    original = make_snapshot(project, [])
    original.pop("physical_assembly")
    old = deepcopy(original)
    reopened, _ = restore_snapshot(original)
    assert physical_assembly(reopened) == assembly and original == old


def test_known_previous_brand_opens_and_preserves_approved_workstation():
    path = Path(__file__).resolve().parents[1] / "docs/examples/revision-consistency-project.json"
    saved = json.loads(path.read_bytes())
    saved["format"] = "museautolab-project"
    original = deepcopy(saved)
    restored, history = restore_snapshot(saved)
    assembly = physical_assembly(restored)
    assert_references(assembly)
    assert restored["revision"] == saved["revision"] and restored["status"] == "confirmed"
    assert any(p["origin"] == "imported_foundation" for p in assembly["instances"])
    assert any(p["kind"] == "deck_slot" or p["kind"] == "proposed_layout"
               for p in assembly["connections"])
    portable = make_snapshot(restored, history)
    assert portable["format"] == "autolab-project"
    assert portable["physical_assembly"] == {
        "version": 1, "geometry_sha256": assembly["geometry_sha256"]}
    reopened, _ = restore_snapshot(portable)
    assert physical_assembly(reopened) == assembly
    assert saved == original


def test_geometry_changing_rebuild_requires_one_new_revision_and_fresh_approval(monkeypatch):
    project = approve_machine(project_fixture())
    saved = make_snapshot(project, [])
    original = deepcopy(saved)

    def shifted_layout(*args, **kwargs):
        design = compile_concept(*args, **kwargs)
        for part in design["components"]:
            x, y, z = part["position_mm"]
            part["position_mm"] = [x + 2, y, z]
        return design

    monkeypatch.setattr("app.concept_design.compile_concept", shifted_layout)
    restored, _ = restore_snapshot(saved)
    assert saved == original
    assert restored["revision"] == saved["revision"] + 1
    assert restored["status"] == "draft" and not restored["design_workflow"]["machine_approved"]
    assert "approve" in restored["rebuild_notice"]
    assert assembly_binding(restored) != saved["physical_assembly"]
    twice, _ = restore_snapshot(make_snapshot(restored, []))
    assert twice["revision"] == restored["revision"] and not twice.get("machine_approval")
    approved = approve_machine(twice)
    confirmed, _ = restore_snapshot(make_snapshot(approved, []))
    assert confirmed["design_workflow"]["machine_approved"]
    assert confirmed["revision"] == approved["revision"]


def test_empty_project_has_no_physical_instances():
    assembly = physical_assembly(new_project())
    assert assembly["route"] == "unselected"
    assert not any(assembly[name] for name in ("instances", "connections", "requirements"))


def test_agent_inspects_the_active_custom_parts_and_their_requirements():
    project = project_fixture()
    ctx = ToolContext(project=project, user_message="Inspect this design.")
    result = asyncio.run(execute_tool("inspect_project_state", {"include_components": True}, ctx))
    assert result.ok and not result.mutated
    data = result.data
    assembly = physical_assembly(project)
    assert {p["id"] for p in data["components"]} == {"rail_base", "sample_tray"}
    instances = {p["id"] for p in assembly["instances"]}
    assert {p["instance_id"] for p in data["components"]} == instances
    assert data["connections"] == assembly["connections"]
    assert data["assembly_evidence"] == assembly["evidence"]
    assert all(row["evidence_ids"] for row in data["procurement_links"])

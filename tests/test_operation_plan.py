"""Check preview, simulation, exports and recorded device calls against the same revision."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_design_workflow import hardware
from test_microplates import select

import app.api as api
from app.bench_station import StationProposal, compile_station
from app.bench_station_runner import dry_run, run_record
from app.design_workflow import approve_machine
from app.domain import apply_update, new_project
from app.instrument_drivers import BarcodeScannerDriver, SerialBalanceDriver
from app.operation_plan import project_operation_plan
from app.project_storage import make_snapshot, restore_snapshot
from app.protocol_generators import generate_jubilee_protocol

TARGETS = "5 mM, 2 mM, 1 mM, 500 uM, 200 uM, 100 uM, 10 uM, 1 uM, 100 nM, 10 nM, 5 nM, 1 nM"


class RecordingPipette:
    def __init__(self):
        self.calls = []

    def transfer(self, volume, source, dest, *, new_tip):
        self.calls.append(("transfer", volume, source, dest, new_tip))

    def mix(self, volume, repeats, dest):
        self.calls.append(("mix", volume, repeats, dest))


def issue_software(client, project):
    base = "/api/projects/" + project["id"]
    response = client.post(
        base + "/program-recipe",
        json={
            "project_revision": project["revision"],
            "stock_concentration": "10 mM",
            "target_concentrations": TARGETS,
            "final_volume": "200 uL",
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()["project"]
    response = client.post(
        base + "/software/protocol", json={"project_revision": updated["revision"]}
    )
    assert response.status_code == 200, response.text
    return updated, response.json()


def test_jubilee_preview_simulation_and_recorded_dispatch_match_each_operation(monkeypatch):
    project = approve_machine(select(hardware()))
    monkeypatch.setitem(api._sessions, project["id"], api.ProjectSession(project=project))
    client = TestClient(api.app)
    saved, issued = issue_software(client, project)
    base = "/api/projects/" + project["id"]
    query = {"revision": saved["revision"]}
    plan = client.get(base + "/software/operations", params=query).json()
    assert issued["operation_plan"] == plan
    assert plan["scope"] == "approved" and plan["bindings"]["pipette"]["component_id"].endswith(
        "/part/pipette"
    )
    ids = [r["id"] for r in plan["operations"]]
    assert (len(ids), issued["simulation"]["tips_used"]) == (51, 34)
    assert issued["simulation"]["operation_ids"] == ids
    assert issued["simulation"]["plan_sha256"] == plan["sha256"]
    assert all(
        v == pytest.approx(200.0) for v in issued["simulation"]["destination_wells"].values()
    )
    assert len(issued["simulation"]["destination_wells"]) == 12
    assert any("300 uL" in reason for reason in plan["hardware_blockers"])

    args = {"format": "protocol", **query}
    template = client.get(base + "/instructions/download", params=args).json()
    from app.protocol_preview import render_preview

    text = render_preview(template)
    assert template["operation_plan"] == plan
    assert [line.split(" · ")[0] for line in text.splitlines() if line.startswith("op-")] == ids
    program = client.get(base + "/instructions/download", params={**args, "format": "program"}).text
    assert plan["sha256"] in program

    module = {"__name__": "recording"}
    exec(issued["source_code"], module)
    pipette = RecordingPipette()
    assert module["_dispatch"](pipette, lambda role, well: (role, well), plan) == ids
    expected = []
    for op in plan["operations"]:
        p = op["parameters"]
        dest = (p["dest_role"], p["dest_well"])
        if op["action"] == "transfer":
            expected.append(
                ("transfer", p["volume_ul"], (p["source_role"], p["source_well"]), dest, "always")
            )
        else:
            expected.append(("mix", p["volume_ul"], 3, dest))
    assert pipette.calls == expected
    with pytest.raises(ValueError, match="Review and arm"):
        module["_run_hardware"]("unused", 0, armed=True, commissioning={})
    reopened, _ = restore_snapshot(make_snapshot(saved, []))
    assert project_operation_plan(reopened) == plan
    assert (
        client.get(
            base + "/software/operations", params={"revision": project["revision"]}
        ).status_code
        == 409
    )


def test_recipe_variants_and_alternate_platforms_are_illustrative(monkeypatch):
    project = approve_machine(select(hardware()))
    project = apply_update(
        project,
        {"stock_concentration": "10 mM", "target_concentrations": "1 mM", "final_volume": "200 uL"},
    )
    monkeypatch.setitem(api._sessions, project["id"], api.ProjectSession(project=project))
    client = TestClient(api.app)
    base = "/api/projects/" + project["id"]
    args = {"project_revision": project["revision"]}
    own = client.post(base + "/software/protocol", json=args).json()
    other = client.post(
        base + "/software/protocol", json={**args, "target_platform": "opentrons-ot2"}
    ).json()
    variant = client.post(
        base + "/software/protocol", json={**args, "target_concentrations": "2 mM"}
    ).json()
    assert own["operation_plan"]["scope"] == "approved"
    for issued in (other, variant):
        assert issued["operation_plan"]["scope"] == "illustrative"
        assert issued["operation_plan"]["sha256"] != own["operation_plan"]["sha256"]
        assert issued["simulation"]["plan_sha256"] == issued["operation_plan"]["sha256"]
    # An unchanged public method can only recheck source issued for the requested revision/platform.
    stale = client.post(
        base + "/software/simulate", json={**args, "source_code": other["source_code"]}
    )
    assert stale.status_code == 409
    altered = client.post(
        base + "/software/simulate", json={**args, "source_code": own["source_code"] + "\n"}
    )
    assert altered.status_code == 409


def station_project():
    project = apply_update(
        new_project(), {"workflow_kind": "general", "brief_objective": "Record reference blocks."}
    )
    project["bench_station"] = compile_station(
        StationProposal(operations=["read_barcode", "weigh", "inspect"])
    )
    from app.spatial_fit import refresh_spatial_fit

    refresh_spatial_fit(project)
    return approve_machine(project)


def test_bench_export_runs_same_checkpoints_and_recorded_instrument_calls(monkeypatch):
    project = station_project()
    monkeypatch.setitem(api._sessions, project["id"], api.ProjectSession(project=project))
    client = TestClient(api.app)
    base = "/api/projects/" + project["id"]
    params = {"revision": project["revision"]}
    plan = client.get(base + "/software/operations", params=params).json()
    exported = client.get(base + "/bench-station/plan.json", params=params).json()
    assert exported["operation_plan"] == plan and plan["scope"] == "approved"
    rows = plan["operations"]
    assert [r["mode"] for r in rows] == ["driver", "driver", "manual"]
    ids = [r["id"] for r in rows]
    assert [r["operation_id"] for r in dry_run(exported)["pending_human_checkpoints"]] == ids
    sim = client.post(
        base + "/software/simulate", json={"project_revision": project["revision"]}
    ).json()["simulation"]
    assert (
        sim["operation_ids"] == ids and sim["plan_sha256"] == plan["sha256"] and not sim["events"]
    )
    assert {v["component_id"] for v in plan["bindings"].values()} <= {
        p["id"] for p in exported["physical_assembly"]["instances"]
    }
    assert (
        plan["bindings"]["weigh"]["unit"] == "g" and plan["bindings"]["inspect"]["driver"] is None
    )
    answers = iter(
        ["test operator", "REF-42", "yes", "yes", "Readable label", "pass", "Test record"]
    )
    calls = []

    def frame(capability, value):
        calls.append(capability)
        return value

    result = run_record(
        exported,
        lambda _: next(answers),
        barcode_driver=BarcodeScannerDriver(line_reader=lambda: frame("read_barcode", "]C1REF-42")),
        balance_driver=SerialBalanceDriver(line_reader=lambda: frame("weigh", "S S 42.125 g")),
    )
    assert calls == ["read_barcode", "weigh"]
    assert [event["operation_id"] for event in result["events"]] == ids
    assert (
        result["events"][1]["mass"] == 42.125
        and result["events"][2]["provenance"] == "operator_entered"
    )
    assert result["operation_plan_sha256"] == plan["sha256"]
    with pytest.raises(ValueError, match="commissioning"):
        run_record(exported, balance_driver=SerialBalanceDriver(port="unreviewed"))
    stale = deepcopy(exported)
    stale["revision"] += 1
    with pytest.raises(ValueError, match="revision"):
        dry_run(stale)
    saved, _ = restore_snapshot(make_snapshot(project, []))
    assert project_operation_plan(saved) == plan


def test_draft_plan_does_not_arm_jubilee():
    project = apply_update(
        select(hardware()),
        {"stock_concentration": "10 mM", "target_concentrations": "1 mM", "final_volume": "200 uL"},
    )
    generated = generate_jubilee_protocol(project)
    assert generated["operation_plan"]["scope"] == "illustrative"
    module = {"__name__": "recording"}
    exec(generated["source_code"], module)
    with pytest.raises(ValueError, match="Review and arm"):
        module["_run_hardware"]("unused", 0, armed=True, commissioning={})


def test_custom_controller_reports_missing_driver_and_rejects_unbound_execution(monkeypatch):
    from test_custom_project_routes import project_fixture

    from app.concept_controller import run_sequence
    from app.operation_plan import simulate_sequence
    from app.spatial_fit import refresh_spatial_fit

    project = project_fixture()
    refresh_spatial_fit(project)
    project = approve_machine(project)
    monkeypatch.setitem(api._sessions, project["id"], api.ProjectSession(project=project))
    client = TestClient(api.app)
    base = "/api/projects/" + project["id"]
    query = {"revision": project["revision"]}
    payload = client.get(base + "/concept/project.json", params=query).json()
    config = client.get(base + "/concept/gateway-config.json", params=query).json()
    expected = client.get(base + "/software/operations", params=query).json()
    assert payload["operation_plan"] == expected and config["plan_sha256"] == expected["sha256"]
    missing = [r["id"] for r in expected["operations"] if r["mode"] == "missing_driver"]
    simulation = simulate_sequence(project)
    assert missing and simulation["missing_drivers"] == missing and not simulation["ok"]
    assert [e["operation_id"] for e in run_sequence(payload)["events"]] == simulation[
        "operation_ids"
    ]
    with pytest.raises(ValueError, match="device bindings"):
        run_sequence(payload, object(), commissioned=True)
    assert not simulate_sequence(new_project())["ok"]


def test_station_commissioning_binds_each_port_to_its_installed_component():
    from app.bench_station_runner import verify_devices

    plan = project_operation_plan(station_project())
    ports = {"weigh": "test-scale"}
    device = {"component_id": plan["bindings"]["weigh"]["component_id"], "port": ports["weigh"]}
    record = {
        "commissioning_reviewed": True,
        "project_id": plan["project_id"],
        "revision": plan["revision"],
        "plan_sha256": plan["sha256"],
        "devices": {"weigh": device},
    }
    verify_devices({"operation_plan": plan}, ports, True, record)
    for change in (
        {"revision": plan["revision"] + 1},
        {"devices": {"weigh": {**device, "port": "other"}}},
        {"devices": {"weigh": {**device, "component_id": "other"}}},
    ):
        with pytest.raises(ValueError, match="[Cc]ommission"):
            verify_devices({"operation_plan": plan}, ports, True, {**record, **change})


def test_ot2_uses_the_adopted_deck_for_its_driver_and_simulated_wells():
    from test_commercial_design import (
        adopt_commercial,
        compile_commercial_layout,
        layout_project,
        proposal,
    )

    from app.opentrons_verifier import verify_opentrons_protocol
    from app.protocol_generators import generate_opentrons_protocol

    project = approve_machine(
        compile_commercial_layout(
            adopt_commercial(layout_project()), proposal(), {"model": "offline"}
        )
    )
    issued = generate_opentrons_protocol(project)
    plan = issued["operation_plan"]
    result = verify_opentrons_protocol(issued["source_code"], try_cli=False)
    assert plan["scope"] == "approved" and result["ok"] and result["plan_sha256"] == plan["sha256"]
    assert issued["deck_slots"] == result["deck_slots_used"]
    assert result["operation_ids"] == [row["id"] for row in plan["operations"]]
    assert result["well_final_volumes_ul"] == {"A1": 100, "B1": 100}
    for device in plan["bindings"].values():
        assert (
            device["limits"]["destination_slot"] == "5" and device["limits"]["source_slot"] == "1"
        )
    first = next(iter(plan["bindings"].values()))
    assert first["limits"]["tiprack_slots"] == ["11"]
    assert first["limits"]["diluent_slot"] == "2"
    assert any("no bound assembly part" in reason for reason in plan["hardware_blockers"])

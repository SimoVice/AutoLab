"""Tests for Phase 6.1 executable Opentrons API v2 & Science Jubilee protocol generation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_design_workflow import hardware
from test_microplates import select

import app.api as api
from app.dilution import create_dilution_plan, create_protocol_dilution_plan
from app.domain import apply_update, new_project
from app.opentrons_verifier import verify_jubilee_protocol, verify_opentrons_protocol
from app.protocol_generators import (
    generate_jubilee_protocol,
    generate_opentrons_protocol,
)


def _approved_project(
    stock: str = "10 mM",
    targets: str = "1 mM, 0.5 mM",
    volume: str = "200 uL",
) -> dict:
    project = select(hardware())
    project = apply_update(
        project,
        {
            "stock_concentration": stock,
            "target_concentrations": targets,
            "final_volume": volume,
        },
    )
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    client = TestClient(api.app)
    res = client.post(
        f"/api/projects/{project['id']}/approve-machine",
        json={"project_revision": project["revision"]},
    )
    assert res.status_code == 200
    return res.json()["project"]


def test_legacy_create_dilution_plan_still_caps_at_six():
    assert create_dilution_plan("10 mM", ", ".join(["1 mM"] * 7), "1 mL")["status"] == "invalid"


def test_protocol_dilution_supports_serial_multi_dispense_and_over_six_targets():
    targets = ", ".join(
        [
            "5 mM",
            "1 mM",
            "500 uM",
            "100 uM",
            "10 uM",
            "1 uM",
            "500 nM",
            "100 nM",
            "50 nM",
            "10 nM",
            "5 nM",
            "1 nM",
        ]
    )
    plan = create_protocol_dilution_plan(
        "10 mM",
        targets,
        "200 uL",
        min_transfer_ul=1.0,
        allow_serial=True,
        allow_multi_dispense=True,
    )
    assert plan["status"] == "ready"
    assert plan["mode"] == "serial"
    assert plan["target_count"] == 12
    assert len(plan["intermediate_steps"]) >= 3
    assert plan["totals"]["tips_saved"] > 0
    assert plan["totals"]["multi_dispense_tips"] < plan["totals"]["single_mode_tips"]
    for step in plan["target_steps"]:
        if step["target"] != "0 mM":
            assert step["source_volume_ul"] >= 1.0
        assert step["total_volume_ul"] == pytest.approx(200.0)


def test_protocol_dilution_rejects_serial_when_disallowed():
    plan = create_protocol_dilution_plan(
        "10 mM", "10 nM", "200 uL", min_transfer_ul=1.0, allow_serial=False
    )
    assert plan["status"] == "invalid"
    assert "serial dilution" in plan["message"].lower()


def test_opentrons_protocol_supports_16_targets_serial_and_passes_simulator():
    project = new_project()
    targets = ", ".join(
        f"{val} uM"
        for val in [
            5000, 2000, 1000, 500, 250, 100, 50, 10,
            5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.01,
        ]
    )
    plan = create_protocol_dilution_plan("10 mM", targets, "200 uL")
    assert plan["status"] == "ready" and plan["target_count"] == 16

    generated = generate_opentrons_protocol(project, protocol_plan=plan)
    code = generated["source_code"]
    assert "from opentrons import protocol_api" in code
    assert '"apiLevel": "2.19"' in code
    assert "def run(protocol: protocol_api.ProtocolContext):" in code

    sim = verify_opentrons_protocol(code, try_cli=False)
    assert sim["ok"] is True, sim["errors"]
    assert sim["api_level"] == "2.19"
    assert sim["steps_executed"] > 30
    assert sim["tips_used"] == plan["totals"]["multi_dispense_tips"]
    assert sim["total_aspirated_ul"] >= sim["total_dispensed_ul"] > 0
    for well, vol in sim["well_final_volumes_ul"].items():
        assert vol == pytest.approx(200.0, abs=0.2), f"{well} volume mismatch"


def test_opentrons_96_well_full_plate_loads_multiple_tip_racks():
    project = new_project()
    targets = ", ".join(f"{10 + i * 0.5:g} uM" for i in range(96))
    plan = create_protocol_dilution_plan("1 mM", targets, "150 uL")
    assert plan["status"] == "ready" and plan["target_count"] == 96
    generated = generate_opentrons_protocol(project, protocol_plan=plan)
    sim = verify_opentrons_protocol(generated["source_code"], try_cli=False)
    assert sim["ok"] is True, sim["errors"]
    assert len(sim["well_final_volumes_ul"]) == 96
    assert sim["tips_used"] > 96


def test_opentrons_verifier_catches_real_protocol_bugs():
    bad_no_tip = """
from opentrons import protocol_api
requirements = {"apiLevel": "2.19"}
def run(protocol: protocol_api.ProtocolContext):
    plate = protocol.load_labware('corning_96_wellplate_360ul_flat', '1')
    tr = protocol.load_labware('opentrons_96_tiprack_300ul', '4')
    pip = protocol.load_instrument('p300_single_gen2', 'right', tip_racks=[tr])
    pip.aspirate(100, plate['A1'])
"""
    res1 = verify_opentrons_protocol(bad_no_tip, try_cli=False)
    assert res1["ok"] is False
    assert any("without a tip" in e.lower() for e in res1["errors"])

    bad_range = """
from opentrons import protocol_api
requirements = {"apiLevel": "2.19"}
def run(protocol: protocol_api.ProtocolContext):
    plate = protocol.load_labware('corning_96_wellplate_360ul_flat', '1')
    tr = protocol.load_labware('opentrons_96_tiprack_300ul', '4')
    pip = protocol.load_instrument('p300_single_gen2', 'right', tip_racks=[tr])
    pip.pick_up_tip()
    pip.aspirate(5, plate['A1'])
"""
    res2 = verify_opentrons_protocol(bad_range, try_cli=False)
    assert res2["ok"] is False
    assert any("outside range" in e.lower() for e in res2["errors"])

    bad_import = "import os\nrequirements = {'apiLevel': '2.19'}\ndef run(protocol):\n    pass\n"
    res3 = verify_opentrons_protocol(bad_import, try_cli=False)
    assert res3["ok"] is False
    assert any("forbidden import" in e.lower() for e in res3["errors"])


def test_jubilee_protocol_executes_simulate_subprocess():
    project = new_project()
    plan = create_protocol_dilution_plan(
        "10 mM",
        "5 mM, 1 mM, 200 uM, 50 uM, 5 uM, 0.5 uM, 50 nM",
        "200 uL",
    )
    generated = generate_jubilee_protocol(project, protocol_plan=plan)
    assert "from science_jubilee.Machine import Machine" in generated["source_code"]
    sim = verify_jubilee_protocol(generated["source_code"])
    assert sim["ok"] is True, sim["errors"]
    assert sim["steps_executed"] >= 7
    assert sim["consumed_ul"]["diluent"] > 0
    assert len(sim["destination_wells"]) == 7


def test_software_protocol_and_export_routes():
    project = _approved_project()
    client = TestClient(api.app)
    base = f"/api/projects/{project['id']}"

    dl_ot = client.get(
        f"{base}/instructions/download",
        params={"format": "opentrons", "revision": project["revision"]},
    )
    assert dl_ot.status_code == 200
    assert 'filename="opentrons_protocol.py"' in dl_ot.headers["Content-Disposition"]
    assert "def run(protocol: protocol_api.ProtocolContext):" in dl_ot.text

    dl_jb = client.get(
        f"{base}/instructions/download",
        params={"format": "jubilee", "revision": project["revision"]},
    )
    assert dl_jb.status_code == 200
    assert 'filename="jubilee_protocol.py"' in dl_jb.headers["Content-Disposition"]
    assert "science_jubilee" in dl_jb.text

    gen = client.post(
        f"{base}/software/protocol",
        json={
            "project_revision": project["revision"],
            "target_platform": "opentrons-ot2",
            "stock_concentration": "10 mM",
            "target_concentrations": "1 mM, 100 uM, 10 uM, 1 uM, 100 nM, 10 nM, 1 nM",
            "final_volume": "200 uL",
            "allow_serial": True,
            "allow_multi_dispense": True,
            "min_transfer_ul": 1.0,
        },
    )
    assert gen.status_code == 200, gen.text
    payload = gen.json()
    assert payload["plan"]["mode"] == "serial"
    assert payload["plan"]["target_count"] == 7
    assert payload["simulation"]["ok"] is True
    assert payload["simulation"]["tips_used"] == payload["plan"]["totals"]["multi_dispense_tips"]

    sim_res = client.post(
        f"{base}/software/simulate",
        json={
            "project_revision": project["revision"],
            "target_platform": "opentrons-ot2",
            "source_code": payload["source_code"],
            "try_opentrons_cli": False,
        },
    )
    assert sim_res.status_code == 200
    assert sim_res.json()["simulation"]["ok"] is True

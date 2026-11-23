"""Tests for Task 3.2 data-driven labware & standard hardware library."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from test_foundation_storage import source_project

import app.api as api
from app.agent_tools import ToolContext, execute_tool
from app.cad_exports import build_cad_files
from app.domain import apply_update
from app.fixture_adapter import compile_standalone_fixture
from app.labware_registry import (
    clear_custom_labware_registry,
    compile_labware_holder,
    get_hardware_part,
    list_hardware_parts,
    list_labware,
    register_custom_labware,
)
from app.microplates import LabwareSelection, select_plate


def _without_foundation(project: dict) -> dict:
    stripped = deepcopy(project)
    stripped["design"].pop("foundation", None)
    stripped["design"]["components"] = [
        c for c in stripped["design"]["components"] if c["id"] != "foundation-jubilee"
    ]
    return stripped


def _custom_opentrons_plate(load_name: str = "custom_12_vial_slas_plate") -> dict:
    cols = []
    wells = {}
    for c in range(4):
        col = []
        for r in range(3):
            lbl = f"{chr(65 + r)}{c + 1}"
            col.append(lbl)
            wells[lbl] = {
                "depth": 35.0,
                "shape": "circular",
                "diameter": 18.0,
                "totalLiquidVolume": 5000,
                "x": 25.0 + c * 25.0,
                "y": 65.0 - r * 22.0,
                "z": 5.0,
            }
        cols.append(col)
    return {
        "schemaVersion": 2,
        "namespace": "user_lab",
        "version": 1,
        "ordering": cols,
        "brand": {"brand": "CustomVial", "brandId": [], "links": []},
        "metadata": {
            "displayName": "Custom 12 Vial Block 5 mL",
            "displayCategory": "wellPlate",
            "displayVolumeUnits": "µL",
            "tags": [],
        },
        "dimensions": {"xDimension": 127.76, "yDimension": 85.48, "zDimension": 40.0},
        "cornerOffsetFromSlot": {"x": 0, "y": 0, "z": 0},
        "wells": wells,
        "groups": [{"metadata": {"wellBottomShape": "flat"}, "wells": list(wells)}],
        "parameters": {
            "format": "12Standard",
            "isTiprack": False,
            "isMagneticModuleCompatible": False,
            "loadName": load_name,
        },
    }


def test_builtin_labware_families_and_hardware_catalog_validate():
    clear_custom_labware_registry()
    plates = list_labware(include_tipracks=False)
    load_names = {item["load_name"] for item in plates}
    assert {
        "corning_96_wellplate_360ul_flat",
        "corning_384_wellplate_112ul_flat",
        "corning_24_wellplate_3.4ml_flat",
        "corning_48_wellplate_1.6ml_flat",
        "nest_96_wellplate_2ml_deep",
        "biorad_96_wellplate_200ul_pcr",
        "nest_12_reservoir_15ml",
        "nest_1_reservoir_195ml",
    } <= load_names

    all_items = list_labware(include_tipracks=True)
    tip_names = {item["load_name"] for item in all_items if item["is_tiprack"]}
    assert {
        "opentrons_96_tiprack_20ul",
        "opentrons_96_tiprack_300ul",
        "opentrons_96_tiprack_1000ul",
        "tipone_96_tiprack_200ul",
    } <= tip_names

    hardware = list_hardware_parts()
    assert len(hardware) >= 15
    ext = get_hardware_part("extrusion-2020")
    assert ext["category"] == "frame" and ext["dimensions_mm"][0] == 20.0


@pytest.mark.parametrize(
    "load_name,mention,expected_wells,expected_cap",
    [
        ("corning_384_wellplate_112ul_flat", "Use Corning 384 well plate", 384, 112),
        ("nest_96_wellplate_2ml_deep", "Switch to NEST 96 Deep Well Plate 2 mL", 96, 2000),
        ("biorad_96_wellplate_200ul_pcr", "Select Bio-Rad 96 Well Plate 200 uL PCR", 96, 200),
        ("corning_24_wellplate_3.4ml_flat", "Select Corning 24 Well Plate 3.4 mL", 24, 3400),
        ("corning_48_wellplate_1.6ml_flat", "Select Corning 48 Well Plate 1.6 mL", 48, 1600),
        ("nest_12_reservoir_15ml", "Select NEST 12-Channel Reservoir 15 mL", 12, 15000),
        ("nest_1_reservoir_195ml", "Select NEST 1-Well Reservoir 195 mL", 1, 195000),
    ],
)
def test_multi_family_plate_switching_and_holder_cad_export(
    load_name, mention, expected_wells, expected_cap
):
    clear_custom_labware_registry()
    base = apply_update(source_project(), {"final_volume": "100 uL"})
    selected, run = select_plate(
        base, LabwareSelection(profile_id=load_name, source=mention), mention
    )
    assert run["status"] == "accepted"
    plate = selected["labware_plate"]
    assert plate["profile_id"] == load_name
    assert plate["derived"]["well_count"] == expected_wells
    assert plate["derived"]["well_capacity_ul"] == expected_cap
    if expected_wells == 12:
        assert selected["plan"]["destination_labels"] == ["A1", "A2"]
    elif expected_wells == 1:
        assert selected["plan"]["destination_labels"] == ["A1"]
        assert plate["compatibility"]["blocking_issues"]
    else:
        assert selected["plan"]["destination_labels"] == ["A1", "B1"]

    files = build_cad_files(_without_foundation(selected))
    assert "cad/plate-holder.step" in files and "cad/plate-holder.stl" in files
    report = json.loads(files["cad/geometry-report.json"])
    assert any(c["id"] == "sample-rack" and c["valid"] for c in report["components"])


def test_custom_opentrons_v2_labware_validation_registration_and_rejection():
    clear_custom_labware_registry()
    try:
        custom = _custom_opentrons_plate()
        summary = register_custom_labware(custom)
        assert summary["load_name"] == "custom_12_vial_slas_plate"
        assert summary["well_count"] == 12
        compiled = compile_labware_holder("custom_12_vial_slas_plate")
        assert compiled["derived"]["well_capacity_ul"] == 5000

        # Select onto a workstation project directly and verify standalone fixture resolution.
        msg = "Use Custom 12 Vial Block 5 mL"
        selected, _ = select_plate(
            source_project(),
            LabwareSelection(profile_id="custom_12_vial_slas_plate", source=msg),
            msg,
            custom_definition=custom,
        )
        assert selected["labware_plate"]["labware_definition"]["parameters"]["loadName"] == (
            "custom_12_vial_slas_plate"
        )
        files = build_cad_files(_without_foundation(selected))
        assert len(files["cad/plate-holder.step"]) > 500

        fixture = compile_standalone_fixture(
            {"title": "Reservoir dock", "labware_load_name": "nest_12_reservoir_15ml"}
        )
        assert fixture["spec"]["object_length_mm"] == 127.76
        assert fixture["spec"]["object_height_mm"] == 31.4

        bad_missing_well = _custom_opentrons_plate("bad_missing")
        del bad_missing_well["wells"]["A1"]
        with pytest.raises(ValueError, match="missing ordered wells"):
            register_custom_labware(bad_missing_well)

        bad_collision = _custom_opentrons_plate("corning_384_wellplate_112ul_flat")
        with pytest.raises(ValueError, match="collides"):
            register_custom_labware(bad_collision)
    finally:
        clear_custom_labware_registry()


def test_labware_and_hardware_http_routes_and_agent_tools():
    clear_custom_labware_registry()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            r1 = await client.get("/api/labware-library", params={"category": "reservoir"})
            assert r1.status_code == 200
            assert r1.json()["count"] >= 2

            r2 = await client.get("/api/labware-library/nest_96_wellplate_2ml_deep")
            assert r2.status_code == 200
            assert r2.json()["summary"]["well_capacity_ul"] == 2000

            r3 = await client.post(
                "/api/labware-library/custom", json=_custom_opentrons_plate("api_custom_plate")
            )
            assert r3.status_code == 201
            assert r3.json()["labware"]["load_name"] == "api_custom_plate"

            r4 = await client.post("/api/labware-library/custom", json={"schemaVersion": 1})
            assert r4.status_code == 422

            r5 = await client.get("/api/hardware-library", params={"category": "motion"})
            assert r5.status_code == 200
            assert any(p["id"] == "linear-rail-mgn12h-300" for p in r5.json()["parts"])

    import asyncio

    try:
        asyncio.run(run())
        proj = apply_update(source_project(), {"final_volume": "100 uL"})
        ctx = ToolContext(project=proj, user_message="Select Bio-Rad 96 well PCR plate")
        refs = asyncio.run(
            execute_tool(
                "lookup_reviewed_references",
                {"query": "384", "library_kind": "labware"},
                ctx,
            )
        )
        assert refs.ok and any(
            i["load_name"] == "corning_384_wellplate_112ul_flat"
            for i in refs.data["labware_catalog"]
        )
        sel = asyncio.run(
            execute_tool(
                "configure_labware",
                {
                    "action": "select_microplate",
                    "plate_profile_id": "biorad_96_wellplate_200ul_pcr",
                },
                ctx,
            )
        )
        assert (
            sel.ok and ctx.project["labware_plate"]["profile_id"] == "biorad_96_wellplate_200ul_pcr"
        )
    finally:
        clear_custom_labware_registry()

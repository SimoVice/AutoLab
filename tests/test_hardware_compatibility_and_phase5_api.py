"""Tests for Task 5.3: hardware compatibility checks, Phase 5 routes, exports & agent tools."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from evolution_fixtures import layout_project
from fastapi.testclient import TestClient
from test_bom_and_print_estimator import designed_workstation
from test_concept_design import proposal as concept_proposal

from app import api
from app.agent_tools import ToolContext, execute_tool
from app.assembly import build_document_files
from app.bom_engine import build_complete_bom
from app.commercial_design import adopt_commercial
from app.concept_agent import attach_concept
from app.concept_design import compile_concept
from app.domain import apply_update, new_project
from app.fixture_adapter import compile_standalone_fixture
from app.fixture_drawings import build_fixture_package_zip
from app.hardware_compatibility import evaluate_hardware_compatibility
from app.supplier_apis import clear_supplier_api_caches
from app.upstream_docs import parse_upstream_bom


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AUTOLAB_DISABLE_DOTENV_SECRETS", "1")
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab-profile.json"))
    api._sessions.clear()
    clear_supplier_api_caches()
    yield
    api._sessions.clear()
    clear_supplier_api_caches()


def test_compatible_workstation_passes_all_deterministic_checks():
    proj = designed_workstation(cooling_enabled=True)
    report = evaluate_hardware_compatibility(proj)
    assert report["status"] == "compatible"
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["pass_count"] >= 5


@pytest.mark.parametrize("brief", [
    {},
    {"workflow_kind": "general", "brief_objective": "Measure reflected light from a tile."},
    {"workflow_kind": "liquid_preparation"},
])
def test_general_brief_needs_a_design_in_agent_and_api(brief):
    project = apply_update(new_project(), brief)
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    response = TestClient(api.app).get(
        f"/api/projects/{project['id']}/bom", params={"revision": project["revision"]}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["bom"]["lines"] == []
    assert payload["bom"]["status"] == "design_required"
    assert payload["compatibility"]["status"] == "not_evaluated"
    parts = TestClient(api.app).get(
        f"/api/projects/{project['id']}/parts", params={"revision": project["revision"]}
    ).json()
    assert parts["rows"] == [] and parts["summary"]["total_low_usd"] is None
    result = asyncio.run(execute_tool(
        "generate_purchasable_bom", {"currency": "CHF"},
        ToolContext(project=project, user_message="List parts.")
    ))
    assert result.ok and result.data["status"] == "design_required"
    assert result.data["lines"] == []


def test_general_supplies_keep_their_requirements_without_inventing_offers():
    project = apply_update(new_project(), {"workflow_kind": "general"})
    proposal = concept_proposal()
    proposal["supplies"] = [
        {"key": key, "name": name, "quantity": 3, "unit": "each", "source_id": None,
         "role": "Acquire an optical signal.", "component_keys": ["optic_head"]}
        for key, name in (
            ("detector", "Silicon photodiode"), ("isolator", "Camera trigger isolator")
        )
    ]
    project = attach_concept(project, project.copy(), compile_concept(proposal))
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    response = TestClient(api.app).get(
        f"/api/projects/{project['id']}/bom", params={"revision": project["revision"]}
    )
    assert response.status_code == 200
    bom = response.json()["bom"]
    assert bom["status"] == "incomplete"
    result = asyncio.run(execute_tool(
        "generate_purchasable_bom", {}, ToolContext(project=project, user_message="List parts.")
    ))
    assert result.data["status"] == "incomplete"
    client = TestClient(api.app)
    base = f"/api/projects/{project['id']}"
    params = {"revision": project["revision"]}
    proposal_rows = client.get(base + "/parts", params=params).json()["rows"]
    package = client.get(base + "/concept/package.zip", params=params)
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        stored_json = json.loads(archive.read("bom/purchasable-bom.json"))
        stored_csv = list(csv.DictReader(io.StringIO(archive.read("bom/purchasable-bom.csv")
                                                      .decode("utf-8"))))
    assert stored_json["lines"] == bom["lines"]
    for supply in proposal["supplies"]:
        line = next(r for r in bom["lines"] if r["label"] == supply["name"])
        assert line["quantity"] == 3 and line["unit"] == "each"
        assert line["resolution_status"] == "selection_required"
        assert all(line[k] is None for k in ("catalog_id", "supplier", "sku", "mpn", "line_total"))
        agent_line = next(r for r in result.data["lines"] if r["label"] == supply["name"])
        assert agent_line["qty"] == 3 and agent_line["unit"] == "each"
        assert agent_line["line_total"] is None and agent_line["sku"] is None
        group = "concept-" + supply["key"]
        proposal_line = next(r for r in proposal_rows if r["group_id"] == group)
        assert proposal_line["line_total_low_usd"] is None
        export_line = next(r for r in stored_csv if r["line_id"] == line["line_id"])
        assert export_line["line_total"] == export_line["sku"] == ""


def test_imported_requirements_preserve_source_identity_without_flat_rate_pricing():
    project = apply_update(new_project(), {"workflow_kind": "general"})
    project["upstream_project"] = {"bom": parse_upstream_bom({"bom.csv": (
        "name,sku,mpn,supplier,qty,unit\n"
        "Reference lamp,LAB-X17,,Example Instruments,2,each\n"
        "MGN12 rail for a 650 mm axis,,,,as needed,rail\n"
        "Cable,,,,1.5,m\n"
        "Different device,SC0917,,Example Instruments,1,each\n"
        "Identified controller,,SC0917,,1,each\n"
    )})}
    bom = build_complete_bom(project)
    assert [row["quantity"] for row in bom["lines"]] == [2, "as needed", "1.5", 1, 1]
    assert bom["summary"]["unresolved_count"] == 4
    lamp = bom["lines"][0]
    assert lamp["label"] == "Reference lamp" and lamp["requirement"]["sku"] == "LAB-X17"
    assert lamp["requirement"]["source_file"] == "bom.csv"
    assert all(row["line_total"] is None and row["supplier"] is None for row in bom["lines"][:4])
    assert bom["lines"][2]["unit"] == "m"
    assert bom["lines"][4]["catalog_id"] == "pico-h"


def test_selected_ot2_keeps_its_own_consumables_in_api_and_agent():
    project = adopt_commercial(layout_project())
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    payload = TestClient(api.app).get(
        f"/api/projects/{project['id']}/bom", params={"revision": project["revision"]}
    ).json()["bom"]
    assert {row["catalog_id"] for row in payload["lines"]} == {
        "opentrons-ot2", "opentrons-300ul-tips-pack", "corning-3596-case",
    }
    result = asyncio.run(execute_tool(
        "generate_purchasable_bom", {}, ToolContext(project=project, user_message="List parts.")
    ))
    assert result.ok and result.data["status"] == "itemized"
    assert [row["mpn"] for row in result.data["lines"]] == [r["mpn"] for r in payload["lines"]]


def test_unavailable_and_expired_supplier_observations_survive_bom_download(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app import supplier_apis

    project = apply_update(new_project(), {"workflow_kind": "general"})
    raw = concept_proposal()
    raw["supplies"] = [{
        "key": "controller", "name": "Detector readout controller", "quantity": 1,
        "unit": "board", "source_id": "pico-h", "role": "Read the detector.",
        "component_keys": ["optic_head"],
    }]
    project = attach_concept(project, project.copy(), compile_concept(raw))
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    clock = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(supplier_apis, "_now", lambda: clock)
    monkeypatch.setenv("MOUSER_API_KEY", "fixture")
    response = {"ManufacturerPartNumber": "SC0917", "MouserPartNumber": "485-5525",
                "AvailabilityInStock": "0", "LifecycleStatus": "Discontinued",
                "ProductDetailUrl": "https://www.mouser.com/ProductDetail/485-5525",
                "PriceBreaks": [{"Quantity": 1, "Price": "$5.00", "Currency": "USD"}]}
    client_type = httpx.AsyncClient
    online = True
    def supply(_):
        if online:
            return httpx.Response(200, json={"SearchResults": {"Parts": [response]}})
        return httpx.Response(503)
    transport = httpx.MockTransport(supply)
    monkeypatch.setattr(supplier_apis.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=transport))
    client = TestClient(api.app)
    base, rev = f"/api/projects/{project['id']}", project["revision"]
    params = {"revision": rev}
    def board(bom):
        return next(line for line in bom["lines"] if line["catalog_id"] == "pico-h")

    refreshed = client.post(base + "/bom/refresh", json={"project_revision": rev}).json()["bom"]
    row = board(refreshed)
    assert refreshed["status"] == "incomplete"
    assert row["mpn"] == "SC0917" and row["resolution_status"] == "catalog_identified"
    assert row["price_source"] == "unavailable" and row["availability"] == "Discontinued"
    assert row["pack_price"] is row["line_total"] is row["packs_to_order"] is None
    assert row["fetched_at"] and row["supplier"] == "Mouser"
    assert refreshed["summary"]["unavailable_count"] == 1
    download = client.get(base + "/bom/download", params={**params, "format": "json"})
    assert board(download.json()) == row
    proposal = client.get(base + "/parts", params=params).json()["rows"]
    proposal_part = next(r for r in proposal if r["group_id"] == "concept-controller")
    assert proposal_part["price_kind"] == "unavailable"
    tool = asyncio.run(execute_tool(
        "generate_purchasable_bom", {}, ToolContext(project=project, user_message="List parts.")
    ))
    agent_line = next(line for line in tool.data["lines"] if line["mpn"] == "SC0917")
    assert agent_line["availability"] == row["availability"] and agent_line["line_total"] is None

    clock += timedelta(seconds=supplier_apis.QUOTE_CACHE_TTL_SECONDS + 1)
    online = False
    failed_refresh = client.post(base + "/bom/refresh", json={"project_revision": rev})
    assert failed_refresh.status_code == 200 and failed_refresh.json()["live_quote_warnings"]
    stale = board(client.get(base + "/bom", params=params).json()["bom"])
    assert board(failed_refresh.json()["bom"]) == stale
    assert stale["price_source"] == "stale_observation" and stale["line_total"] is None
    assert stale["fetched_at"] == row["fetched_at"]
    assert stale["availability"] == "recheck_required"


def test_voltage_power_driver_csi_and_mechanical_mismatches_caught():
    proj = designed_workstation()
    bom = build_complete_bom(proj)

    # Inject 12V Peltier without 12V PSU, NEMA17+A4988, and CSI camera on Pico
    lines = [
        r
        for r in bom["lines"]
        if r["catalog_id"] not in ("pololu-1209", "drv8825")
    ]
    from app.bom_engine import _catalog_line

    lines.append(_catalog_line("nema17-17hs4401", 4, group_id="motors"))
    lines.append(_catalog_line("a4988", 3, group_id="drivers"))  # 3 < 4 motors!
    lines.append(_catalog_line("diy-peltier", 2, group_id="cooling"))  # Needs 12V & > 156W PSU!
    lines.append(_catalog_line("camera-module-3", 1, group_id="optics"))  # CSI on Pico!
    lines.append(
        _catalog_line("jubilee-xy-mount-set", 1, group_id="frame")
    )  # 31mm mount vs NEMA17 OK, let's test NEMA14 pitch mismatch separately!
    bom_bad = {**bom, "lines": lines}

    report = evaluate_hardware_compatibility(proj, bom_bad)
    assert report["status"] == "incompatible"
    ids = {c["id"]: c for c in report["checks"]}
    assert ids["voltage-diy-peltier"]["severity"] == "error"
    assert ids["driver-channel-count"]["severity"] == "error"
    assert ids["power-budget"]["severity"] == "error"
    assert ids["camera-csi-interface"]["severity"] == "error"
    assert ids["camera-csi-interface"]["recommended_fix"]

    # Check warning when driver channel count == motor count (4) but A4988 uncooled (1.0A) < 1.7A
    lines_warn = [
        _catalog_line("a4988", 4, group_id="drivers") if r["catalog_id"] == "a4988" else r
        for r in lines
    ]
    report_warn = evaluate_hardware_compatibility(proj, {**bom, "lines": lines_warn})
    warn_ids = {c["id"]: c for c in report_warn["checks"]}
    assert warn_ids["driver-current-rating"]["severity"] == "warning"

    # Check NEMA 14 motor (26mm pitch) on Jubilee NEMA 17 motor mount (31mm pitch) -> error
    lines_mech = [
        _catalog_line("pololu-1209", 4, group_id="motors"),
        _catalog_line("drv8825", 4, group_id="drivers"),
        _catalog_line("pico", 1, group_id="controller"),
        _catalog_line("psu-lrs-150-24", 1, group_id="power"),
        _catalog_line("buck-5v-3a", 1, group_id="power"),
        _catalog_line("jubilee-xy-mount-set", 1, group_id="frame"),
    ]
    rep_mech = evaluate_hardware_compatibility(proj, {**bom, "lines": lines_mech})
    mech_ids = {c["id"]: c for c in rep_mech["checks"]}
    assert mech_ids["mech-pitch-jubilee-xy-mount-set"]["severity"] == "error"


def test_printer_bed_overflow_and_material_warning_caught():
    fx_proj = new_project()
    fx_proj["standalone_fixture"] = compile_standalone_fixture(
        {
            "title": "Large Slide Holder",
            "fixture_kind": "sample_holder",
            "object_label": "Oversize Tray",
            "object_shape": "rectangular",
            "object_length_mm": 210.0,
            "object_width_mm": 120.0,
            "object_height_mm": 15.0,
            "material": "ABS",
        }
    )
    lab_profile = {
        "printer": {
            "name": "Bambu Lab A1 mini",
            "build_volume_mm": [180.0, 180.0, 180.0],
            "nozzle_mm": 0.4,
            "materials": ["PLA", "PETG"],
        }
    }
    report = evaluate_hardware_compatibility(fx_proj, lab_profile=lab_profile)
    assert report["status"] == "incompatible"
    assert any(
        c["category"] == "printer_fabrication" and c["severity"] == "error"
        for c in report["checks"]
    )
    assert any(
        c["category"] == "printer_fabrication" and c["severity"] == "warning"
        for c in report["checks"]
    )


def test_phase5_http_routes_fx_download_and_zip_exports(monkeypatch: pytest.MonkeyPatch):
    fx_proj = apply_update(new_project(), {"workflow_kind": "general"})
    fx_proj["standalone_fixture"] = compile_standalone_fixture(
        {
            "title": "Plate Rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
            "include_camera_mount": True,
            "camera_mount_type": "pi_camera_v2_v3",
        }
    )
    api._sessions[fx_proj["id"]] = api.ProjectSession(project=fx_proj)
    client = TestClient(api.app)
    pid, rev = fx_proj["id"], fx_proj["revision"]

    # Zero-network GET /bom USD
    get_res = client.get(f"/api/projects/{pid}/bom", params={"revision": rev, "currency": "USD"})
    assert get_res.status_code == 200
    payload = get_res.json()
    assert payload["bom"]["summary"]["unpriced_placeholder_count"] == 0
    assert payload["compatibility"]["status"] == "compatible"

    # Cold GET CHF without FX refresh returns 409 honestly
    assert client.get(
        f"/api/projects/{pid}/bom", params={"revision": rev, "currency": "CHF"}
    ).status_code == 409

    # Mock ECB FX + Mouser + Adafruit/Shopify in POST /bom/refresh
    monkeypatch.setenv("MOUSER_API_KEY", "mock-mouser-key")
    orig_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.frankfurter.dev" in url:
            return httpx.Response(
                200,
                json={"base": "USD", "date": "2026-11-24", "rates": {"CHF": 0.9, "EUR": 0.95}},
            )
        if "api.mouser.com" in url:
            return httpx.Response(
                200,
                json={
                    "SearchResults": {
                        "Parts": [
                            {
                                "ManufacturerPartNumber": "SC0872",
                                "MouserPartNumber": "485-5657",
                                "Description": "Raspberry Pi Camera Module 3",
                                "AvailabilityInStock": "75 In Stock",
                                "LeadTime": "5 Days",
                                "ProductDetailUrl": "https://www.mouser.com/485-5657",
                                "PriceBreaks": [
                                    {"Quantity": 1, "Price": "$25.00", "Currency": "USD"}
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    from app.supplier_catalogs import CatalogError

    async def _raise_offline(*_a, **_kw):
        raise CatalogError("mock offline html skipped")

    monkeypatch.setattr(
        "app.supplier_apis.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("app.supplier_catalogs.fetch_product", _raise_offline)

    ref_res = client.post(
        f"/api/projects/{pid}/bom/refresh",
        json={"project_revision": rev, "currency": "CHF", "use_official_apis": True},
    )
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert ref_data["bom"]["currency"] == "CHF"
    assert ref_data["bom"]["fx_rate_used"] == 0.9
    assert ref_data["bom"]["summary"]["live_api_priced_count"] >= 1

    # BOM CSV download + compatibility GET + supplier search POST
    dl_csv = client.get(
        f"/api/projects/{pid}/bom/download",
        params={"revision": rev, "format": "csv", "currency": "CHF"},
    )
    assert dl_csv.status_code == 200
    csv_rows = list(csv.DictReader(io.StringIO(dl_csv.text)))
    assert len(csv_rows) == ref_data["bom"]["summary"]["line_count"]

    compat_res = client.get(f"/api/projects/{pid}/compatibility", params={"revision": rev})
    assert compat_res.status_code == 200 and compat_res.json()["status"] == "compatible"

    search_res = client.post(
        "/api/suppliers/search", json={"query": "Camera Module 3", "currency": "CHF"}
    )
    assert search_res.status_code == 200
    assert search_res.json()["currency"] == "CHF"

    # ZIP exports include purchasable BOM + compatibility report
    z_bytes = build_fixture_package_zip(fx_proj)
    with zipfile.ZipFile(io.BytesIO(z_bytes)) as zf:
        names = set(zf.namelist())
        assert {
            "bom/purchasable-bom.json",
            "bom/purchasable-bom.csv",
            "bom/hardware-compatibility.json",
        } <= names

    ws = designed_workstation()
    ws_files = build_document_files(ws)
    assert "bom/purchasable-bom.csv" in ws_files
    assert "bom/hardware-compatibility.json" in ws_files


def test_autonomous_agent_calls_bom_and_compatibility_tools(monkeypatch: pytest.MonkeyPatch):
    from app.agent_loop import run_agent_turn
    from app.model_client import ModelSettings

    proj = designed_workstation()
    calls = 0

    class MockOA(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                tc = {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "generate_purchasable_bom",
                        "arguments": json.dumps({"currency": "USD", "refresh_live_prices": False}),
                    },
                }
            elif calls == 2:
                tc = {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "check_hardware_compatibility",
                        "arguments": json.dumps({"include_passed_checks": True}),
                    },
                }
            else:
                return httpx.Response(
                    200,
                    json={
                        "model": "gpt-test",
                        "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "BOM generated and compatibility checks passed.",
                                },
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": "gpt-test",
                    "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {"role": "assistant", "content": None, "tool_calls": [tc]},
                        }
                    ],
                },
            )

    async def _run():
        settings = ModelSettings(
            api_key="test", base_url="https://mock.openai.local/v1", model="gpt-test"
        )
        async with httpx.AsyncClient(transport=MockOA()) as client:
            return await run_agent_turn(
                proj, "Check compatibility and generate a purchasable BOM in USD.", [],
                client=client, settings=settings,
            )

    res = asyncio.run(_run())
    assert [t["tool"] for t in res["tool_trace"]] == [
        "generate_purchasable_bom",
        "check_hardware_compatibility",
    ]

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
from fastapi.testclient import TestClient

from app import api
from app.assembly import build_document_files
from app.bom_engine import build_complete_bom
from app.domain import apply_update, new_project
from app.fixture_adapter import compile_standalone_fixture
from app.fixture_drawings import build_fixture_package_zip
from app.hardware_compatibility import evaluate_hardware_compatibility
from app.supplier_apis import clear_supplier_api_caches


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
    proj = apply_update(
        new_project(),
        {"workflow_kind": "liquid_preparation", "cooling_enabled": True},
    )
    report = evaluate_hardware_compatibility(proj)
    assert report["status"] == "compatible"
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["pass_count"] >= 5


def test_voltage_power_driver_csi_and_mechanical_mismatches_caught():
    proj = apply_update(new_project(), {"workflow_kind": "liquid_preparation"})
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

    ws = apply_update(new_project(), {"workflow_kind": "liquid_preparation"})
    ws_files = build_document_files(ws)
    assert "bom/purchasable-bom.csv" in ws_files
    assert "bom/hardware-compatibility.json" in ws_files


def test_autonomous_agent_calls_bom_and_compatibility_tools(monkeypatch: pytest.MonkeyPatch):
    from app.agent_loop import run_agent_turn
    from app.model_client import ModelSettings

    proj = apply_update(new_project(), {"workflow_kind": "liquid_preparation"})
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

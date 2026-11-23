"""Tests for Task 5.2: purchasable BOM engine, analytical slicer estimator & search integration."""

from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest

from app.bench_station import StationProposal, compile_station
from app.bom_engine import (
    bom_to_csv,
    bom_to_json,
    build_complete_bom,
    load_purchasable_catalog,
)
from app.concept_design import compile_concept
from app.domain import apply_update, new_project
from app.fixture_adapter import compile_standalone_fixture
from app.foundation_design import adopt_foundation, foundation_sourcing
from app.print_estimator import MATERIAL_SPECS, estimate_fdm_part
from app.supplier_apis import clear_supplier_api_caches, search_official_suppliers


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTOLAB_DISABLE_DOTENV_SECRETS", "1")
    clear_supplier_api_caches()
    yield
    clear_supplier_api_caches()


def test_fdm_print_estimator_calculates_filament_time_cost_and_bed_fit():
    profile = {
        "printer": {
            "name": "Bambu Lab A1 mini",
            "build_volume_mm": [180.0, 180.0, 180.0],
            "nozzle_mm": 0.4,
            "materials": ["PLA", "PETG"],
        }
    }
    est = estimate_fdm_part(
        "sample-holder",
        "96-Well Locating Nest",
        [140.0, 96.0, 16.0],
        material="PETG",
        infill_percent=25.0,
        quantity=2,
        lab_profile=profile,
        currency="CHF",
        fx_rates={"base": "USD", "rates": {"USD": 1.0, "CHF": 0.9, "EUR": 0.95}},
    )
    assert est["fits_printer_bed"] is True
    assert est["material_available_in_lab"] is True
    assert est["filament_mass_g"] > 10.0
    assert est["print_time_minutes"] > 15.0
    assert est["material_cost"] == round(est["material_cost_usd"] * 0.9, 2)

    oversize = estimate_fdm_part(
        "huge-tray", "Oversize Tray", [250.0, 140.0, 15.0], lab_profile=profile
    )
    assert oversize["fits_printer_bed"] is False
    assert set(MATERIAL_SPECS) >= {"PLA", "PETG", "ABS", "ASA", "TPU", "PA-CF"}


def test_purchasable_catalog_integrity():
    cat = load_purchasable_catalog()
    assert cat["schema_version"] == "1"
    for item_id, item in cat["items"].items():
        assert item["id"] == item_id
        assert item["reference_unit_price_usd"] > 0
        assert item["pack_size"] >= 1
        assert isinstance(item["specs"], dict) and item["specs"].get("kind")


def test_complete_bom_eliminates_all_unpriced_placeholders_across_all_5_routes():
    fx = {
        "base": "USD",
        "rates": {"USD": 1.0, "CHF": 0.9, "EUR": 0.95},
        "rate_date": "2026-11-24",
        "source": "ECB",
    }

    # 1. Standalone fixture + Pi camera
    fx_proj = new_project()
    fx_proj["standalone_fixture"] = compile_standalone_fixture(
        {
            "title": "Plate Rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
            "include_camera_mount": True,
            "camera_mount_type": "pi_camera_v2_v3",
        }
    )
    bom_fx = build_complete_bom(fx_proj, currency="CHF", fx_rates=fx)
    assert bom_fx["summary"]["unpriced_placeholder_count"] == 0
    assert bom_fx["summary"]["printed_line_count"] >= 3
    assert bom_fx["summary"]["total_filament_mass_g"] > 0
    assert bom_fx["currency"] == "CHF"

    # 2. Custom workstation (formerly 5 unpriced placeholders!)
    ws = apply_update(
        new_project(),
        {"workflow_kind": "liquid_preparation", "cooling_enabled": True},
    )
    bom_ws = build_complete_bom(ws, currency="EUR", fx_rates=fx)
    assert bom_ws["summary"]["unpriced_placeholder_count"] == 0
    groups_ws = {line["group_id"] for line in bom_ws["lines"]}
    assert {"frame", "linear-motion", "pipette", "power", "cooling", "fixtures"} <= groups_ws

    # 3. Jubilee foundation workstation
    jub = adopt_foundation(new_project(), bed_drop_mm=25.0)
    jub["sourcing"] = foundation_sourcing(jub)
    bom_jub = build_complete_bom(jub, currency="USD")
    assert bom_jub["summary"]["unpriced_placeholder_count"] == 0
    assert any(line["catalog_id"] == "duet3-mini5plus" for line in bom_jub["lines"])

    # 4. Concept design with 2020 extrusion + printed cradle
    conc = new_project()
    conc["concept_design"] = compile_concept(
        {
            "title": "Extrusion Stand",
            "overview": "2020 gantry frame with printed cradle",
            "components": [
                {
                    "key": "post",
                    "name": "2020 Aluminum Post",
                    "role": "Vertical post",
                    "source_id": None,
                    "assembly": "Bolt post",
                    "check": "Squareness",
                    "primitives": [
                        {
                            "shape": "extrusion",
                            "profile": "2020",
                            "size_mm": [20.0, 200.0, 20.0],
                            "center_mm": [0.0, 100.0, 0.0],
                            "finish": "frame",
                        }
                    ],
                },
                {
                    "key": "holder",
                    "name": "Printed Sample Holder",
                    "role": "Locates sample",
                    "source_id": None,
                    "assembly": "Slide on post",
                    "check": "Fit",
                    "primitives": [
                        {
                            "shape": "box",
                            "size_mm": [60.0, 18.0, 60.0],
                            "center_mm": [0.0, 9.0, 0.0],
                            "finish": "polymer",
                        }
                    ],
                },
            ],
            "steps": [
                {
                    "name": "Inspect holder",
                    "instruction": "Slide tray out for loading",
                    "component_keys": ["holder"],
                    "moving_key": "holder",
                    "translation_mm": [20.0, 0.0, 0.0],
                    "rotation_deg": 0.0,
                }
            ],
            "software": {
                "controller": "Manual bench positioning with serial telemetry",
                "interfaces": ["USB"],
                "setup": ["Connect controller over USB"],
                "checks": ["Verify limit clearance"],
            },
            "open_questions": ["Confirm bench bolt spacing"],
        }
    )
    bom_conc = build_complete_bom(conc)
    assert bom_conc["summary"]["unpriced_placeholder_count"] == 0
    assert any(line["catalog_id"] == "extrusion-2020-500" for line in bom_conc["lines"])
    assert any(line["kind"] == "3d_printed" for line in bom_conc["lines"])

    # 5. Bench station
    bs = new_project()
    bs["bench_station"] = compile_station(
        StationProposal(operations=["weigh", "read_barcode", "print_label"])
    )
    bom_bs = build_complete_bom(bs)
    assert bom_bs["summary"]["unpriced_placeholder_count"] == 0
    assert bom_bs["summary"]["commercial_line_count"] == 3


def test_bom_keeps_live_api_separate_from_curated_reference_and_exports_safe_csv():
    proj = apply_update(new_project(), {"workflow_kind": "liquid_preparation"})
    live_quotes = {
        "pico": {
            "currency": "USD",
            "availability": "InStock",
            "stock_quantity": 99,
            "lead_time_days": 3,
            "price_breaks": [{"minimum_packs": 1, "pack_price": "4.50"}],
            "source_url": "https://www.mouser.com/485-4864",
            "fetched_at": "2026-11-24T00:00:00Z",
            "adapter": "mouser_v2",
        }
    }
    bom = build_complete_bom(proj, live_quotes=live_quotes)
    assert bom["summary"]["live_api_priced_count"] == 1
    assert bom["summary"]["live_api_subtotal"] == 4.5
    assert bom["summary"]["reference_catalog_subtotal"] > 0
    assert bom["summary"]["printed_material_subtotal"] > 0

    parsed_json = json.loads(bom_to_json(bom))
    assert parsed_json["project_revision"] == proj["revision"]

    bom["lines"][0]["notes"] = "=SUM(A1:A2)"
    csv_rows = list(csv.DictReader(io.StringIO(bom_to_csv(bom).decode("utf-8"))))
    assert csv_rows[0]["notes"] == "'=SUM(A1:A2)"


def test_search_official_suppliers_searches_local_catalog_and_reports_missing_keys():
    res = asyncio.run(search_official_suppliers("Mean Well 24V power supply"))
    assert res["api_capabilities"]["digikey_v4"]["configured"] is False
    assert len(res["catalog_matches"]) >= 1
    assert any("LRS" in m["mpn"] for m in res["catalog_matches"])

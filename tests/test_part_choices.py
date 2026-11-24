"""Regression cases for choices, inventory and evidence on independent BOM requirements."""

import asyncio
import csv
import io
import json
import zipfile
from copy import deepcopy

import pytest
from test_custom_project_routes import project_fixture, proposal

from app.agent_tools import ToolContext, execute_tool
from app.bom_engine import bom_to_csv, build_complete_bom, reconcile_part_choices
from app.concept_design import compile_concept
from app.concept_files import render_asset
from app.design_workflow import approve_machine, workflow_state
from app.hardware_compatibility import evaluate_hardware_compatibility
from app.parts_proposal import build_parts_proposal
from app.price_estimation import price_context
from app.procurement import initial_procurement_rows
from app.project_storage import make_snapshot, restore_snapshot
from app.supplier_apis import clear_supplier_api_caches
from app.upstream_docs import parse_upstream_bom

LINE = "concept:controller_board"


@pytest.fixture
def project(monkeypatch):
    monkeypatch.setenv("AUTOLAB_DISABLE_DOTENV_SECRETS", "1")
    clear_supplier_api_caches()
    p = project_fixture()
    raw = proposal()
    raw["supplies"] = [
        {
            **raw["supplies"][0],
            "name": "Readout controller",
            "unit": "item",
            "source_id": None,
            "critical_specs": [
                {"key": "logic_voltage_v", "value": "3.3", "unit": "V"},
                {"key": "available_gpio", "comparison": "at_least", "value": "20"},
            ],
        }
    ]
    p["concept_design"] = compile_concept(raw, 600)
    yield p
    clear_supplier_api_caches()


def edit(ctx, action, message, line_id=LINE, **fields):
    ctx.user_message = message
    args = {"action": action, "line_id": line_id, "user_quote": message, **fields}
    return asyncio.run(execute_tool("choose_requirement_part", args, ctx))


def row(project, **kwargs):
    return next(r for r in build_complete_bom(project, **kwargs)["lines"] if r["line_id"] == LINE)


def test_part_changes_pack_rounding_inventory_approval_and_portable_outputs(project):
    ctx = ToolContext(approve_machine(project), "")
    base = build_complete_bom(ctx.project)["summary"]["documented_subtotal"]
    assert edit(ctx, "propose", "Compare readout boards.", catalog_id="pico-h").ok
    assert workflow_state(ctx.project)["machine_approved"]
    assert row(ctx.project)["line_total"] is None
    selection = "Use SC0917. I have 1 unit on hand."
    assert edit(ctx, "select", selection, catalog_id="pico-h", owned_quantity=1).ok
    assert not workflow_state(ctx.project)["machine_approved"]
    line = row(ctx.project)
    assert line["part_choice"]["accepted"]["interface_status"] == "matched"
    assert (line["quantity"], line["owned_quantity"], line["purchase_quantity"]) == (3, 1, 2)
    assert line["line_total"] == 10
    assert build_complete_bom(ctx.project)["summary"]["documented_subtotal"] == (base or 0) + 10
    version = ctx.project["revision"]
    assert not edit(ctx, "select", selection, catalog_id="pico-h", owned_quantity=1).mutated
    assert ctx.project["revision"] == version
    two_pack = {
        "pico-h": {
            "price_breaks": [{"minimum_packs": 1, "pack_price": "9.00"}],
            "pack_size": 2,
            "availability": "InStock",
        }
    }
    stamp = "2026-11-24T12:00:00Z"
    observed = {"source_url": "https://www.adafruit.com/product/5525", "fetched_at": stamp}
    two_pack["pico-h"].update(observed)
    packed = row(ctx.project, live_quotes=two_pack)
    assert (packed["packs_to_order"], packed["line_total"]) == (1, 9)
    missing = row(ctx.project, live_quotes={"pico-h": {**observed, "availability": "OutOfStock"}})
    assert missing["line_total"] is None and missing["resolution_status"] == "user_selected"
    proc = next(
        r for r in initial_procurement_rows(ctx.project) if r["group_id"] == line["group_id"]
    )
    assert (proc["selected_id"], proc["quantity"]) == ("pico-h", 2)
    ctx.project = approve_machine(ctx.project)
    assert edit(
        ctx, "select", "Use SC0917. I own 3 units.", catalog_id="pico-h", owned_quantity=3
    ).ok
    assert workflow_state(ctx.project)["machine_approved"]
    owned = row(ctx.project)
    assert (owned["price_source"], owned["unit_price"], owned["line_total"]) == (
        "lab_inventory",
        None,
        0,
    )
    assert build_complete_bom(ctx.project)["summary"]["documented_subtotal"] == base
    assert not any(r["group_id"] == line["group_id"] for r in price_context(ctx.project)["rows"])
    expected = next(
        r for r in build_parts_proposal(ctx.project)["rows"] if r["group_id"] == line["group_id"]
    )
    assert (
        expected["quantity"],
        expected["purchase_quantity"],
        expected["line_total_high_usd"],
    ) == (3, 0, 0)
    snapshot = make_snapshot(ctx.project, [])
    restored, _ = restore_snapshot(snapshot)
    assert row(restored) == owned and workflow_state(restored)["machine_approved"]
    bom = build_complete_bom(restored)
    payload, _ = render_asset(
        restored["concept_design"],
        restored["id"],
        restored["revision"],
        "package.zip",
        purchasing_bom=bom,
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read("bom/purchasable-bom.json"))["lines"] == bom["lines"]
        assert archive.read("bom/purchasable-bom.csv") == bom_to_csv(bom)
        csv_row = next(
            r
            for r in csv.DictReader(io.StringIO(archive.read("parts.csv").decode("utf-8-sig")))
            if r["Part"] == "Readout controller"
        )
        assert (csv_row["Selected part number"], csv_row["Units to purchase"]) == ("5525", "0")
    snapshot["part_choices"][LINE]["accepted"]["line_total"] = 1
    with pytest.raises(ValueError):
        restore_snapshot(snapshot)


def test_external_substitution_conflicts_missing_facts_and_reopening(project):
    ctx = ToolContext(project, "")
    assert edit(ctx, "select", "Use SC0917.", catalog_id="pico-h").ok
    wrong = "Compare Example Lab RX-2, item, 5 V."
    args = dict(
        issuer="Example Lab",
        part_number="RX-2",
        unit="item",
        facts=[{"key": "logic_voltage_v", "value": "5", "unit": "V"}],
    )
    proposed = edit(ctx, "propose", wrong, **args)
    assert proposed.ok
    candidates = proposed.data["line"]["part_choice"]["candidates"]
    assert candidates[-1]["interface_status"] == "mismatch"
    original = deepcopy(ctx.project)
    assert not edit(ctx, "select", "Use RX-2.", issuer="Example Lab", part_number="RX-2").ok
    assert not edit(ctx, "select", "Yes.").ok
    assert not edit(ctx, "select", "Do not use SC0917.", catalog_id="pico-h").ok
    assert ctx.project == original
    choice = "Replace SC0917 with Example Lab RX-3, item, from https://example.org/rx3."
    assert edit(
        ctx,
        "select",
        choice,
        issuer="Example Lab",
        part_number="RX-3",
        unit="item",
        source_url="https://example.org/rx3",
    ).ok
    picked = row(ctx.project)
    assert picked["price_source"] == "quote_required" and picked["line_total"] is None
    assert picked["part_choice"]["accepted"]["interface_status"] == "unverified"
    assert evaluate_hardware_compatibility(ctx.project)["warning_count"] > 0
    snapshot = make_snapshot(ctx.project, [])
    revived, _ = restore_snapshot(snapshot)
    assert row(revived) == picked
    assert edit(ctx, "clear", "Remove the selected component.").ok
    assert row(ctx.project)["resolution_status"] == "selection_required"


def test_existing_imported_line_accepts_specs_and_external_lab_inventory():
    p = project_fixture(with_design=False)
    p["upstream_project"] = {
        "provenance": {"repo_full_name": "example/optics", "commit_sha": "a" * 40},
        "bom": parse_upstream_bom({"bom.csv": "name,quantity,unit\nPhotodiode,2,each\n"}),
    }
    ctx = ToolContext(p, "")
    line_id = build_complete_bom(p)["lines"][0]["line_id"]
    criteria = [{"key": "diameter_mm", "value": "5", "unit": "mm"}]
    assert edit(
        ctx, "specify", "We require a diameter of 5 mm.", line_id, required_specs=criteria
    ).ok
    choice = "Use Optics Co PD-A, each. Diameter 5 mm, https://example.org/pd-a. I own 2."
    args = dict(
        issuer="Optics Co",
        part_number="PD-A",
        source_url="https://example.org/pd-a",
        unit="each",
        facts=criteria,
        owned_quantity=2,
    )
    result = edit(ctx, "select", choice, line_id, **args)
    assert result.ok, result.summary
    line = result.data["line"]
    assert (line["part_choice"]["accepted"]["interface_status"], line["purchase_quantity"]) == (
        "matched",
        0,
    )
    assert line["requirement"]["source_file"] == "bom.csv"
    forged = {**args, "source_url": "https://example.org/other"}
    assert not edit(ctx, "select", choice, line_id, **forged).ok
    revised = edit(
        ctx,
        "specify",
        "Change the required diameter to 6 mm.",
        line_id,
        required_specs=[{**criteria[0], "value": "6"}],
    )
    assert revised.ok
    assert revised.data["line"]["resolution_status"] == "selection_required"
    assert revised.data["line"]["part_choice"]["candidates"][0]["interface_status"] == "mismatch"
    ctx.project["upstream_project"]["provenance"]["commit_sha"] = "b" * 40
    reconcile_part_choices(ctx.project)
    assert not ctx.project["part_choices"]


def test_catalog_conflict_cannot_auto_price_and_stale_choices_do_not_migrate(project):
    ctx = ToolContext(project, "")
    assert edit(ctx, "select", "Use SC0917.", catalog_id="pico-h").ok
    p = ctx.project
    original = deepcopy(p["concept_design"]["proposal"])
    revised = deepcopy(original)
    revised["supplies"][0].update(
        source_id="pico-h",
        critical_specs=[
            {"key": "logic_voltage_v", "comparison": "equals", "value": "5", "unit": "V"}
        ],
    )
    p["concept_design"] = compile_concept(revised, 600)
    line = row(p)
    assert (
        line["line_total"] is None and line["reference_candidate"]["interface_status"] == "mismatch"
    )
    reconcile_part_choices(p)
    p["concept_design"] = compile_concept(original, 600)
    assert not row(p).get("part_choice")


def test_supplier_search_binds_exact_distributor_sku_and_requires_a_current_offer(project):
    ctx = ToolContext(project, "")
    ctx.supplier_hits = [
        {"supplier": "Mouser", "sku": "1-EX-2", "mpn": "EX", "source_url": "https://example.org/2"},
        {"supplier": "Mouser", "sku": "1-EX-1", "mpn": "EX", "source_url": "https://example.org/1"},
    ]
    assert not edit(ctx, "propose", "Compare these parts.", issuer="Mouser", part_number="EX").ok
    result = edit(ctx, "propose", "Compare these parts.", issuer="Mouser", part_number="1-EX-1")
    assert result.ok, result.summary
    chosen = edit(ctx, "select", "Yes, use that candidate.")
    assert chosen.ok, chosen.summary
    line = chosen.data["line"]
    assert (line["sku"], line["mpn"], line["source_url"]) == (
        "1-EX-1",
        "EX",
        "https://example.org/1",
    )
    assert line["price_source"] == "quote_required" and line["line_total"] is None


def test_shared_manufacturer_code_requires_distributor_variant(project):
    ctx = ToolContext(project, "")
    for sku in ["PK-A", "PK-B"]:
        ctx.supplier_hits.append(
            {
                "supplier": "Mouser",
                "sku": sku,
                "mpn": "SHARED",
                "pack_size": 1,
                "source_url": "https://example.org/" + sku,
            }
        )
        assert edit(
            ctx, "propose", "Compare the two variants.", issuer="Mouser", part_number=sku
        ).ok
    assert not edit(ctx, "select", "Use SHARED.", issuer="Mouser", part_number="PK-A").ok
    assert edit(ctx, "select", "Use PK-B.", issuer="Mouser", part_number="PK-B").ok
    line = row(ctx.project)
    quote = {
        "pack_size": 1,
        "sku": "PK-B",
        "source_url": "https://example.org/pk-b",
        "fetched_at": "2026-11-24T12:00:00Z",
        "price_breaks": [{"minimum_packs": 1, "pack_price": "4"}],
    }
    priced = row(ctx.project, live_quotes={line["offer_id"]: quote})
    assert (priced["sku"], priced["line_total"]) == ("PK-B", 12)


def test_clear_or_disqualify_original_reference_drops_its_procurement_scope(project):
    raw = deepcopy(project["concept_design"]["proposal"])
    raw["supplies"][0]["source_id"] = "pico-h"
    project["concept_design"] = compile_concept(raw, 600)
    for action in ("clear", "specify"):
        ctx = ToolContext(deepcopy(project), "")
        if action == "clear":
            result = edit(ctx, action, "Leave the controller choice open.")
        else:
            result = edit(
                ctx,
                action,
                "Change the required logic voltage to 5V.",
                required_specs=[{"key": "logic_voltage_v", "value": "5", "unit": "V"}],
            )
        assert result.ok, result.summary
        line = result.data["line"]
        assert line["resolution_status"] == "selection_required" and line["line_total"] is None
        proc = next(
            r for r in initial_procurement_rows(ctx.project) if r["group_id"] == line["group_id"]
        )
        assert not proc["catalog_supported"] and proc["selected_id"] != "pico-h"
        revived, _ = restore_snapshot(make_snapshot(ctx.project, []))
        assert row(revived) == line


def test_fractional_stock_and_unspecified_import_quantities_survive_reopen():
    project = project_fixture(with_design=False)
    project["upstream_project"] = {
        "provenance": {"repo_full_name": "example/bench", "commit_sha": "b" * 40},
        "bom": parse_upstream_bom(
            {"bom.csv": "name,quantity,unit\nCable,1.5,m\nSleeve,as needed,m\n"}
        ),
    }
    ctx = ToolContext(project, "")
    cable, sleeve = build_complete_bom(project)["lines"]
    chosen = edit(
        ctx,
        "select",
        "Use Lab Supply C-7, m. I own 0.5 m.",
        cable["line_id"],
        issuer="Lab Supply",
        part_number="C-7",
        unit="m",
        owned_quantity=0.5,
    )
    assert chosen.ok, chosen.summary
    assert chosen.data["line"]["purchase_quantity"] == 1
    unknown = edit(
        ctx,
        "select",
        "Use Lab Supply H-2, m.",
        sleeve["line_id"],
        issuer="Lab Supply",
        part_number="H-2",
        unit="m",
    )
    assert unknown.ok, unknown.summary
    assert unknown.data["line"]["price_source"] == "quantity_required"
    assert unknown.data["line"]["purchase_quantity"] is None
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []))
    assert build_complete_bom(restored)["lines"] == build_complete_bom(ctx.project)["lines"]


def test_same_motor_family_with_different_current_rating(project):
    raw = deepcopy(project["concept_design"]["proposal"])
    raw["supplies"][0].update(
        name="NEMA 14 stepper motor",
        critical_specs=[
            {"key": "mounting_pitch_mm", "value": "26", "unit": "mm"},
            {"key": "rated_current_a", "comparison": "at_most", "value": "0.6", "unit": "A"},
        ],
    )
    project["concept_design"] = compile_concept(raw, 600)
    ctx = ToolContext(project, "")
    assert edit(ctx, "select", "Use Pololu 1208.", catalog_id="pololu-1208").ok
    comparison = edit(
        ctx, "propose", "Compare it with the other NEMA 14.", catalog_id="pololu-1209"
    )
    other = comparison.data["line"]["part_choice"]["candidates"][-1]
    assert other["interface_status"] == "mismatch"
    assert [c["key"] for c in other["checks"] if c["status"] == "mismatch"] == ["rated_current_a"]
    assert not edit(ctx, "select", "Use Pololu 1209.", catalog_id="pololu-1209").ok
    assert row(ctx.project)["part_choice"]["accepted"]["part_number"] == "1208"

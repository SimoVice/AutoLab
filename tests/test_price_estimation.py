"""Evidence-backed budgets, freshness, whole lines and revision bindings."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app import procurement
from app.agent_tools import ToolContext, execute_tool
from app.bom_engine import build_complete_bom
from app.budget_allowances import LineBudget, reconcile_budgets
from app.domain import new_project
from app.price_estimation import purchase_cost_report
from app.project_storage import make_snapshot, restore_snapshot


def project_fixture():
    project = new_project()
    project["engineering_design"] = {
        "schema_version": "offline-cost-unit-fixture",
        "proposal": {"rationale": "Synthetic offline cost-test layout."},
        "provenance": {"model": "offline-fixture"},
    }
    project["sourcing"]["groups"] = [
        g for g in project["sourcing"]["groups"] if g["id"] in {"controller", "fixtures"}
    ]
    return project


def supplier_report(project, *, price=12.0):
    rows = procurement.initial_procurement_rows(project)
    now = datetime.now(UTC)
    row = next(row for row in rows if row["group_id"] == "controller")
    row.update(
        status="priced",
        freshness="fresh",
        line_total=price,
        unit_price=price,
        fetched_at=now.isoformat(),
        pack_price=price,
        issue=None,
    )
    return {
        "schema_version": 1,
        "report_id": "offline-supplier-report",
        "project_id": project["id"],
        "project_revision": project["revision"],
        "market": "US",
        "currency": "USD",
        "fetched_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "rows": rows,
        "exclusions": [],
    }


def declare(project, line_id, low=80, high=120, quote="Allow USD 80–120 for this line."):
    ctx = ToolContext(project=project, user_message=quote)
    args = {"line_id": line_id, "budget": {"low_usd": low, "high_usd": high, "user_quote": quote}}
    outcome = asyncio.run(execute_tool("set_line_budget", args, ctx))
    assert outcome.ok, outcome.summary
    return ctx.project


@pytest.mark.parametrize(
    ("quote", "low", "high"),
    [
        ("USD 80–120", 80, 120),
        ("$80 to $120", 80, 120),
        ("$80-$120", 80, 120),
        ("80-120 USD", 80, 120),
        ("USD 4.25", 4.25, 4.25),
        ("USD 0", 0, 0),
        ("$1,200–1,500", 1200, 1500),
    ],
)
def test_supported_numeric_declarations(quote, low, high):
    assert LineBudget(low_usd=low, high_usd=high, user_quote=quote).low_usd == low


@pytest.mark.parametrize(
    ("quote", "low", "high"),
    [
        ("Estimate this camera", 80, 120),
        ("EUR 80–120", 80, 120),
        ("CAD $80", 80, 80),
        ("USD 80–120", 80, 100),
        ("USD 120–80", 120, 80),
        ("USB 80-120", 80, 120),
        ("$10.005", 10, 10),
        ("-$50", 50, 50),
        ("−50 USD", 50, 50),
        ("USD 10", True, 10),
        ("USD 5", float("nan"), 5),
        ("USD 10", -10, 10),
    ],
)
def test_unsubstantiated_or_invalid_numbers_are_rejected(quote, low, high):
    with pytest.raises(ValidationError):
        LineBudget(low_usd=low, high_usd=high, user_quote=quote)


def test_tool_requires_verbatim_user_evidence_and_current_unpriced_line():
    from test_custom_project_routes import project_fixture as concept

    project = concept()
    ctx = ToolContext(project=project, user_message="How much is the rail?")
    args = {
        "line_id": "concept:rail_base",
        "budget": {"low_usd": 80, "high_usd": 120, "user_quote": "Allow USD 80–120."},
    }
    assert not asyncio.run(execute_tool("set_line_budget", args, ctx)).ok
    ctx.user_message = args["budget"]["user_quote"]
    for line in ["unknown-id", "concept:controller_board"]:
        assert not asyncio.run(execute_tool("set_line_budget", {**args, "line_id": line}, ctx)).ok
    assert ctx.project == project


def test_budget_roundtrip_idempotent_replace_clear_and_scope_change():
    from test_custom_project_pricing import modified_project
    from test_custom_project_routes import project_fixture as concept

    original = concept()
    key = "concept:rail_base"
    saved = declare(original, key)
    assert declare(saved, key) == saved
    rebudgeted = declare(saved, key, 5, 5, "Allow USD 5 for this line.")
    assert purchase_cost_report(rebudgeted)["summary"]["planning_low"] == 5
    loaded, _ = restore_snapshot(make_snapshot(saved, []))
    assert loaded["budget_allowances"] == saved["budget_allowances"]
    assert purchase_cost_report(loaded)["summary"]["planning_low"] == 80
    tampered = make_snapshot(saved, [])
    tampered["budget_allowances"][key]["low_usd"] = 17
    with pytest.raises(ValueError):
        restore_snapshot(tampered)

    def longer_rail(raw):
        raw["components"][0]["primitives"][0]["size_mm"][0] += 1

    changed = modified_project(saved, longer_rail)
    reconcile_budgets(changed)
    assert not changed["budget_allowances"]
    ctx = ToolContext(project=loaded, user_message="Remove the rail budget.")
    assert asyncio.run(execute_tool("set_line_budget", {"line_id": key, "budget": None}, ctx)).ok
    assert purchase_cost_report(ctx.project)["summary"]["planning_low"] is None


def test_legacy_model_sidecar_never_supplies_prices():
    from test_custom_project_routes import project_fixture as concept

    from app.parts_proposal import build_parts_proposal

    project = concept()
    old = {
        "project_id": project["id"],
        "project_revision": project["revision"],
        "rows": [
            {
                "group_id": "concept-rail_base",
                "source_kind": "model_estimate",
                "low_usd": 456789,
                "high_usd": 456790,
                "rationale": "Typical class price",
            }
        ],
    }
    assert "456789" not in json.dumps(build_parts_proposal(project, estimate=old))
    project["cost_estimate"] = old
    assert "456789" not in json.dumps(purchase_cost_report(project))


def test_sourced_floor_keeps_open_ceiling_and_original_date_in_other_currencies():
    from test_commercial_design import layout_project

    from app.commercial_design import adopt_commercial

    project = adopt_commercial(layout_project())
    fx = {
        "rates": {"USD": 1, "EUR": 0.9},
        "base": "USD",
        "rate_date": "2026-11-24",
        "source": "test",
    }
    usd = build_complete_bom(project)
    eur = build_complete_bom(project, currency="EUR", fx_rates=fx)
    us, eu = usd["lines"][0], eur["lines"][0]
    assert us["line_total"] is eu["line_total"] is None
    assert eu["planning_basis"] == us["planning_basis"]
    assert us["planning_basis"]["source_url"] and us["planning_basis"]["observed_on"]
    assert (eu["planning_low"], eu["planning_high"]) == (14355, None)
    assert usd["summary"]["documented_subtotal"] == 352.99
    assert usd["summary"]["planning_high"] is eur["summary"]["planning_high"] is None


def test_expired_observation_is_separate_from_unchanged_budgets():
    project = project_fixture()
    report = supplier_report(project, price=7)
    result = purchase_cost_report(project, report)
    assert result["summary"]["live_api_subtotal"] == 7
    expired = deepcopy(report)
    expired["expires_at"] = "2000-01-01T00:00:00Z"
    result = purchase_cost_report(project, expired)
    row = next(r for r in result["rows"] if r["group_id"] == "controller")
    assert row["source_kind"] == "stale_observation" and row["documented_usd"] is None
    assert not result["summary"]["planning_count"]


def test_legacy_draft_does_not_acquire_template_prices_from_its_sourcing_groups():
    project = new_project()
    project.pop("workflow", None)
    assert project["sourcing"]["groups"]
    ledger = purchase_cost_report(project)
    assert ledger["status"] == "design_required" and ledger["rows"] == []
    assert ledger["summary"]["documented_subtotal"] is ledger["summary"]["planning_low"] is None


def test_undated_supplier_amount_cannot_replace_a_catalog_price():
    project = project_fixture()
    quote = {
        "availability": "InStock",
        "pack_price": 24567,
        "source_url": "https://example.org/pico",
    }
    line = next(
        r
        for r in build_complete_bom(project, live_quotes={"pico": quote})["lines"]
        if r["group_id"] == "controller"
    )
    assert line["price_source"] == "curated_reference" and line["line_total"] != 24567


def test_available_offer_replaces_the_allowance_only_for_its_line():
    from test_bench_station_procurement import station_project

    from app.budget_allowances import budget_signature

    project = station_project(["inspect"])
    # The purchasing catalog can retain identified items with no documented price.
    from unittest.mock import patch

    from app.bom_engine import load_purchasable_catalog

    catalog = load_purchasable_catalog()
    catalog["items"]["camera-module-3"]["reference_unit_price_usd"] = None
    with patch("app.bom_engine.load_purchasable_catalog", return_value=catalog):
        line = build_complete_bom(project)["lines"][0]
        budget = LineBudget(low_usd=60, high_usd=80, user_quote="Budget USD 60–80 for the camera.")
        project["budget_allowances"] = {
            line["line_id"]: {
                **budget.model_dump(),
                "scope_signature": budget_signature(project, line),
            }
        }
        assert build_complete_bom(project)["summary"]["planning_low"] == 60
        quote = {
            "pack_price": 27,
            "availability": "InStock",
            "source_url": "https://example.org/5657",
            "fetched_at": "2026-11-24T12:00:00Z",
        }
        bom = build_complete_bom(project, live_quotes={"camera-module-3": quote})
        assert bom["summary"]["documented_subtotal"] == 27
        assert bom["summary"]["planning_low"] is None
        assert not bom["lines"][0].get("planning_basis")


def test_unchanged_concept_geometry_keeps_its_budget_across_pose_and_prose_edits():
    from test_custom_project_pricing import modified_project
    from test_custom_project_routes import project_fixture as concept

    original = declare(concept(), "concept:sample_tray")

    def move(raw):
        raw["components"][1]["placement_mm"] = [10, 10, 0]
        raw["components"][1]["assembly"] = "Use the same part in the new location."

    changed = modified_project(original, move)
    reconcile_budgets(changed)
    assert changed["budget_allowances"] == original["budget_allowances"]


def test_both_csv_formats_preserve_subtotals_fabrication_and_missing_prices():
    import csv
    import io

    from test_foundation_storage import source_project

    from app.bom_engine import bom_to_csv
    from app.price_estimation import estimate_csv

    project = declare(source_project(), "upstream-platform")
    bom = build_complete_bom(project)
    s = bom["summary"]
    for content, documented, filament in (
        (bom_to_csv(bom), "documented_line_total", "printed_material_total"),
        (estimate_csv(purchase_cost_report(project)), "documented_usd", "printed_material_usd"),
    ):
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        assert len(rows) == s["line_count"] and len({r["line_id"] for r in rows}) == len(rows)
        assert float(rows[0]["revision_planning_low"]) == 80
        assert float(rows[0]["revision_planning_high"]) == 120
        assert float(rows[0]["revision_documented_subtotal"]) == s["documented_subtotal"]
        assert int(rows[0]["revision_missing_price_count"]) == s["missing_price_count"]
        assert all(r["revision_documented_subtotal"] == "" for r in rows[1:])
        assert round(sum(float(r[documented] or 0) for r in rows), 2) == s["documented_subtotal"]
        material = sum(float(r[filament] or 0) for r in rows)
        assert round(material, 2) == s["printed_material_subtotal"]
        assert all(not (r[documented] and r[filament]) for r in rows)

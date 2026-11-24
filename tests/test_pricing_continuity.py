"""Offline pricing continuity: observations retain their original time and scope."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_price_estimation import declare, project_fixture, supplier_report

import app.api as api
from app import price_estimation as prices
from app import procurement
from app.project_storage import restore_snapshot


def revision(project):
    result = deepcopy(project)
    result["revision"] += 1
    result["summary"]["target_concentrations"] = "1 mg/mL, 0.2 mg/mL, 0.1 mg/mL"
    result["brief"]["objective"] = "Rephrased recipe request, unchanged machine"
    return result


def test_recipe_rebind_retains_observations_ranges_and_expiry():
    old = project_fixture()
    report = supplier_report(old)
    new = revision(old)
    rebound = procurement.rebind_report(old, new, report)
    assert procurement.report_for_project(new, report) is None
    for key in ["report_id", "fetched_at", "expires_at"]:
        assert rebound[key] == report[key]
    before = prices.purchase_cost_report(old, report)
    assert prices.purchase_cost_report(new, rebound)["summary"] == before["summary"]


@pytest.mark.parametrize("change", ["sku", "supplier", "url", "quantity", "size", "geometry"])
def test_changed_purchase_basis_drops_only_affected_observation(change):
    old = project_fixture()
    report = supplier_report(old)
    new = revision(old)
    group = next(g for g in new["sourcing"]["groups"] if g["id"] == "controller")
    selected = next(o for o in group["options"] if o["id"] == group["selected_id"])
    if change == "quantity":
        group["quantity"] += 1
    elif change in ["sku", "supplier"]:
        selected[change] = "different"
    elif change == "url":
        selected["source_url"] += "?different=1"
    else:
        part = next(p for p in new["design"]["components"] if p["id"] in group["component_ids"])
        if change == "size":
            part["size"][0] += 1
        else:
            part["geometry"] = {"kind": "custom", "hole_count": 20}
    rebound = procurement.rebind_report(old, new, report)
    row = next(r for r in rebound["rows"] if r["group_id"] == "controller")
    assert row["line_total"] is None and row["freshness"] == "unverified"


def test_expiry_is_not_revived_and_mismatched_sidecar_cannot_rebind(monkeypatch):
    old = project_fixture()
    report = supplier_report(old)
    expiry = datetime.fromisoformat(report["expires_at"])
    monkeypatch.setattr(procurement, "_now", lambda: expiry + timedelta(seconds=1))
    rebound = procurement.rebind_report(old, revision(old), report)
    row = next(r for r in rebound["rows"] if r["group_id"] == "controller")
    assert row["line_total"] is None and row["last_known_line_total"] == 12
    assert row["freshness"] == "stale" and rebound["expires_at"] == report["expires_at"]
    report["project_revision"] += 99
    assert procurement.rebind_report(old, revision(old), report) is None


def test_declared_budget_survives_recipe_edit_without_refresh():
    from test_foundation_storage import source_project

    from app.services.session_store import accept_project

    old = declare(source_project(), "upstream-platform")
    session = api.ProjectSession(project=old)
    updated = revision(old)
    updated.pop("budget_allowances")
    accept_project(session, updated)
    assert session.project["budget_allowances"] == old["budget_allowances"]
    assert prices.purchase_cost_report(updated)["summary"]["planning_low"] == 80


def test_actual_jubilee_extension_retains_supplier_facts_and_invalidates_remainder_only():
    from evolution_fixtures import inspection_proposal

    from app.inspection import apply_extension

    payload = json.loads(Path("docs/examples/presentation-flow-project.json").read_text())
    old, _ = restore_snapshot(payload)
    rows = procurement.initial_procurement_rows(old)
    now = datetime.now(UTC)
    for row in rows:
        if row["catalog_supported"]:
            row.update(
                status="priced",
                freshness="fresh",
                line_total=10,
                unit_price=10,
                fetched_at=now.isoformat(),
                issue=None,
            )
    report = {
        "schema_version": 1,
        "report_id": "offline-jubilee-observations",
        "project_id": old["id"],
        "project_revision": old["revision"],
        "market": "US",
        "currency": "USD",
        "rows": rows,
        "fetched_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
    }
    original = prices.purchase_cost_report(old, report)
    new = apply_extension(old, inspection_proposal(), {"model": "offline-fixture"})
    rebound = procurement.rebind_report(old, new, report)
    cost = prices.purchase_cost_report(new, rebound)
    added = {r["group_id"] for r in cost["rows"]} - {r["group_id"] for r in original["rows"]}
    assert {"inspection-camera", "inspection-arm", "inspection-mount"} <= added
    assert cost["summary"]["live_api_subtotal"] == original["summary"]["live_api_subtotal"]
    assert cost["summary"]["planning_low"] is None
    assert rebound["report_id"] == report["report_id"]
    for row in rebound["rows"]:
        previous = next((r for r in report["rows"] if r["group_id"] == row["group_id"]), None)
        if previous and previous["freshness"] == "fresh":
            assert row == previous


def test_accepted_inspection_reply_and_guide_share_derived_footprint(monkeypatch):
    import asyncio

    from evolution_fixtures import inspection_proposal

    import app.inspection as inspection
    from app.instructions import build_instruction_guide

    payload = json.loads(Path("docs/examples/presentation-flow-project.json").read_text())
    project, _ = restore_snapshot(payload)

    async def proposal(*args):
        return inspection_proposal(), {"model": "offline-fixture"}

    monkeypatch.setattr(inspection, "propose_inspection", proposal)
    updated, run = asyncio.run(inspection.develop_inspection(project, "Add camera inspection."))
    assert run["status"] == "accepted"
    bounds = updated["inspection_extension"]["assembly_bounds"]
    width = bounds["max_mm"][0] - bounds["min_mm"][0]
    depth = bounds["max_mm"][2] - bounds["min_mm"][2]
    dimensions = f"{width:.1f} × {depth:.1f} mm"
    notice = inspection.footprint_notice(updated)
    assert dimensions in run["message"]
    assert "Bench depth and collision clearance have not been validated." in run["message"]
    guide = build_instruction_guide(updated)
    assert notice in json.dumps(guide, ensure_ascii=False)


def test_recipe_echoing_unchanged_width_never_develops_a_new_layout(monkeypatch):
    import app.chat_actions as actions
    from app.model_client import ExtractedTurn

    payload = json.loads(Path("docs/examples/presentation-flow-project.json").read_text())
    project, _ = restore_snapshot(payload)
    project["summary"]["workspace_width_mm"] = 800
    original = deepcopy(project["engineering_design"])

    async def extracted(*args):
        return ExtractedTurn.model_validate(
            {
                "assistant_message": "Updated the recipe; same workstation.",
                "stock_concentration": "5 mg/mL",
                "target_concentrations": "1 mg/mL, 0.2 mg/mL, 0.1 mg/mL",
                "final_volume": "150 uL",
                "workspace_width_mm": 800,
            }
        ), {"total_tokens": 5}

    async def forbidden(*args, **kwargs):
        raise AssertionError("An echoed unchanged width must not regenerate geometry")

    monkeypatch.setattr(api, "generate_turn", extracted)
    monkeypatch.setattr(actions, "develop_design", forbidden)
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    try:
        with TestClient(api.app) as client:
            response = client.post(
                f"/api/projects/{project['id']}/messages",
                json={
                    "message": (
                        "Keep the machine layout unchanged. "
                        "Use 150 uL per well; width stays 800 mm."
                    )
                },
            )
            assert response.status_code == 200, response.text
            updated = response.json()["project"]
            assert updated["engineering_design"] == original
            assert updated["summary"]["final_volume"] == "150 uL"
            assert not [
                run for run in response.json()["action_runs"] if run["kind"] == "develop_layout"
            ]
    finally:
        api._sessions.pop(project["id"], None)


def test_observed_constraint_source_failure_stays_strict_and_literal_pair_retains_depth():
    from app.model_client import ExtractedTurn

    message = "My bench remains 800 mm wide and 800 mm deep; keep the design."
    # Synthetic reproduction of provider24's rejected source/value pair.
    failed = ExtractedTurn.model_validate(
        {
            "assistant_message": "Constraints recorded.",
            "brief_constraints": "bench 800 mm wide and 800 mm deep",
            "source_brief_constraints": "800 mm wide and 800 mm deep",
        }
    )
    with pytest.raises(ValueError, match="Brief value must occur in its supporting source"):
        failed.verify_sources(message)
    literal = failed.model_copy(
        update={
            "brief_constraints": "800 mm wide and 800 mm deep",
        }
    )
    literal.verify_sources(message)
    assert "wide" in literal.brief_constraints and "deep" in literal.brief_constraints

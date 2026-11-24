"""Offline purchasing cost projections from the current revision's itemized BOM."""

import csv
import hashlib
import io
import json

from app.bom_engine import _safe_csv_cell, build_complete_bom, revision_cost_columns
from app.procurement import report_for_project


def price_context(project, procurement=None):
    """Fingerprint purchasing identities independently of supplier price and freshness."""
    bom = build_complete_bom(project)
    signature = [line["budget_scope_signature"] for line in bom["lines"]]
    digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    return {
        "input_scope_sha256": digest,
        "rows": [line for line in bom["lines"] if line.get("purchase_quantity") != 0],
    }


def purchase_cost_report(project, procurement=None, *, lab_profile=None):
    current = report_for_project(project, procurement)
    bom = build_complete_bom(project, procurement_report=current, lab_profile=lab_profile)
    return cost_report_from_bom(bom, current)


def cost_report_from_bom(bom, current=None):
    rows = []
    for line in bom["lines"]:
        basis = line.get("planning_basis") or {}
        rows.append(
            {
                "line_id": line["line_id"],
                "group_id": line["group_id"],
                "label": line["label"],
                "quantity": line["quantity"],
                "source_kind": line["price_source"],
                "cost_category": line["cost_category"],
                "documented_usd": line["documented_line_total"],
                "printed_material_usd": line["printed_material_total"],
                "planning_low_usd": line["planning_low"],
                "planning_high_usd": line["planning_high"],
                "planning_basis": basis.get("kind"),
                "planning_detail": basis.get("detail"),
                "planning_source_url": basis.get("source_url"),
                "planning_date": basis.get("observed_on"),
                "price_source_url": line.get("source_url")
                if line["cost_category"] == "documented"
                else None,
                "price_checked_on": line.get("price_checked_on") or line.get("fetched_at"),
            }
        )
    return {
        "schema_version": 2,
        "project_id": bom["project_id"],
        "project_revision": bom["project_revision"],
        "currency": "USD",
        "status": bom["status"],
        "generated_at": bom["generated_at"],
        "expires_at": current["expires_at"] if current and not current["is_expired"] else None,
        "rows": rows,
        "summary": bom["summary"],
        "exclusions": bom["exclusions"],
    }


def estimate_csv(report):
    buffer = io.StringIO(newline="")
    totals = revision_cost_columns(report["summary"])
    columns = [
        "project_id",
        "project_revision",
        "currency",
        "line_id",
        "group_id",
        "label",
        "quantity",
        "source_kind",
        "cost_category",
        "documented_usd",
        "printed_material_usd",
        "planning_low_usd",
        "planning_high_usd",
        "planning_basis",
        "planning_detail",
        "planning_source_url",
        "planning_date",
        "price_source_url",
        "price_checked_on",
        *totals,
    ]
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for index, row in enumerate(report["rows"]):
        values = {key: row.get(key, report.get(key)) for key in columns}
        if index == 0:
            values.update(totals)
        writer.writerow({key: _safe_csv_cell(value) for key, value in values.items()})
    return buffer.getvalue().encode("utf-8")

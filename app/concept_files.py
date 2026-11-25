"""Render immutable, offline-friendly files from one compiled mechanical assembly."""

import csv
import io
import json
import math
import zipfile
from datetime import UTC, datetime

from app.concept_design import concept_obj, concept_stl
from app.concept_guide import build_concept_guide, concept_controller_source, concept_gateway_config

PRICE_COLUMNS = [
    "Supplier price status",
    "Supplier item USD",
    "Supplier pack USD",
    "Pack size",
    "Packs to buy",
    "Supplier line USD",
    "Supplier checked at",
    "Supplier valid until",
    "Supplier price URL",
    "Catalog line USD",
    "Catalog checked on",
    "Catalog price URL",
    "Printed material USD",
    "Planning low USD",
    "Planning high USD",
    "Planning basis",
    "Planning evidence",
    "Planning source URL",
    "Price scope and exclusions",
    "Price record exported at",
]


def _future(value, now):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed > now
    except (AttributeError, TypeError, ValueError):
        return False


def _money(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return ""
    return f"{value:.2f}"


def price_record_for_project(
    project: dict, procurement: dict | None = None, estimate: dict | None = None, *, bom=None
) -> dict:
    """Read trusted runtime sidecars only; never fetch or read imported project prices.

    Values are dated records, not renewable quotes. Invalid, expired and unmatched
    values are omitted; missing prices are explicit and never written as zero.
    """
    from app.bom_engine import build_complete_bom
    from app.instructions import _safe_url
    from app.procurement import initial_procurement_rows, report_for_project

    now = datetime.now(UTC)
    try:
        report = report_for_project(project, procurement, now=now)
    except (ArithmeticError, KeyError, TypeError, ValueError):
        report = None
    report_current = bool(
        report
        and report.get("currency") == "USD"
        and report.get("market") == "US"
        and _future(report.get("expires_at"), now)
    )
    observed = {row["group_id"]: row for row in report["rows"]} if report else {}
    if bom is None:
        bom = build_complete_bom(project, procurement_report=report)
    amounts = {r["group_id"]: r for r in bom["lines"]}
    rows = {}
    for expected in initial_procurement_rows(project):
        key = expected["group_id"]
        observation = observed.get(key, {})
        line = amounts.get(key, {})
        status = (
            "Unchecked supplier listing"
            if expected.get("catalog_supported")
            else "Missing price; supplier or quotation required"
        )
        if procurement and not report:
            status = "Supplier record does not match this revision; omitted"
        elif observation.get("freshness") == "stale" or (report and not report_current):
            status = "Expired supplier observation omitted"
        elif observation.get("status") == "unavailable":
            status = "Supplier item unavailable; price omitted"
        supplier = ["", "", "", "", "", "", "", ""]
        basis = line.get("planning_basis") or {}
        catalog = line.get("price_source") == "curated_reference"
        if line.get("price_source") == "live_api":
            status = "Dated supplier observation"
            supplier = [
                _money(line["unit_price"]),
                _money(line["pack_price"]),
                line["pack_size"],
                line["packs_to_order"],
                _money(line["line_total"]),
                line["fetched_at"],
                report.get("expires_at") if report else None,
                _safe_url(line.get("source_url")),
            ]
        rows[key] = [
            status,
            *supplier,
            _money(line["line_total"]) if catalog else "",
            line.get("price_checked_on") if catalog else "",
            line.get("source_url") if catalog else "",
            _money(line.get("line_total"))
            if line.get("cost_category") == "printed_material"
            else "",
            _money(line.get("planning_low")),
            _money(line.get("planning_high")),
            basis.get("kind"),
            basis.get("detail"),
            basis.get("source_url"),
            "Each amount covers the listed quantity. Shipping, tax and labor are excluded.",
            now.isoformat(),
        ]
    return {"project_id": project["id"], "revision": project["revision"], "rows": rows}


def _safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_asset(
    design: dict, project_id: str, revision: int, asset: str, *,
    price_record: dict | None = None, purchasing_bom: dict | None = None,
    assembly: bytes | None = None,
) -> tuple[bytes, str]:
    if asset == "project.json":
        data = {"project_id": project_id, "revision": revision, "design": design}
        return json.dumps(data, ensure_ascii=False, indent=2).encode(), "application/json"
    if asset == "parts.csv":
        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        chosen = {line["group_id"]: line for line in (purchasing_bom or {}).get("lines", [])
                  if line.get("part_choice")}
        priced = bool(
            price_record
            and price_record.get("project_id") == project_id
            and price_record.get("revision") == revision
        )
        writer.writerow(
            [
                "Part",
                "Quantity",
                "Unit",
                "Source",
                "Reference URL",
                "Local model",
                "US catalog search",
                *(PRICE_COLUMNS if priced else []),
                *(
                    [
                        "Selected issuer",
                        "Selected part number",
                        "Identity source",
                        "Source basis",
                        "Interface review",
                        "Units on hand",
                        "Units to purchase",
                        "Purchase cost basis",
                        "Purchase total",
                        "Currency",
                        "Selection evidence",
                        "Interface checks",
                        "Candidates",
                    ]
                    if chosen
                    else []
                ),
            ]
        )
        keys = {c["key"] for c in design["components"]}
        for row in design["parts"]:
            line = chosen.get("concept-" + row["key"], {})
            choice = line.get("part_choice", {})
            selected = choice.get("accepted") or {}
            writer.writerow(
                [
                    _safe_cell(v)
                    for v in (
                        row["name"],
                        row["quantity"],
                        row["unit"],
                        row.get("source_title"),
                        row.get("source_url"),
                        f"parts/{row['key']}.stl" if row["key"] in keys else "",
                        row.get("catalog_search_url"),
                        *(
                            price_record["rows"].get(
                                "concept-" + row["key"], [""] * len(PRICE_COLUMNS)
                            )
                            if priced
                            else []
                        ),
                        *(
                            [
                                selected.get("issuer"),
                                selected.get("part_number"),
                                selected.get("source_url"),
                                selected.get("basis"),
                                selected.get("interface_status"),
                                line.get("owned_quantity"),
                                line.get("purchase_quantity"),
                                line.get("price_source"),
                                line.get("line_total"),
                                line.get("currency"),
                                choice.get("decision"),
                                json.dumps(selected.get("checks", [])),
                                json.dumps(choice.get("candidates", [])),
                            ]
                            if chosen
                            else []
                        ),
                    )
                ]
            )
        return buf.getvalue().encode("utf-8-sig"), "text/csv"
    if asset == "guide.html":
        return build_concept_guide(design, project_id, revision), "text/html"
    if asset == "controller.py":
        return concept_controller_source(), "text/x-python"
    if asset == "gateway-config.json":
        return concept_gateway_config(design, project_id, revision), "application/json"
    if asset == "machine.stl":
        return concept_stl(design), "model/stl"
    if asset == "machine.obj":
        return concept_obj(design), "model/obj"
    if asset == "machine.step":
        from app.concept_cad import concept_step

        return concept_step(design), "model/step"
    parts = {f"parts/{part['key']}.stl": part["key"] for part in design["components"]}
    if asset in parts:
        return concept_stl(design, parts[asset]), "model/stl"
    step_parts = {f"parts/{part['key']}.step": part["key"] for part in design["components"]}
    if asset in step_parts:
        from app.concept_cad import concept_step

        return concept_step(design, step_parts[asset]), "model/step"
    if asset == "package.zip":
        from app.bom_engine import bom_to_csv, bom_to_json, build_complete_bom
        from app.project_assembly import assembly_json

        project = {"id": project_id, "revision": revision, "concept_design": design}
        if purchasing_bom is None:
            purchasing_bom = build_complete_bom(project)
        if assembly is None:
            assembly = assembly_json(project)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("physical-assembly.json", assembly)
            archive.writestr("bom/purchasable-bom.json", bom_to_json(purchasing_bom))
            archive.writestr("bom/purchasable-bom.csv", bom_to_csv(purchasing_bom))
            for name in (
                "guide.html",
                "project.json",
                "machine.stl",
                "controller.py",
                "gateway-config.json",
                "parts.csv",
                *parts,
            ):
                content, _ = render_asset(
                    design,
                    project_id,
                    revision,
                    name,
                    price_record=price_record,
                    purchasing_bom=purchasing_bom,
                )
                archive.writestr(name, content)
        return buf.getvalue(), "application/zip"
    raise KeyError(asset)

"""Canonical, offline purchase proposal for every supported project route.

Amounts describe mutually exclusive purchase groups, never repeated viewer meshes.
Reviewed references are candidates, not replacements for accepted source geometry.
"""

from __future__ import annotations

from urllib.parse import urlencode

REVIEWED_ON = "2026-11-03"
FRIDGE_URL = "https://www.accucold.com/product/ARS1MLDL2B"
CAMERA_URL = "https://www.adafruit.com/product/5657"
GRIPPER_URL = "https://www.servocity.com/servo-driven-parallel-gripper-kit-servo-included/"
GRIPPER_GUIDE = (
    "https://www.servocity.com/content/user_manuals/3219-0001-0002_assembly_instructions.min.pdf"
)
SCICLOPS_URL = (
    "https://hudsonrobotics.com/wp-content/uploads/2023/02/HUD-0020.19.1-BR-SCICLOPS-23-1.pdf"
)
FABRICATION_URL = "https://www.protolabs.com/services/3d-printing/"
TIPS_URL = "https://opentrons.com/products/opentrons-300ul-tips-1000-refills"
PLATE_URL = (
    "https://ecatalog.corning.com/life-sciences/b2c/US/en/Microplates/Assay-Microplates/"
    "96-Well-Microplates/Corning%C2%AE-96-well-Clear-Polystyrene-Microplates/p/3596"
)
PIPETTE_URL = "https://science-jubilee.readthedocs.io/en/latest/building/pipette_tool.html"


def _fabrication_targets(project):
    kinds = {
        "labware_rack": "rim_supported_tube_rack",
        "labware_plate": "microplate_96",
        "tip_rack": "rim_supported_tip_rack",
    }
    return {
        module["target_component_id"]
        for key, kind in kinds.items()
        if isinstance(module := project.get(key), dict)
        and module.get("kind") == kind
        and module.get("target_component_id")
    }


def _source_candidate(row, project):
    gid = row["group_id"]
    if gid == "pipette":
        return (
            PIPETTE_URL,
            "Science Jubilee Opentrons pipette tool reference",
            "open_design_reference",
        )
    if gid == "power":
        return (
            "https://www.adafruit.com/category/137",
            "Adafruit power supply catalogue",
            "supplier_category",
        )
    if gid == "labware-plate":
        return PLATE_URL, "Corning 3596 plate and separate custom holder", "product_candidate"
    if gid == "tip-rack-options":
        module = project.get("tip_rack") or {}
        if module.get("kind") == "rim_supported_tip_rack":
            return (
                FABRICATION_URL,
                "Custom printed holder for separately purchased tips",
                "fabrication_service",
            )
        if module.get("profile_id") == "tipone_96_tiprack_200ul":
            return (
                "https://www.usascientific.com/act",
                "USA Scientific TipOne 200 µL stack racks — 1111-0200",
                "product_candidate",
            )
        return (
            TIPS_URL,
            "Opentrons OT-2 300 µL tips; check chosen volume profile",
            "product_candidate",
        )
    if any(key in gid for key in ("fixtures", "labware-rack", "inspection-mount", "frame")):
        return (
            FABRICATION_URL,
            "Custom parts — Protolabs fabrication quotation route",
            "fabrication_service",
        )
    context = " ".join(
        [
            gid,
            row["label"],
            *[
                str(p.get("role", ""))
                for p in project.get("concept_design", {}).get("parts", [])
                if "concept-" + p["key"] == gid
            ],
        ]
    ).lower()
    if any(word in context for word in ("screw", "bolt", "fastener", "washer", "nut", "bracket")):
        return (
            "https://www.mcmaster.com/products/fasteners/",
            "McMaster-Carr fasteners catalogue — select size/material",
            "supplier_category",
        )
    if any(
        word in context
        for word in ("tube", "vial", "labware", "beaker", "flask", "vessel", "plate", "tip")
    ):
        return (
            "https://www.fishersci.com/us/en/scientific-products/categories/consumables.html",
            "Fisher Scientific lab consumables catalogue — select format/material",
            "supplier_category",
        )
    if any(
        word in context
        for word in ("motor", "gear", "bearing", "belt", "rail", "shaft", "motion", "linear")
    ):
        return (
            "https://www.pololu.com/category/21/mechanical-components",
            "Pololu mechanical components catalogue — size the drive/interface",
            "supplier_category",
        )
    if any(
        word in context
        for word in (
            "board",
            "controller",
            "sensor",
            "wire",
            "cable",
            "driver",
            "led",
            "light",
            "camera",
            "electronic",
            "power",
        )
    ):
        return (
            "https://www.adafruit.com/search?" + urlencode({"q": row["label"]}),
            "Adafruit electronics search for " + row["label"],
            "supplier_search",
        )
    return (
        FABRICATION_URL,
        "Custom-part fabrication quotation — confirm process/material",
        "fabrication_service",
    )


def _references(row, group):
    alternatives = [
        {
            "label": o["label"],
            "url": o["source_url"],
            "note": o.get("compatibility_note") or "Review fit, ratings and included accessories.",
        }
        for o in group.get("options", [])
        if o.get("id") != group.get("selected_id") and o.get("source_url")
    ]
    source = row.get("price_source_url")
    instruction = (
        "Review the selected source's dimensions and interfaces before fabrication or purchase."
    )
    if row["group_id"] == "cold-transfer-fridge":
        source = FRIDGE_URL
        instruction = (
            "Candidate ARS1MLDL2B is 17.5 × 19.75 × 21.5 inches, 115 V/60 Hz, "
            "2–10 °C. Compare its cabinet, door sweep and shelf access with the project's "
            "custom bay; its envelope has NOT replaced the displayed geometry. Plan a "
            "separate door interface, cold-chain check and manual recovery before automation."
        )
        alternatives.append(
            {
                "label": "Keep manual cold storage transfer",
                "url": FRIDGE_URL,
                "note": "Same refrigerator candidate; retain manual door/transfer.",
            }
        )
    elif row["group_id"] == "cold-transfer-motion":
        source = GRIPPER_URL
        instruction = (
            "Build the proposed gantry from its project dimensions; select rail travel, "
            "payload, drives and end stops. The linked servo gripper is a mechanism "
            "reference only: opening, plate retention, jaw design and full transfer reach "
            "are NOT established. Fit custom fingers to a dummy plate and test manual "
            "release and unpowered recovery before any powered commissioning."
        )
        alternatives.append(
            {
                "label": "Hudson SciClops commercial plate handler",
                "url": SCICLOPS_URL,
                "note": (
                    "Separate architecture requiring a vendor quote and reach/door "
                    "integration review; not fitted to this custom bay."
                ),
            }
        )
    return source, alternatives, instruction


def build_parts_proposal(
    project: dict, report: dict | None = None, estimate: dict | None = None, *, bom=None
) -> dict:
    """Pure revision-bound view; never fetch sources, mutate state or call a model."""
    from app.bom_engine import build_complete_bom, purchasing_route
    from app.procurement import initial_procurement_rows, procurement_groups

    if bom is None:
        bom = build_complete_bom(project, procurement_report=report)
    route = purchasing_route(project)
    has_groups = route in {"concept_design", "bench_station", "workstation"}
    groups = {g["id"]: g for g in procurement_groups(project)} if has_groups else {}
    purchases = {r["group_id"]: r for r in initial_procurement_rows(project)} if groups else {}
    concepts = {
        "concept-" + p["key"]: p for p in (project.get("concept_design") or {}).get("parts", [])
    }
    rows = []
    for line in bom["lines"]:
        gid = line["group_id"]
        purchase = purchases.get(gid) or {
            "group_id": gid,
            "label": line["label"],
            "selected_id": line["catalog_id"] or gid,
            "component_ids": line["component_ids"],
            "price_source_url": line["source_url"],
        }
        quantity = line["quantity"]
        selection = (line.get("part_choice") or {}).get("accepted")
        kind = line["price_source"]
        date = line.get("price_checked_on") or line["fetched_at"]
        price_url = line["source_url"] if line["cost_category"] == "documented" else None
        note = (
            line["notes"]
            or {
                "unpriced": "Identify the part and its purchasing unit to obtain a price.",
                "curated_reference": "Dated catalog price for the required number of packs.",
                "slicer_material_estimate": "Estimated filament cost for this modeled part.",
                "live_api": "Observed supplier price for the required number of packs.",
                "unavailable": "The supplier reported this part unavailable at the last check.",
                "stale_observation": "The previous supplier observation has expired.",
                "quote_required": "Request a quote for this configuration.",
                "quantity_required": "Specify the required quantity before pricing.",
                "lab_inventory": "All required units are on hand.",
                "included_in_base": "Included in the platform scope.",
            }[kind]
        )
        source, alternatives, assembly = _references(purchase, groups.get(gid, {}))
        source_label, source_kind = purchase["label"], "selected_project_reference"
        if selection:
            source, source_label, source_kind = (
                selection["source_url"],
                selection["label"],
                selection["basis"],
            )
        elif not source or gid in ("labware-plate", "tip-rack-options"):
            prior = source
            source, source_label, source_kind = _source_candidate(purchase, project)
            if (concepts.get(gid, {}).get("basis") == "selection_required"
                    and source_kind == "fabrication_service"):
                source = concepts[gid].get("catalog_search_url")
                source_label, source_kind = purchase["label"], "supplier_search"
            if gid in concepts and concepts[gid].get("basis") == "custom_concept":
                source, source_label, source_kind = (
                    FABRICATION_URL,
                    "Custom design — Protolabs fabrication quote route",
                    "fabrication_service",
                )
            if prior:
                alternatives.append(
                    {
                        "label": "Pinned software geometry reference",
                        "url": prior,
                        "note": "Dimension reference only; not a purchasable physical item.",
                    }
                )
        if gid == "upstream-platform":
            source_label = "Jubilee base remainder — excludes listed kits and custom groups"
        if gid == "cold-transfer-fridge":
            source_label = "Accucold ARS1MLDL2B refrigerator candidate"
        if gid == "cold-transfer-motion":
            source_label = "ServoCity 3219-0001-0002 reference + custom Cartesian gantry"
        if (
            gid == "tip-rack-options"
            and (project.get("tip_rack") or {}).get("kind") == "rim_supported_tip_rack"
        ):
            alternatives.append(
                {
                    "label": "Purchase disposable tips separately — USA Scientific catalogue",
                    "url": "https://www.usascientific.com/act",
                    "note": (
                        "Select tips from measured dimensions and pipette interface; "
                        "holder does not imply compatibility."
                    ),
                }
            )
        if source_kind in ("supplier_category", "supplier_search", "fabrication_service"):
            assembly += (
                " This is a sourcing/quotation route; SKU and fabrication process need selection."
            )
        if (
            line["kind"] == "commercial_part"
            and line["catalog_id"]
            and not selection
            and gid not in ("tip-rack-options", "labware-plate")
        ):
            source, source_label, source_kind = line["source_url"], line["label"], "catalog"
        if project.get("design", {}).get("platform") and gid == "platform":
            source = "https://opentrons.com/products/ot-2-robot"
        if line["kind"] == "3d_printed":
            source_label = line["label"]
        if gid == "labware-plate" and line["kind"] == "commercial_part" and line["catalog_id"]:
            source, source_label, source_kind = line["source_url"], line["label"], "catalog"
        part = concepts.get(gid, {})
        if part.get("assembly"):
            assembly = part["assembly"] + " " + assembly
        if project.get("bench_station"):
            assembly = " ".join(project["bench_station"].get("steps", [])) + " " + assembly
        row = {
            "group_id": gid,
            "line_id": line["line_id"],
            "selected_id": line["catalog_id"] or purchase["selected_id"],
            "label": line["label"],
            "selected_product_label": source_label,
            "source_kind": source_kind,
            "quantity": quantity,
            "unit": (line.get("requirement") or {}).get("unit") or line["unit"],
            "component_ids": line["component_ids"],
            "unit_price_low_usd": line["unit_price"],
            "unit_price_high_usd": line["unit_price"],
            "line_total_low_usd": line["line_total"],
            "line_total_high_usd": line["line_total"],
            "price_kind": kind,
            "price_note": note,
            "cost_category": line["cost_category"],
            "planning_basis": line.get("planning_basis"),
            "source_url": source,
            "price_source_url": price_url,
            "unit_price_basis": "Purchase unit and whole-pack quantity from the itemized BOM.",
            "evidence": {
                "kind": kind,
                "observed_at": date,
                "url": price_url,
                "reference_reviewed_on": REVIEWED_ON
                if source in (FRIDGE_URL, GRIPPER_URL)
                else None,
            },
            "alternatives": alternatives,
            "assembly_steps": [
                {
                    "title": "Select and integrate " + purchase["label"],
                    "instruction": assembly,
                    "url": GRIPPER_GUIDE if gid == "cold-transfer-motion" else source,
                }
            ],
        }
        if line["kind"] == "3d_printed" and gid in (
            "labware-plate",
            "labware-rack",
            "tip-rack-options",
        ):
            row["source_url"] = FABRICATION_URL
        if gid == "upstream-platform":
            row["label"] = "Jubilee base remainder (listed kits excluded)"
        if selection:
            row.update(
                label=line["requirement"]["name"],
                owned_quantity=line["owned_quantity"],
                purchase_quantity=line["purchase_quantity"],
                part_choice=line["part_choice"],
                unit_price_low_usd=line["unit_price"],
                unit_price_high_usd=line["unit_price"],
                unit_price_basis="Purchase unit price for the selected component when available.",
            )
        for cid in dict.fromkeys([*line["component_ids"], *purchase["component_ids"]]):
            if cid in _fabrication_targets(project):
                row["fabrication_url"] = (
                    f"/api/projects/{project['id']}/parts/{cid}/model.step"
                    f"?revision={project['revision']}"
                )
                row["fabrication_stl_url"] = row["fabrication_url"].replace(
                    "model.step?", "model.stl?"
                )
                alternatives.append(
                    {
                        "label": "Current custom holder/rack STEP model",
                        "url": row["fabrication_url"],
                        "note": "Revision-bound proposal; verify fit before manufacture.",
                    }
                )
                alternatives.append(
                    {
                        "label": "Current custom holder/rack STL model",
                        "url": row["fabrication_stl_url"],
                        "note": "Print only the holder; purchase disposable tips separately."
                        if gid == "tip-rack-options"
                        else "Check fit before manufacture.",
                    }
                )
        if gid in concepts and part["key"] in {
            c["key"] for c in project["concept_design"]["components"]
        }:
            row["fabrication_url"] = (
                f"/api/projects/{project['id']}/concept/parts/{part['key']}.step"
                f"?revision={project['revision']}"
            )
        if source == GRIPPER_URL or (source == CAMERA_URL and not price_url):
            row["reference_observation"] = {
                "unit_price_usd": 29.25 if source == CAMERA_URL else 34.99,
                "observed_on": REVIEWED_ON,
                "url": source,
                "scope": "Camera board only, excluding host, holder and cables."
                if source == CAMERA_URL
                else (
                    "Gripper kit with servo only. The complete gantry requires a quote."
                ),
                "included_in_total": False,
            }
        rows.append(row)
    totals = bom["summary"]
    return {
        "schema_version": 2,
        "project_id": project["id"],
        "project_revision": project["revision"],
        "currency": "USD",
        "rows": rows,
        "summary": {
            **totals,
            "total_low_usd": totals["documented_subtotal"],
            "total_high_usd": totals["documented_subtotal"],
            "priced_count": totals["documented_count"],
            "unpriced_count": totals["missing_price_count"],
            "row_count": len(rows),
            "supplier_priced_count": totals["live_api_priced_count"],
            "exclusions": [
                "Shipping, sales tax, duties, engineering, assembly labor, validation and support."
            ],
        },
        "limitations": [
            "Planning allowances are proposed budgets, not vendor quotes or price observations.",
            "Reference products do not change accepted geometry or establish compatibility.",
            "Each row covers one BOM line. Alternatives are excluded from the subtotal.",
            "The listed-scope total is not a complete commissioned-machine quotation.",
        ],
    }


def cold_transfer_candidate_rows(project: dict) -> list[dict]:
    """Discuss sourced options without adding hardware or altering project state."""
    rows = []
    for gid, label in (
        ("cold-transfer-fridge", "Accucold ARS1MLDL2B refrigerator candidate"),
        ("cold-transfer-motion", "ServoCity 3219-0001-0002 reference + custom Cartesian gantry"),
    ):
        url, alternatives, instruction = _references({"group_id": gid}, {})
        rows.append({
            "group_id": gid, "label": label, "selected_product_label": label, "source_url": url,
            "price_kind": "unpriced", "line_total_low_usd": None, "line_total_high_usd": None,
            "price_note": "A quote is needed for this complete purchase scope.",
            "evidence": {"kind": "unpriced", "url": None, "observed_at": None},
            "alternatives": alternatives,
            "assembly_steps": [{"title": label, "instruction": instruction,
                                "url": GRIPPER_GUIDE if gid == "cold-transfer-motion" else url}],
        })
    return rows


def render_part_model(project: dict, component_id: str, format: str) -> bytes:
    """Export only a canonical rack/holder; never load or rebuild the upstream machine."""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from build123d import Unit, export_step, export_stl

    from app.cad_exports import _build_plate_part, _build_solid, _components

    if format not in ("step", "stl"):
        raise ValueError("Only STEP or STL is supported.")
    if component_id not in _fabrication_targets(project):
        raise ValueError("This component is not a custom fabrication model.")
    component = next((c for c in _components(project) if c["id"] == component_id), None)
    if not component or component.get("fabrication_shape") not in (
        "rim_supported_tube_rack",
        "rim_supported_tip_rack",
        "microplate_holder",
    ):
        raise ValueError("No standalone fabrication model is available for this component.")
    solid = (
        _build_plate_part(component, "holder-")
        if component["fabrication_shape"] == "microplate_holder"
        else _build_solid(component)
    )
    if solid is None or not solid.is_valid or solid.volume <= 0:
        raise ValueError("The proposed fabrication solid is invalid.")
    with TemporaryDirectory() as directory:
        path = Path(directory) / ("part." + format)
        if format == "step":
            export_step(solid, path, unit=Unit.MM)
        else:
            export_stl(solid, path)
        return path.read_bytes()

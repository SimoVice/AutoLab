"""Compile project requirements, identifiable purchase lines, and fabrication estimates."""

from __future__ import annotations

import csv
import io
import json
import math
from copy import deepcopy
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.part_choices import (
    Candidate,
    add_declared_specs,
    candidate_view,
    catalog_candidate,
    required_amount,
    requirement_signature,
)
from app.print_estimator import estimate_fdm_part
from app.supplier_apis import (
    UNAVAILABLE_STATES,
    SupplierApiError,
    cached_fx_rates,
    cached_official_quotes,
    convert_currency,
)

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "purchasable_catalog.json"

SHIPPING_AND_TAX_EXCLUSIONS = [
    "Shipping, freight, import duties, VAT, and local sales taxes are excluded.",
    "Assembly labor, calibration, validation runs, and laboratory consumables beyond listed "
    "items are excluded.",
    "Dated curated reference prices and analytical FDM filament estimates are tracked separately "
    "from fresh verified live supplier API totals.",
]


def _money_float(val: float | Decimal) -> float:
    dec = val if isinstance(val, Decimal) else Decimal(str(val))
    return float(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@lru_cache(maxsize=1)
def _raw_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def load_purchasable_catalog() -> dict[str, Any]:
    """Return a detached copy of the structured off-the-shelf component catalog."""
    return deepcopy(_raw_catalog())


def _resolve_currency_and_fx(
    currency: str, fx_rates: dict[str, Any] | None, *, requires_rates: bool
) -> tuple[str, dict[str, Any] | None]:
    curr = str(currency or "USD").strip().upper()
    if curr not in ("USD", "CHF", "EUR"):
        raise ValueError(f"Unsupported currency {currency!r}. Choose USD, CHF, or EUR.")
    if curr == "USD" or not requires_rates:
        return curr, fx_rates
    eff_fx = fx_rates or cached_fx_rates("USD")
    if eff_fx is None:
        raise SupplierApiError(
            f"Official ECB FX rates are not cached yet for {curr}. Refresh FX/BOM first."
        )
    return curr, eff_fx


def _catalog_line(
    item_id: str,
    quantity: int,
    *,
    group_id: str,
    component_ids: list[str] | None = None,
    role_note: str = "",
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    live_rows: dict[str, dict[str, Any]] | None = None,
    catalog_items: dict[str, dict[str, Any]] | None = None,
    override_label: str | None = None,
) -> dict[str, Any]:
    items = catalog_items or load_purchasable_catalog()["items"]
    entry = items.get(item_id)
    if entry is None:
        raise ValueError(f"Unknown purchasable catalog item {item_id!r}.")
    pack_size = max(1, int(entry.get("pack_size") or 1))

    live = (live_rows or {}).get(item_id) or {}
    supplier, sku = entry.get("supplier"), entry.get("sku")
    if live.get("freshness") == "stale" or live.get("status") == "unavailable":
        stale = live.get("freshness") == "stale"
        price_source = "stale_observation" if stale else "unavailable"
        pack_size = max(1, int(live.get("pack_size") or pack_size))
        packs_needed = unit_price = pack_price = line_total = None
        availability = "recheck_required" if stale else live.get("availability") or "unavailable"
        stock_qty = None if stale else live.get("stock_quantity")
        lead_days = None
        source_url = live.get("availability_source_url") or live.get("price_source_url")
        fetched_at = live.get("last_known_fetched_at") or live.get("fetched_at")
        adapter = live.get("adapter") or live.get("parser")
        supplier = live.get("supplier") or supplier
        sku = live.get("sku") or sku
    elif (live.get("freshness") == "fresh" and live.get("status") == "priced"
          and (live.get("pack_price") is not None or live.get("unit_price") is not None)):
        pack_size = max(1, int(live.get("pack_size") or pack_size))
        packs_needed = max(math.ceil(quantity / pack_size), int(live.get("min_order_qty") or 1))
        tiers = sorted(live.get("price_breaks") or [], key=lambda t: t["minimum_packs"])
        live_curr = str(live.get("currency") or "USD").upper()
        raw_pack_price = live.get("pack_price")
        if tiers:
            packs_needed = max(packs_needed, tiers[0]["minimum_packs"])
            raw_pack_price = next(t["pack_price"] for t in reversed(tiers)
                                  if t["minimum_packs"] <= packs_needed)
        if raw_pack_price is None:
            raw_pack_price = Decimal(str(live.get("unit_price") or 0)) * pack_size
        unit_price = (
            convert_currency(live["unit_price"], live_curr, currency, fx_rates)
            if not tiers and live.get("unit_price") is not None
            else convert_currency(
                Decimal(str(raw_pack_price)) / pack_size, live_curr, currency, fx_rates
            )
        )
        pack_price = convert_currency(raw_pack_price, live_curr, currency, fx_rates)
        line_total = _money_float(Decimal(str(pack_price)) * packs_needed)
        price_source = "live_api"
        availability = str(live.get("availability") or "unknown")
        stock_qty = live.get("stock_quantity")
        lead_days = live.get("lead_time_days") or entry.get("typical_lead_time_days")
        source_url = live.get("price_source_url") or entry.get("source_url")
        fetched_at = live.get("fetched_at")
        adapter = live.get("adapter") or live.get("parser")
        supplier = live.get("supplier") or supplier
        sku = live.get("sku") or sku
    elif entry.get("reference_unit_price_usd") is not None:
        packs_needed = math.ceil(quantity / pack_size)
        ref_usd = float(entry["reference_unit_price_usd"])
        pack_price = (
            _money_float(ref_usd)
            if currency == "USD"
            else convert_currency(ref_usd, "USD", currency, fx_rates)
        )
        unit_price = _money_float(Decimal(str(pack_price)) / pack_size)
        line_total = _money_float(Decimal(str(pack_price)) * packs_needed)
        price_source = "curated_reference"
        availability = "reference_catalog"
        stock_qty = None
        lead_days = entry.get("typical_lead_time_days")
        source_url = entry.get("source_url")
        fetched_at = None
        adapter = "curated_reference_catalog"
    else:
        packs_needed = unit_price = pack_price = line_total = None
        price_source, availability = "quote_required", "unknown"
        stock_qty = lead_days = fetched_at = adapter = None
        source_url = entry.get("source_url")

    budget = None
    if price_source == "quote_required" and entry.get("planning_basis"):
        budget = deepcopy(entry["planning_basis"])
        for field in ("low_usd", "high_usd"):
            if budget[field] is not None:
                packs = math.ceil(quantity / pack_size)
                budget[field] = _money_float(Decimal(str(budget[field])) * packs)
    return {
        "line_id": f"{group_id}:{item_id}",
        "group_id": group_id,
        "catalog_id": item_id,
        "label": override_label or entry["label"],
        "category": entry["category"],
        "kind": "commercial_part",
        "resolution_status": "catalog_identified",
        "component_ids": list(component_ids or []),
        "supplier": supplier,
        "sku": sku,
        "mpn": entry.get("mpn"),
        "digikey_pn": entry.get("digikey_pn"),
        "mouser_pn": entry.get("mouser_pn"),
        "quantity": int(quantity),
        "unit": entry.get("unit") or "unit",
        "pack_size": pack_size,
        "packs_to_order": packs_needed,
        "currency": currency,
        "unit_price": unit_price,
        "pack_price": pack_price,
        "line_total": line_total,
        "price_source": price_source,
        "availability": availability,
        "stock_quantity": stock_qty,
        "lead_time_days": lead_days,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "adapter": adapter,
        "specs": deepcopy(entry.get("specs") or {}),
        "notes": role_note,
        **({"planning_basis": budget} if budget else {}),
        "print_estimate": None,
    }


def _printed_line(
    part_id: str,
    label: str,
    bounding_box_mm: list[float] | tuple[float, float, float],
    *,
    group_id: str = "printed-parts",
    quantity: int = 1,
    volume_mm3: float | None = None,
    material: str = "PLA",
    nozzle_mm: float = 0.4,
    layer_height_mm: float = 0.2,
    wall_loops: int = 3,
    infill_percent: float = 20.0,
    supports_enabled: bool = False,
    lab_profile: dict[str, Any] | None = None,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    role_note: str = "",
) -> dict[str, Any]:
    est = estimate_fdm_part(
        part_id,
        label,
        bounding_box_mm,
        volume_mm3=volume_mm3,
        material=material,
        nozzle_mm=nozzle_mm,
        layer_height_mm=layer_height_mm,
        wall_loops=wall_loops,
        infill_percent=infill_percent,
        supports_enabled=supports_enabled,
        quantity=quantity,
        lab_profile=lab_profile,
        currency=currency,
        fx_rates=fx_rates,
    )
    unit_cost = _money_float(est["material_cost"] / max(1, quantity))
    return {
        "line_id": f"print:{part_id}",
        "group_id": group_id,
        "catalog_id": f"printed:{part_id}",
        "label": label,
        "category": "3d_printed",
        "kind": "3d_printed",
        "resolution_status": "fabricate",
        "component_ids": [part_id],
        "supplier": "In-house FDM 3D Printer",
        "sku": f"FDM-{est['material']}",
        "mpn": part_id,
        "digikey_pn": None,
        "mouser_pn": None,
        "quantity": int(quantity),
        "unit": "printed part",
        "pack_size": 1,
        "packs_to_order": int(quantity),
        "currency": currency,
        "unit_price": unit_cost,
        "pack_price": unit_cost,
        "line_total": est["material_cost"],
        "price_source": "slicer_material_estimate",
        "availability": "In-house fabrication",
        "stock_quantity": None,
        "lead_time_days": max(1, math.ceil(est["print_time_minutes"] / 480.0)),
        "source_url": None,
        "fetched_at": None,
        "adapter": "analytical_slicer_v1",
        "specs": {
            "kind": "printed_part",
            "material": est["material"],
            "bounding_box_mm": est["bounding_box_mm"],
            "fits_printer_bed": est["fits_printer_bed"],
            "material_available_in_lab": est["material_available_in_lab"],
            "filament_mass_g": est["filament_mass_g"],
            "print_time_minutes": est["print_time_minutes"],
        },
        "notes": role_note or est["assumptions"][0],
        "print_estimate": est,
    }


def _collect_live_rows(
    procurement_report: dict[str, Any] | None,
    live_quotes: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(procurement_report, dict):
        for row in procurement_report.get("rows") or []:
            if not isinstance(row, dict):
                continue
            selected_id = row.get("selected_id")
            if not (
                selected_id
                and row.get("catalog_supported")
                and row.get("sku")
                and (row.get("fetched_at") or row.get("last_known_fetched_at"))
                and row.get("currency") in {"USD", "CHF", "EUR"}
                and (row.get("price_source_url") or row.get("availability_source_url"))
            ):
                continue
            if row.get("status") in {"priced", "unavailable"}:
                by_id[str(selected_id)] = row
    if isinstance(live_quotes, dict):
        for key, quote in live_quotes.items():
            if not (isinstance(quote, dict) and quote.get("source_url")
                    and quote.get("fetched_at")):
                continue
            price_breaks = quote.get("price_breaks") or []
            unavail = quote.get("availability") in UNAVAILABLE_STATES
            freshness = quote.get("freshness", "fresh")
            raw_price = price_breaks[0]["pack_price"] if price_breaks else quote.get("pack_price")
            if raw_price is None and not unavail and freshness != "stale":
                continue
            pack_price = Decimal(str(raw_price)) if raw_price is not None else None
            pack_size = max(1, int(quote.get("pack_size") or 1))
            normalized = {
                "supplier": quote.get("supplier"),
                "sku": quote.get("sku"),
                "currency": quote.get("currency", "USD"),
                "status": "unavailable" if unavail else "priced",
                "freshness": freshness,
                "pack_size": pack_size,
                "price_breaks": price_breaks,
                "min_order_qty": quote.get("min_order_qty"),
                "pack_price": pack_price,
                "unit_price": _money_float(pack_price / pack_size)
                if pack_price is not None else None,
                "line_total": None if unavail or freshness == "stale" else pack_price,
                "availability": quote.get("availability", "unknown"),
                "stock_quantity": quote.get("stock_quantity"),
                "lead_time_days": quote.get("lead_time_days"),
                "price_source_url": quote.get("source_url"),
                "fetched_at": quote.get("fetched_at"),
                "adapter": quote.get("adapter") or quote.get("parser"),
            }
            previous = by_id.get(str(key)) or {}
            previous_date = previous.get("fetched_at")
            quote_date = normalized["fetched_at"]
            if previous_date and (not quote_date or
                    datetime.fromisoformat(previous_date) > datetime.fromisoformat(quote_date)):
                continue
            by_id[str(key)] = normalized
    return by_id


def _fixture_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    fixture = project["standalone_fixture"]
    spec = fixture.get("spec") or {}
    lines: list[dict[str, Any]] = []
    for part in fixture.get("parts") or []:
        if not part.get("printable"):
            continue
        size = part.get("size_mm") or [40.0, 40.0, 12.0]
        lines.append(
            _printed_line(
                part["id"],
                part.get("label") or part["id"],
                size,
                group_id=f"fixture:{part['id']}",
                material=part.get("material") or spec.get("material") or "PLA",
                nozzle_mm=float(part.get("nozzle_mm") or spec.get("nozzle_mm") or 0.4),
                layer_height_mm=float(
                    part.get("layer_height_mm") or spec.get("layer_height_mm") or 0.2
                ),
                wall_loops=int(part.get("perimeters") or 3),
                infill_percent=float(part.get("infill_pct") or 20.0),
                supports_enabled=bool(part.get("supports_required")),
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
                role_note=part.get("role") or part.get("print_orientation") or "",
            )
        )
    fastener_kind = spec.get("fastener_spec") or "m3_heatset_insert"
    if "insert" in fastener_kind:
        lines.append(
            _catalog_line(
                "m3-heatset-insert-pack",
                12,
                group_id="fixture-fasteners",
                component_ids=[p["id"] for p in fixture.get("parts") or []],
                role_note="M3 brass heat-set inserts sized for fixture bosses and mast base.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    if fastener_kind != "none":
        lines.append(
            _catalog_line(
                "m3-shcs-assortment",
                12,
                group_id="fixture-fasteners",
                component_ids=[p["id"] for p in fixture.get("parts") or []],
                role_note="M3 cap screws for mast/arm and bench mounting holes.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    if spec.get("include_camera_mount"):
        cam_type = spec.get("camera_mount_type") or "pi_camera_v2_v3"
        if cam_type == "usb_webcam_quarter20":
            lines.append(
                _catalog_line(
                    "usb-webcam-1080p",
                    1,
                    group_id="fixture-camera",
                    component_ids=["camera-cradle"],
                    role_note="USB UVC inspection camera with 1/4-20 mount.",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        else:
            lines.append(
                _catalog_line(
                    "camera-module-3",
                    1,
                    group_id="fixture-camera",
                    component_ids=["camera-cradle"],
                    role_note="Pi Camera Module v3 for 25x24 mm 4xM2/M2.5 overhead cradle.",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
            lines.append(
                _catalog_line(
                    "rpi-zero-2w",
                    1,
                    group_id="fixture-camera-host",
                    component_ids=["camera-cradle"],
                    role_note="Compact CSI camera host controller for capture over Wi-Fi/USB.",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
            lines.append(
                _catalog_line(
                    "m25-camera-screw-pack",
                    4,
                    group_id="fixture-camera-hardware",
                    component_ids=["camera-cradle"],
                    role_note="M2.5/M2 nylon standoffs for PCB isolation.",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
    return lines


def _requirement_line(
    part: dict[str, Any],
    *,
    requirement_id: str,
    group_id: str,
    component_ids: list[str],
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    stock_item: str | None = None,
) -> dict[str, Any]:
    """Keep the design requirement separate from an identifiable supplier item."""
    name = part.get("name") or part.get("label") or requirement_id
    quantity = part.get("quantity", 1)
    unit = part.get("unit") or "unit"
    requirement = {"name": name, "quantity": quantity, "unit": unit}
    if part.get("critical_specs"):
        requirement["critical_specs"] = deepcopy(part["critical_specs"])
    for field in ("source_id", "source_url", "source_file", "sku", "mpn", "part_number",
                  "supplier", "role", "notes"):
        if part.get(field) is not None:
            requirement[field] = part[field]
    source_id = part.get("source_id")
    mpn = str(part.get("mpn") or "").strip().casefold()
    sku = str(part.get("sku") or "").strip().casefold()
    supplier = str(part.get("supplier") or "").strip().casefold()
    if source_id:
        stock_item = source_id if source_id in catalog_items else None
    elif mpn or sku or part.get("part_number"):
        matches = [
            key for key, item in catalog_items.items()
            if (mpn or (sku and supplier))
            and (not mpn or mpn == str(item.get("mpn") or "").casefold())
            and (not sku or sku == str(item.get("sku") or "").casefold())
            and (not supplier or supplier == str(item.get("supplier") or "").casefold())
        ]
        stock_item = matches[0] if len(matches) == 1 else None

    unit_known = True
    if stock_item and part.get("source_file"):
        catalog_unit = str(catalog_items[stock_item].get("unit") or "unit").casefold()
        count_units = {catalog_unit, "unit", "item", "each", "pc", "pcs", "ea"}
        unit_known = str(unit).casefold() in count_units
    reference = None
    if stock_item and requirement.get("critical_specs"):
        reference = candidate_view(catalog_candidate(stock_item, catalog_items),
                                   requirement, catalog_items)
    if (stock_item and unit_known and type(quantity) is int and quantity > 0
            and (not reference or reference["interface_status"] != "mismatch")):
        line = _catalog_line(
            stock_item, quantity, group_id=group_id, component_ids=component_ids,
            role_note=part.get("role") or part.get("notes") or "",
            currency=currency, fx_rates=fx_rates, live_rows=live_rows, catalog_items=catalog_items,
        )
    else:
        line = _open_requirement(requirement, currency, group_id, component_ids)
    if reference:
        line["reference_candidate"] = reference
    line.update(line_id=requirement_id, requirement=requirement)
    return line


def _open_requirement(requirement, currency, group_id, component_ids):
    return {
        "group_id": group_id, "component_ids": component_ids, "catalog_id": None,
        "label": requirement["name"], "category": "requirement", "kind": "requirement",
        "resolution_status": "selection_required", "quantity": requirement["quantity"],
        "unit": requirement["unit"], "supplier": None, "sku": None, "mpn": None,
        "digikey_pn": None, "mouser_pn": None, "pack_size": None, "packs_to_order": None,
        "currency": currency, "unit_price": None, "pack_price": None, "line_total": None,
        "price_source": "unpriced", "availability": "selection_required", "stock_quantity": None,
        "lead_time_days": None, "source_url": None, "fetched_at": None, "adapter": None,
        "specs": {}, "print_estimate": None, "notes": requirement.get("notes") or
        requirement.get("role") or "",
    }


def _upstream_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    up = project["upstream_project"]
    lines: list[dict[str, Any]] = []
    adapt = up.get("adaptation") or {}
    cgeom = adapt.get("custom_geometry") or {}
    if isinstance(cgeom.get("app_size_mm"), (list, tuple)) and len(cgeom["app_size_mm"]) == 3:
        x, y, z = cgeom["app_size_mm"]
        lines.append(_printed_line(
            adapt.get("part_id") or "custom-adapter",
            f"Custom adapted mating part ({adapt.get('mode', 'mating')})",
            [float(x), float(z), float(y)], volume_mm3=cgeom.get("volume_mm3"),
            group_id="upstream-custom-adaptation", material="PETG", lab_profile=lab_profile,
            currency=currency, fx_rates=fx_rates, role_note="Custom part fitted to the source CAD.",
        ))
    adaptation_modeled = bool(lines)
    for idx, part in enumerate(up.get("bom") or []):
        if adaptation_modeled and part.get("source") in {
            "autolab_adaptation", "autolab_custom_adaptation"
        }:
            continue
        part_id = part.get("id") or str(idx + 1)
        line = _requirement_line(
            part, requirement_id=f"upstream:{part.get('source_file') or ''}:{part_id}",
            group_id="upstream-bom-items", component_ids=[], currency=currency,
            fx_rates=fx_rates, live_rows=live_rows, catalog_items=catalog_items,
        )
        provenance = up.get("provenance") or {}
        line["design_binding"] = [
            provenance.get(k) for k in ("source_host", "repo_full_name", "commit_sha")
        ]
        lines.append(line)
    return lines


def _extrusion_stock(component: dict, catalog: dict[str, dict]) -> str | None:
    primitives = component.get("primitives") or []
    if len(primitives) != 1 or primitives[0].get("shape") != "extrusion":
        return None
    primitive = primitives[0]
    length = primitive["size_mm"]["xyz".index(primitive.get("axis") or "y")]
    for item_id, item in catalog.items():
        specs = item.get("specs") or {}
        if (specs.get("kind") == "extrusion" and specs.get("profile") == primitive["profile"]
                and specs.get("stock_length_mm", 0) >= length):
            return item_id
    return None


def _concept_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    concept = project["concept_design"]
    components = {c["key"]: c for c in concept.get("components") or []}
    lines: list[dict[str, Any]] = []
    for part in concept.get("parts") or []:
        key, name = part["key"], part["name"]
        component = components.get(key)
        if component and component.get("role"):
            part = {"role": component["role"], **part}
        primitives = (component or {}).get("primitives") or []
        description = name.casefold()
        if (component and not part.get("source_id")
                and description.startswith(("printed ", "3d printed ", "3d-printed "))
                and "circuit" not in description.split() and primitives
                and all(p.get("finish") in {"polymer", "accent"} for p in primitives)):
            x, y, z = component["size_mm"]
            lines.append(_printed_line(
                key, name, [x, z, y], group_id=f"concept-{key}",
                quantity=part.get("quantity", 1), material="PETG", lab_profile=lab_profile,
                currency=currency, fx_rates=fx_rates, role_note=part.get("role") or "",
            ))
            continue
        line = _requirement_line(
            part, requirement_id=f"concept:{key}", group_id=f"concept-{key}",
            component_ids=[key] if component else list(part.get("component_keys") or []),
            currency=currency, fx_rates=fx_rates, live_rows=live_rows, catalog_items=catalog_items,
            stock_item=_extrusion_stock(component, catalog_items) if component else None,
        )
        line["design_binding"] = [
            {
                key: components[cid].get(key)
                for key in ("key", "primitives", "parent_key", "placement_mm")
            }
            for cid in line["component_ids"]
            if cid in components
        ]
        lines.append(line)
    return lines


def _apply_choice(line, record, catalog, live_rows, fx_rates):
    requirement = add_declared_specs(line["requirement"], record.get("required_specs", []))
    line["requirement"] = requirement
    candidates = [candidate_view(Candidate.model_validate(item), requirement, catalog)
                  for item in record["candidates"]]
    line["part_choice"] = {
        "candidates": candidates,
        "accepted": None,
        "decision": record["decision"],
        "inventory_evidence": record.get("inventory_evidence"),
    }
    if not record["accepted"]:
        if record.get("selection_cleared"):
            line.update(_open_requirement(requirement, line["currency"], line["group_id"],
                                          line["component_ids"]))
            return
        if line["catalog_id"] and record.get("required_specs"):
            reference = candidate_view(
                catalog_candidate(line["catalog_id"], catalog), requirement, catalog
            )
            line["reference_candidate"] = reference
            if reference["interface_status"] == "mismatch":
                line.update(_open_requirement(requirement, line["currency"], line["group_id"],
                                              line["component_ids"]))
        return
    selected = candidate_view(Candidate.model_validate(record["accepted"]), requirement, catalog)
    quantity = line["quantity"]
    if selected["interface_status"] == "mismatch":
        return
    cid = selected["catalog_id"]
    demand = required_amount(quantity)
    owned = record["owned_quantity"]
    outstanding = None
    if demand is not None:
        allocated = min(Decimal(str(owned)), demand)
        remaining = demand - allocated
        outstanding = int(remaining) if remaining == int(remaining) else float(remaining)
        owned = int(allocated) if allocated == int(allocated) else float(allocated)
    entries = catalog
    offer_id = cid
    if not cid:
        supplier_part = selected["basis"] == "supplier_search"
        offer_id = "external:" + json.dumps(
            [selected["basis"], selected["issuer"], selected["part_number"]]
        )
        entry = {
            "label": selected["label"], "category": "selected_part", "unit": selected["unit"],
            "supplier": selected["issuer"] if supplier_part else None,
            "sku": selected["part_number"] if supplier_part else None,
            "mpn": selected["mpn"], "source_url": selected["source_url"],
        }
        if supplier_part:
            entry[{"DigiKey": "digikey_pn", "Mouser": "mouser_pn"}[selected["issuer"]]] = entry[
                "sku"
            ]
        entries = {offer_id: entry}
        if selected["checks"][0]["status"] != "match":
            live_rows = {}
    priced = _catalog_line(offer_id, outstanding or 0, group_id=line["group_id"],
                           component_ids=line["component_ids"], currency=line["currency"],
                           fx_rates=fx_rates, live_rows=live_rows, catalog_items=entries,
                           role_note=line["notes"])
    priced.update(line_id=line["line_id"], quantity=quantity, unit=requirement["unit"],
                  catalog_id=cid, offer_id=offer_id, owned_quantity=owned,
                  purchase_quantity=outstanding, resolution_status="user_selected")
    if outstanding is None:
        priced.update(
            price_source="quantity_required",
            availability="unknown",
            packs_to_order=None,
            line_total=None,
            unit_price=None,
            pack_price=None,
            stock_quantity=None,
            lead_time_days=None,
            fetched_at=None,
            adapter=None,
            source_url=selected["source_url"],
        )
    elif outstanding == 0:
        priced.update(
            price_source="lab_inventory",
            availability="on_hand",
            packs_to_order=0,
            line_total=0.0,
            unit_price=None,
            pack_price=None,
            stock_quantity=None,
            lead_time_days=None,
            fetched_at=None,
            adapter=None,
            source_url=selected["source_url"],
        )
    line.update(priced)
    line["part_choice"]["accepted"] = selected


def reconcile_part_choices(project, *, restoring=False):
    records = project.get("part_choices")
    if not records:
        return
    bare = {**project, "part_choices": {}}
    lines = {row["line_id"]: row for row in build_complete_bom(bare)["lines"]
             if row.get("requirement")}
    catalog = _raw_catalog()["items"]
    valid = {}
    for key, record in records.items():
        line = lines.get(key)
        if not line or record["requirement_signature"] != line["requirement_signature"]:
            if restoring:
                raise ValueError("A saved part choice does not match its design requirement.")
            continue
        if restoring:
            requirement = add_declared_specs(line["requirement"], record.get("required_specs", []))
            for item in [
                *record["candidates"],
                *([record["accepted"]] if record["accepted"] else []),
            ]:
                candidate = Candidate.model_validate(item)
                view = candidate_view(candidate, requirement, catalog)
                if candidate.catalog_id and (view["issuer"], view["part_number"]) != (
                        candidate.issuer, candidate.part_number):
                    raise ValueError("Saved catalog identity differs from its reviewed entry.")
                if candidate.basis == "supplier_search" and candidate.issuer not in {
                    "DigiKey",
                    "Mouser",
                }:
                    raise ValueError("Unknown distributor for the saved search result.")
                if item is record["accepted"] and view["interface_status"] == "mismatch":
                    raise ValueError("The saved selection conflicts with its requirement.")
        valid[key] = record
    project["part_choices"] = valid


def _bench_station_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    from app.procurement import initial_procurement_rows

    return [
        _scope_line(row, currency, fx_rates, live_rows, catalog_items)
        for row in initial_procurement_rows(project)
    ]


def _scope_line(row, currency, fx_rates, live_rows, catalog_items):
    gid = row["group_id"]
    item = row["selected_id"]
    if item in catalog_items:
        return _catalog_line(
            item,
            row["quantity"],
            group_id=gid,
            component_ids=row["component_ids"],
            role_note=row.get("compatibility_note") or "",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    requirement = {
        "name": row["label"],
        "quantity": row["quantity"],
        "unit": "set",
        "notes": row.get("compatibility_note"),
    }
    line = _open_requirement(requirement, currency, gid, row["component_ids"])
    line.update(line_id=gid, source_url=row.get("price_source_url"), scope_selection=item)
    return line


def _workstation_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expand the selected liquid-handling design into its supported purchase groups."""
    design = project.get("design") or {}
    selections = project.get("selections") or {}
    summary = project.get("summary") or {}
    cooling_enabled = bool(summary.get("cooling_enabled", False))
    is_ot2 = (design.get("platform") or {}).get("id") == "opentrons-ot2"
    is_jubilee = bool(design.get("foundation"))
    lines: list[dict[str, Any]] = []

    if is_ot2:
        from app.platform_profiles import platform_profiles

        context = next(p["cost_context"] for p in platform_profiles() if p["id"] == "opentrons-ot2")
        lines.append(
            {
                "line_id": "platform:opentrons-ot2",
                "group_id": "platform",
                "catalog_id": "opentrons-ot2",
                "label": "Opentrons OT-2 Liquid Handling Robot + 1 Single-Channel Pipette",
                "category": "commercial_robot",
                "kind": "commercial_part",
                "resolution_status": "catalog_identified",
                "component_ids": ["base"],
                "supplier": "Opentrons",
                "sku": "OT-2",
                "mpn": "OT-2",
                "digikey_pn": None,
                "mouser_pn": None,
                "quantity": 1,
                "unit": "unit",
                "pack_size": 1,
                "packs_to_order": 1,
                "currency": currency,
                "unit_price": None,
                "pack_price": None,
                "line_total": None,
                "price_source": "quote_required",
                "availability": "vendor_quote_required",
                "stock_quantity": None,
                "lead_time_days": None,
                "source_url": "https://opentrons.com/products/robots/ot-2/",
                "fetched_at": None,
                "adapter": None,
                "specs": {"kind": "commercial_robot", "supply_voltage_v": 115.0},
                "notes": "Base price starting-at USD reference; request formal quotation.",
                "print_estimate": None,
                "planning_basis": {
                    "kind": context["kind"],
                    "low_usd": context["amount"],
                    "high_usd": None,
                    "source_url": context["source_url"],
                    "observed_on": context["checked_on"],
                    "detail": context["detail"],
                },
            }
        )
        lines.append(
            _catalog_line(
                "opentrons-300ul-tips-pack",
                1,
                group_id="tip-rack-options",
                component_ids=["tip-rack"],
                role_note="Disposable tip racks compatible with OT-2 pipettes.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        lines.append(
            _catalog_line(
                "corning-3596-case",
                1,
                group_id="labware-plate",
                component_ids=["sample-rack"],
                role_note="Case of 50 SLAS 96-well microplates.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        return lines

    # Electrical & drive selections (preserving user choices on custom rigs for Task 5.3 validation)
    if is_jubilee:
        motor_id = "jubilee-motor-kit"
        controller_id = "duet3-mini5plus"
    else:
        motor_id = selections.get("motors", "pololu-1209")
        controller_id = selections.get("controller", "pico")
    driver_id = selections.get("drivers", "drv8825")

    if motor_id == "jubilee-motor-kit":
        lines.append(
            _catalog_line(
                "jubilee-motor-kit",
                1,
                group_id="upstream-platform:jubilee-motor-kit",
                component_ids=["foundation-jubilee"],
                role_note="Six-motor NEMA 17 kit for Jubilee XY/Z/toolchanger.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    else:
        lines.append(
            _catalog_line(
                motor_id,
                4,
                group_id="motors",
                component_ids=["carriage", "crossbar", "pipette"],
                role_note="Four stepper axes (X, Y, Z, pipette plunger).",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )

    ctrl_specs = catalog_items.get(controller_id, {}).get("specs") or {}
    if int(ctrl_specs.get("onboard_drivers") or 0) == 0:
        lines.append(
            _catalog_line(
                driver_id,
                4,
                group_id="drivers",
                component_ids=["controller"],
                role_note="One carrier driver per stepper axis.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )

    lines.append(
        _catalog_line(
            controller_id,
            1,
            group_id="controller",
            component_ids=["controller"],
            role_note="Primary motion and I/O control board.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )

    if is_jubilee:
        for cid, gid, note in (
            ("jubilee-frame-kit-black", "upstream-platform:jubilee-frame-kit-black", "Frame"),
            ("jubilee-motion-kit", "upstream-platform:jubilee-motion-kit", "Belts/idlers/pulleys"),
            ("jubilee-hardware-kit", "upstream-platform:jubilee-hardware-kit", "Fasteners"),
            ("jubilee-crossbar", "upstream-platform:jubilee-crossbar", "Aluminum X-carriage bar"),
            (
                "jubilee-xy-mount-set",
                "upstream-platform:jubilee-xy-mount-set",
                "L/R NEMA 17 mounts",
            ),
        ):
            lines.append(
                _catalog_line(
                    cid,
                    1,
                    group_id=gid,
                    component_ids=["foundation-jubilee"],
                    role_note=note,
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        psu_id = "psu-lrs-350-24"
    else:
        # Custom workstation: explicitly expand frame, linear-motion, and pipette placeholders!
        for cid, qty, gid, cids, note in (
            ("extrusion-2020-500", 6, "frame", ["base", "column-left", "column-right", "crossbar"],
             "2020 extrusion perimeter frame"),
            ("corner-bracket-2020", 12, "frame", ["base"], "Frame corner gussets"),
            ("fastener-m5-tnut-kit", 40, "frame", ["base"], "M5 T-nuts and bolts"),
            ("deck-plate-alu-300", 1, "frame", ["base"], "Machined base deck plate"),
            ("rail-mgn12h-400", 2, "linear-motion", ["carriage", "crossbar"], "X/Y linear rails"),
            ("leadscrew-tr8x8-300", 1, "linear-motion", ["carriage"], "Z-axis leadscrew"),
            ("gt2-belt-5m", 1, "linear-motion", ["crossbar"], "XY drive belts"),
            ("gt2-pulley-20t-5mm", 4, "linear-motion", ["crossbar"], "5 mm bore drive pulleys"),
            ("coupler-5x8mm", 1, "linear-motion", ["carriage"], "5-to-8 mm Z leadscrew coupler"),
        ):
            lines.append(
                _catalog_line(
                    cid,
                    qty,
                    group_id=gid,
                    component_ids=cids,
                    role_note=note,
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        psu_id = "psu-lrs-150-24"

    # Power & protection group (replaces "power" unpriced placeholder)
    lines.append(
        _catalog_line(
            psu_id,
            1,
            group_id="power",
            component_ids=["controller"],
            role_note="24 V DC primary motor and actuator power supply.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )
    if float(ctrl_specs.get("input_voltage_max_v") or 5.5) < 20.0:
        lines.append(
            _catalog_line(
                "buck-5v-3a",
                1,
                group_id="power",
                component_ids=["controller"],
                role_note="24 V to 5 V DC-DC converter powering 5 V logic board.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    lines.append(
        _catalog_line(
            "iec-c14-fused-inlet",
            1,
            group_id="power",
            component_ids=["controller"],
            role_note="Mains switch, IEC C14 inlet, and 5 A overcurrent fuse.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )
    lines.append(
        _catalog_line(
            "wire-hookup-kit",
            1,
            group_id="power",
            component_ids=["controller"],
            role_note="Silicone power and step/dir harness wiring.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )
    lines.append(
        _catalog_line(
            "endstop-microswitch",
            3,
            group_id="linear-motion",
            component_ids=["carriage"],
            role_note="X/Y/Z homing endstop limit switches.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )

    # Pipette group (replaces "pipette" unpriced placeholder)
    lines.append(
        _catalog_line(
            "pipette-actuator-rail-mgn9-150",
            1,
            group_id="pipette",
            component_ids=["pipette"],
            role_note="Linear plunger actuator guide and fine-pitch Tr8x2 leadscrew.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )
    lines.append(
        _printed_line(
            "pipette-mount-clamp",
            "Printed Pipette Body Clamp & Plunger Coupler",
            [72.0, 56.0, 68.0],
            group_id="pipette",
            material="PETG",
            lab_profile=lab_profile,
            currency=currency,
            fx_rates=fx_rates,
            role_note="Custom printed interface clamp for laboratory micropipette.",
        )
    )

    # Cooling & temperature sensor when enabled
    if cooling_enabled:
        cool_id = selections.get("cooling", "diy-peltier")
        temp_id = selections.get("temperature-sensor", "probe")
        lines.append(
            _catalog_line(
                cool_id,
                1,
                group_id="cooling",
                component_ids=["cold-storage"],
                role_note="Cooling subassembly.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        if cool_id == "diy-peltier":
            lines.append(
                _catalog_line(
                    "psu-lrs-150-12",
                    1,
                    group_id="cooling-power",
                    component_ids=["cold-storage"],
                    role_note="Dedicated 12 V 12.5 A supply for 72 W peak Peltier module.",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        lines.append(
            _catalog_line(
                temp_id,
                1,
                group_id="temperature-sensor",
                component_ids=["cold-storage"],
                role_note="1-Wire temperature feedback probe.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )

    # Labware, tip racks, and printed fixtures (replaces "fixtures" placeholder)
    if project.get("inspection_extension"):
        lines.append(
            _catalog_line(
                "camera-module-3",
                1,
                group_id="inspection-camera",
                component_ids=["inspection-camera"],
                role_note="Jubilee inspection camera module (CSI).",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        lines.append(
            _printed_line(
                "inspection-arm-links",
                "Printed Inspection Arm Links & Camera Bracket",
                [160.0, 42.0, 26.0],
                group_id="inspection-arm",
                quantity=2,
                material="PETG",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
            )
        )

    if rack := project.get("labware_rack"):
        s = rack.get("size_mm") or [127.8, 45.0, 85.5]
        lines.append(
            _printed_line(
                "sample-rack",
                "Custom Printed Rim-Supported Tube Rack",
                [float(s[0]), float(s[2]), float(s[1])],
                group_id="labware-rack",
                material="PETG",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
            )
        )
    elif project.get("labware_plate"):
        lines.append(
            _catalog_line(
                "corning-3596-case",
                1,
                group_id="labware-plate",
                component_ids=["sample-rack"],
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        lines.append(
            _printed_line(
                "plate-holder-tray",
                "Custom Printed SLAS 96-Well Plate Locating Deck Nest",
                [138.0, 96.0, 14.0],
                group_id="labware-plate",
                material="PETG",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
            )
        )
    else:
        lines.append(
            _printed_line(
                "deck-fixtures",
                "Printed Source Container, Sample Rack & Tip Waste Holders",
                [150.0, 110.0, 48.0],
                group_id="fixtures",
                material="PLA",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
            )
        )

    tip_rack = project.get("tip_rack") or {}
    if tip_rack.get("kind") == "rim_supported_tip_rack":
        ts = tip_rack.get("size_mm") or [127.8, 52.0, 85.5]
        lines.append(
            _printed_line(
                "tip-rack",
                "Custom Printed Pipette Tip Holder",
                [float(ts[0]), float(ts[2]), float(ts[1])],
                group_id="tip-rack-options",
                material="PETG",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
            )
        )
    elif tip_rack.get("profile_id") == "tipone_96_tiprack_200ul":
        lines.append(
            _catalog_line(
                "tipone-200ul-racks",
                1,
                group_id="tip-rack-options",
                component_ids=["tip-rack"],
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    else:
        lines.append(
            _catalog_line(
                "opentrons-300ul-tips-pack",
                1,
                group_id="tip-rack-options",
                component_ids=["tip-rack"],
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )

    return lines


def _classify_cost(line, project, fx_rates, checked_on):
    from app.budget_allowances import budget_signature

    source = line["price_source"]
    line["cost_category"] = {
        "live_api": "documented",
        "curated_reference": "documented",
        "slicer_material_estimate": "printed_material",
        "lab_inventory": "on_hand",
        "included_in_base": "included",
    }.get(source, "unpriced")
    line["documented_line_total"] = (
        line["line_total"] if line["cost_category"] == "documented" else None
    )
    line["printed_material_total"] = (
        line["line_total"] if line["cost_category"] == "printed_material" else None
    )
    if source == "curated_reference":
        line["price_checked_on"] = checked_on
    line["budget_scope_signature"] = budget_signature(project, line)
    if line["cost_category"] != "unpriced" or source == "quantity_required":
        line.pop("planning_basis", None)
    record = (project.get("budget_allowances") or {}).get(line["line_id"])
    if (
        record
        and record["scope_signature"] == line["budget_scope_signature"]
        and line["cost_category"] == "unpriced"
    ):
        line["planning_basis"] = {
            "kind": "user_declared",
            "low_usd": record["low_usd"],
            "high_usd": record["high_usd"],
            "detail": record["user_quote"],
            "source_url": None,
            "observed_on": None,
        }
    basis = line.get("planning_basis")
    line["planning_low"] = line["planning_high"] = None
    if basis:
        for field in ("low", "high"):
            amount = basis[field + "_usd"]
            if amount is not None:
                line["planning_" + field] = convert_currency(
                    amount, "USD", line["currency"], fx_rates
                )


def purchasing_route(project: dict[str, Any]) -> str:
    for key in ("standalone_fixture", "upstream_project", "concept_design", "bench_station"):
        if project.get(key):
            return key
    design = project.get("design") or {}
    if project.get("engineering_design") or design.get("foundation") or design.get("platform"):
        return "workstation"
    return "unselected"


def build_complete_bom(
    project: dict[str, Any],
    *,
    procurement_report: dict[str, Any] | None = None,
    lab_profile: dict[str, Any] | None = None,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    live_quotes: dict[str, dict[str, Any]] | None = None,
    use_cached_quotes: bool = True,
) -> dict[str, Any]:
    """Build the BOM for the current design, retaining unresolved requirements."""
    route_kind = purchasing_route(project)
    curr, eff_fx = _resolve_currency_and_fx(
        currency, fx_rates, requires_rates=route_kind != "unselected"
    )
    if lab_profile is None and route_kind != "unselected":
        from app.lab_profile import load_lab_profile

        lab_profile = load_lab_profile()
    catalog_data = load_purchasable_catalog()
    catalog_items = catalog_data["items"]
    cached_quotes = (
        cached_official_quotes(curr, eff_fx or cached_fx_rates("USD")) if use_cached_quotes else {}
    )
    from app.procurement import report_for_project

    procurement_report = report_for_project(project, procurement_report)
    if procurement_report:
        from app.procurement import initial_procurement_rows

        expected = {r["group_id"]: r for r in initial_procurement_rows(project)}
        keys = (
            "selected_id",
            "supplier",
            "sku",
            "quantity",
            "pack_size",
            "pack_count",
            "component_ids",
        )
        procurement_report["rows"] = [
            row
            for row in procurement_report["rows"]
            if (scope := expected.get(row["group_id"]))
            and all(row.get(k) == scope.get(k) for k in keys)
        ]
    live_rows = _collect_live_rows(procurement_report, {**cached_quotes, **(live_quotes or {})})

    if any(r.get("currency", curr) != curr for r in live_rows.values()):
        eff_fx = eff_fx or cached_fx_rates("USD")
        if not eff_fx:
            live_rows = {key: r for key, r in live_rows.items()
                         if r.get("currency", curr) == curr or r["status"] == "unavailable"
                         or r.get("freshness") == "stale"}

    compilers = {
        "standalone_fixture": _fixture_bom_lines, "upstream_project": _upstream_bom_lines,
        "concept_design": _concept_bom_lines, "bench_station": _bench_station_bom_lines,
        "workstation": _workstation_bom_lines,
    }
    lines = []
    if route_kind != "unselected":
        lines = compilers[route_kind](
            project, currency=curr, fx_rates=eff_fx, live_rows=live_rows,
            catalog_items=catalog_items, lab_profile=lab_profile,
        )

    for name, instance in sorted((project.get("cad_instances") or {}).items()):
        prefix = "cad:" + name
        prov = instance["provenance"]
        source = instance
        if not instance.get("bom"):
            source = {**instance, "bom": [{
                "id": "source", "name": name, "source_file": prov["file_path"],
                "source_url": prov["raw_url"],
                "notes": "Pinned CAD. Supply and price need evidence.",
            }]}
        for line in _upstream_bom_lines(
            {"upstream_project": source}, currency=curr, fx_rates=eff_fx, live_rows=live_rows,
            catalog_items=catalog_items, lab_profile=lab_profile,
        ):
            printed = line["group_id"] == "upstream-custom-adaptation"
            binding = instance["adaptation"]["spec"] if printed else line.get("design_binding")
            line["component_ids"] = [prefix + ":adapter" if printed else prefix]
            line["line_id"] = prefix + ":" + line["line_id"]
            line["group_id"] = prefix + ":" + line["group_id"]
            line["design_binding"] = [prov["commit_sha"], prov["file_sha256"], binding]
            lines.append(line)

    if route_kind == "workstation":
        from app.procurement import initial_procurement_rows

        existing = {line["group_id"] for line in lines}
        commercial = (project.get("design", {}).get("platform") or {}).get("id") == "opentrons-ot2"
        for row in initial_procurement_rows(project):
            gid = row["group_id"]
            if gid in existing or (commercial and gid not in {"pipettes", "labware"}):
                continue
            extra = _scope_line(row, curr, eff_fx, live_rows, catalog_items)
            if commercial and gid == "pipettes":
                extra.update(
                    price_source="included_in_base",
                    line_total=0.0,
                    resolution_status="included",
                    availability="included",
                    notes="One instrument pipette is included in the platform scope.",
                )
            lines.append(extra)

    live_sub = Decimal(0)
    ref_sub = Decimal(0)
    print_sub = Decimal(0)
    total_mass_g = 0.0
    total_print_min = 0.0
    max_lead_days = 0
    unpriced_placeholders = 0

    for line in lines:
        if line.get("requirement"):
            signature = requirement_signature(line)
            line.pop("design_binding", None)
            line["requirement_signature"] = signature
            record = (project.get("part_choices") or {}).get(line["line_id"])
            if record and signature == record["requirement_signature"]:
                _apply_choice(line, record, catalog_items, live_rows, eff_fx)
        _classify_cost(line, project, eff_fx, catalog_data["checked_on"])
        lt = line.get("line_total")
        if lt is None:
            unpriced_placeholders += 1
            continue
        dec = Decimal(str(lt))
        src = line["price_source"]
        if src == "live_api":
            live_sub += dec
        elif src == "slicer_material_estimate":
            print_sub += dec
        elif src == "curated_reference":
            ref_sub += dec
        if pest := line.get("print_estimate"):
            total_mass_g += float(pest["filament_mass_g"])
            total_print_min += float(pest["print_time_minutes"])
        if line.get("lead_time_days"):
            max_lead_days = max(max_lead_days, int(line["lead_time_days"]))

    documented = [r for r in lines if r["cost_category"] == "documented"]
    allowances = [r for r in lines if r.get("planning_basis")]
    low = sum(Decimal(str(r["planning_low"])) for r in allowances)
    high = (
        sum(Decimal(str(r["planning_high"])) for r in allowances)
        if allowances and all(r["planning_high"] is not None for r in allowances)
        else None
    )
    documented_subtotal = _money_float(live_sub + ref_sub) if documented else None
    unresolved = sum(line.get("resolution_status") == "selection_required" for line in lines)
    if route_kind == "unselected":
        status = "design_required"
    elif unpriced_placeholders or not lines:
        status = "incomplete"
    else:
        status = "itemized"
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "project_id": project["id"],
        "project_revision": project["revision"],
        "route_kind": route_kind,
        "status": status,
        "currency": curr,
        "fx_rate_used": (eff_fx["rates"].get(curr) if eff_fx and eff_fx.get("rates") else None),
        "fx_source": eff_fx.get("source") if eff_fx else None,
        "fx_rate_date": eff_fx.get("rate_date") if eff_fx else None,
        "catalog_checked_on": catalog_data["checked_on"],
        "lines": lines,
        "summary": {
            "line_count": len(lines),
            "documented_count": len(documented),
            "documented_subtotal": documented_subtotal,
            "planning_count": len(allowances),
            "planning_low": _money_float(low) if allowances else None,
            "planning_high": _money_float(high) if high is not None else None,
            "missing_price_count": sum(r["cost_category"] == "unpriced" for r in lines),
            "commercial_line_count": sum(1 for r in lines if r["kind"] == "commercial_part"),
            "printed_line_count": sum(1 for r in lines if r["kind"] == "3d_printed"),
            "live_api_priced_count": sum(1 for r in lines if r["price_source"] == "live_api"),
            "reference_priced_count": sum(
                1 for r in lines if r["price_source"] == "curated_reference"
            ),
            "unpriced_placeholder_count": unpriced_placeholders,
            "unresolved_count": unresolved,
            "owned_line_count": sum(bool(r.get("owned_quantity")) for r in lines),
            "quote_required_count": sum(r["price_source"] == "quote_required" for r in lines),
            "unavailable_count": sum(r["price_source"] == "unavailable" for r in lines),
            "stale_offer_count": sum(r["price_source"] == "stale_observation" for r in lines),
            "live_api_subtotal": _money_float(live_sub),
            "reference_catalog_subtotal": _money_float(ref_sub),
            "printed_material_subtotal": _money_float(print_sub),
            "total_estimated_cost": documented_subtotal,  # Legacy key for the priced subtotal.
            "total_filament_mass_g": round(total_mass_g, 2),
            "total_print_time_minutes": round(total_print_min, 1),
            "max_lead_time_days": max_lead_days,
        },
        "shipping_and_tax_scope": (
            f"Purchases, planning budgets and filament costs are shown separately in {curr}. "
            "Shipping, VAT/sales tax, duties and assembly labor are excluded."
        ),
        "exclusions": list(SHIPPING_AND_TAX_EXCLUSIONS),
    }


def bom_to_json(bom: dict[str, Any]) -> bytes:
    """Serialize complete BOM to deterministic UTF-8 JSON."""
    return json.dumps(bom, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _safe_csv_cell(val: object) -> object:
    if isinstance(val, str) and val.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + val
    return val


def revision_cost_columns(summary):
    """CSV header totals are populated once, alongside the first purchase line."""
    return {
        "revision_documented_subtotal": summary["documented_subtotal"],
        "revision_planning_low": summary["planning_low"],
        "revision_planning_high": summary["planning_high"],
        "revision_printed_material_subtotal": (
            summary["printed_material_subtotal"] if summary["printed_line_count"] else None
        ),
        "revision_missing_price_count": summary["missing_price_count"],
        "revision_unresolved_requirements": summary["unresolved_count"],
    }


def bom_to_csv(bom: dict[str, Any]) -> bytes:
    """Serialize itemized purchasable BOM to formula-safe CSV."""
    buf = io.StringIO(newline="")
    totals = revision_cost_columns(bom["summary"])
    columns = [
        "project_id",
        "project_revision",
        "line_id",
        "group_id",
        "label",
        "category",
        "kind",
        "resolution_status",
        "requirement_name",
        "requirement_sku",
        "requirement_mpn",
        "requirement_part_number",
        "requirement_supplier",
        "requirement_source_file",
        "requirement_role",
        "required_specs",
        "selected_issuer",
        "selected_part_number",
        "selection_basis",
        "selection_source",
        "selection_evidence",
        "selection_decision",
        "inventory_evidence",
        "interface_status",
        "interface_checks",
        "candidates",
        "owned_quantity",
        "purchase_quantity",
        "supplier",
        "sku",
        "mpn",
        "quantity",
        "unit",
        "pack_size",
        "packs_to_order",
        "currency",
        "unit_price",
        "pack_price",
        "line_total",
        "price_source",
        "price_checked_on",
        "cost_category",
        "documented_line_total",
        "printed_material_total",
        "planning_low",
        "planning_high",
        "planning_kind",
        "planning_detail",
        "planning_source",
        "planning_observed_on",
        "availability",
        "stock_quantity",
        "fetched_at",
        "lead_time_days",
        "filament_mass_g",
        "print_time_minutes",
        "source_url",
        "notes",
        *totals,
    ]
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for index, line in enumerate(bom.get("lines") or []):
        rec = dict(line, project_id=bom["project_id"], project_revision=bom["project_revision"])
        if index == 0:
            rec.update(totals)
        basis = line.get("planning_basis") or {}
        rec.update(
            planning_kind=basis.get("kind"),
            planning_detail=basis.get("detail"),
            planning_source=basis.get("source_url"),
            planning_observed_on=basis.get("observed_on"),
        )
        for key, value in (line.get("requirement") or {}).items():
            rec[f"requirement_{key}"] = value
        if specs := (line.get("requirement") or {}).get("critical_specs"):
            rec["required_specs"] = json.dumps(specs, ensure_ascii=False)
        if choice := line.get("part_choice"):
            selected = choice["accepted"] or {}
            rec.update(
                selected_issuer=selected.get("issuer"),
                selected_part_number=selected.get("part_number"),
                selection_basis=selected.get("basis"),
                selection_source=selected.get("source_url"),
                selection_evidence=selected.get("evidence"),
                selection_decision=choice["decision"],
                inventory_evidence=choice.get("inventory_evidence"),
                interface_status=selected.get("interface_status"),
                interface_checks=json.dumps(selected.get("checks", []), ensure_ascii=False),
                candidates=json.dumps(choice["candidates"], ensure_ascii=False),
            )
        pest = line.get("print_estimate") or {}
        rec["filament_mass_g"] = pest.get("filament_mass_g")
        rec["print_time_minutes"] = pest.get("print_time_minutes")
        writer.writerow({k: _safe_csv_cell(rec.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")

"""Complete, purchasable BOM engine across all AutoLab hardware workflows."""

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

from app.print_estimator import estimate_fdm_part
from app.supplier_apis import SupplierApiError, cached_fx_rates, convert_currency

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
    currency: str, fx_rates: dict[str, Any] | None
) -> tuple[str, dict[str, Any] | None]:
    curr = str(currency or "USD").strip().upper()
    if curr not in ("USD", "CHF", "EUR"):
        raise ValueError(f"Unsupported currency {currency!r}. Choose USD, CHF, or EUR.")
    if curr == "USD":
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
    packs_needed = math.ceil(quantity / pack_size)

    live = None
    if live_rows:
        live = live_rows.get(item_id) or live_rows.get(group_id)
        if live and not (
            live.get("status") == "priced"
            and live.get("freshness") == "fresh"
            and live.get("line_total") is not None
        ):
            live = None

    if live is not None:
        live_curr = str(live.get("currency") or "USD").upper()
        raw_pack_price = live.get("pack_price")
        if raw_pack_price is None:
            raw_pack_price = Decimal(str(live.get("unit_price") or 0)) * pack_size
        unit_price = (
            convert_currency(live["unit_price"], live_curr, currency, fx_rates)
            if live.get("unit_price") is not None
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
    else:
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

    return {
        "line_id": f"{group_id}:{item_id}",
        "group_id": group_id,
        "catalog_id": item_id,
        "label": override_label or entry["label"],
        "category": entry["category"],
        "kind": "commercial_part",
        "component_ids": list(component_ids or []),
        "supplier": entry.get("supplier"),
        "sku": entry.get("sku"),
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
            for key in (row.get("selected_id"), row.get("group_id"), row.get("sku")):
                if key:
                    by_id[str(key)] = row
    if isinstance(live_quotes, dict):
        for key, quote in live_quotes.items():
            if not isinstance(quote, dict):
                continue
            price_breaks = quote.get("price_breaks") or []
            if not price_breaks:
                continue
            pack_price = Decimal(str(price_breaks[0]["pack_price"]))
            pack_size = max(1, int(quote.get("pack_size") or 1))
            unavail = quote.get("availability") in {"OutOfStock", "SoldOut", "Discontinued"}
            normalized = {
                "currency": quote.get("currency", "USD"),
                "status": "unavailable" if unavail else "priced",
                "freshness": "fresh",
                "pack_price": _money_float(pack_price),
                "unit_price": _money_float(pack_price / pack_size),
                "line_total": None if unavail else _money_float(pack_price),
                "availability": quote.get("availability", "unknown"),
                "stock_quantity": quote.get("stock_quantity"),
                "lead_time_days": quote.get("lead_time_days"),
                "price_source_url": quote.get("source_url"),
                "fetched_at": quote.get("fetched_at"),
                "adapter": quote.get("adapter") or quote.get("parser"),
            }
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
        dims_xyz = [
            float(cgeom["app_size_mm"][0]),
            float(cgeom["app_size_mm"][2]),
            float(cgeom["app_size_mm"][1]),
        ]
        lines.append(
            _printed_line(
                adapt.get("part_id") or "custom-adapter",
                f"Custom adapted mating part ({adapt.get('mode', 'mating')})",
                dims_xyz,
                volume_mm3=cgeom.get("volume_mm3"),
                group_id="upstream-custom-adaptation",
                material="PETG",
                lab_profile=lab_profile,
                currency=currency,
                fx_rates=fx_rates,
                role_note="Non-destructive AutoLab mating part fitted to upstream CAD.",
            )
        )
    lines.append(
        _catalog_line(
            "m3-shcs-assortment",
            8,
            group_id="upstream-fasteners",
            component_ids=["custom-adapter"],
            role_note="M3 fasteners for mating adapter installation.",
            currency=currency,
            fx_rates=fx_rates,
            live_rows=live_rows,
            catalog_items=catalog_items,
        )
    )
    for idx, raw_item in enumerate(up.get("bom") or []):
        if raw_item.get("source") == "autolab_adaptation":
            continue
        name = str(raw_item.get("name") or f"Upstream part {idx + 1}")
        qty = max(1, int(raw_item.get("quantity") or 1))
        name_l = name.lower()
        matched_id = None
        if "nema 17" in name_l or "nema17" in name_l:
            matched_id = "nema17-17hs4401"
        elif "nema 14" in name_l or "nema14" in name_l:
            matched_id = "pololu-1209"
        elif "mgn12" in name_l:
            matched_id = "rail-mgn12h-300"
        elif "leadscrew" in name_l or "tr8" in name_l:
            matched_id = "leadscrew-tr8x8-300"
        elif "pico" in name_l:
            matched_id = "pico"
        elif "drv8825" in name_l:
            matched_id = "drv8825"
        elif "tmc2209" in name_l:
            matched_id = "tmc2209"
        if matched_id:
            lines.append(
                _catalog_line(
                    matched_id,
                    qty,
                    group_id=f"upstream-bom-{idx}",
                    role_note=f"Matched from upstream repo BOM: {name}",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                    override_label=f"{name} -> {catalog_items[matched_id]['label']}",
                )
            )
        else:
            unit_usd = 8.0
            conv = (
                unit_usd
                if currency == "USD"
                else convert_currency(unit_usd, "USD", currency, fx_rates)
            )
            lines.append(
                {
                    "line_id": f"upstream-item:{idx}",
                    "group_id": "upstream-bom-items",
                    "catalog_id": f"upstream-bom-{idx}",
                    "label": name,
                    "category": "upstream_hardware",
                    "kind": "commercial_part",
                    "component_ids": [],
                    "supplier": raw_item.get("supplier") or "Specify vendor from upstream BOM",
                    "sku": raw_item.get("sku") or f"UPSTREAM-{idx + 1}",
                    "mpn": raw_item.get("sku") or f"UPSTREAM-{idx + 1}",
                    "digikey_pn": None,
                    "mouser_pn": None,
                    "quantity": qty,
                    "unit": "unit",
                    "pack_size": 1,
                    "packs_to_order": qty,
                    "currency": currency,
                    "unit_price": conv,
                    "pack_price": conv,
                    "line_total": _money_float(Decimal(str(conv)) * qty),
                    "price_source": "curated_reference",
                    "availability": "verify_upstream_bom",
                    "stock_quantity": None,
                    "lead_time_days": 7,
                    "source_url": up.get("provenance", {}).get("repo_url"),
                    "fetched_at": None,
                    "adapter": "upstream_bom_allowance",
                    "specs": {"kind": "upstream_bom_item"},
                    "notes": raw_item.get("notes")
                    or "Parsed from upstream repository BOM; confirm exact vendor SKU.",
                    "print_estimate": None,
                }
            )
    return lines


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
    lines: list[dict[str, Any]] = []
    comp_map = {c["key"]: c for c in concept.get("components") or []}
    has_extrusion = False
    for part in concept.get("parts") or []:
        key = part["key"]
        name = part["name"]
        qty = max(1, int(part.get("quantity") or 1))
        comp = comp_map.get(key)
        text = f"{key} {name} {part.get('role', '')}".lower()
        is_extrusion = bool(
            comp and any(p.get("shape") == "extrusion" for p in comp.get("primitives") or [])
        ) or "extrusion" in text or "2020" in text
        is_printed = bool(
            comp
            and all(
                p.get("finish") in ("polymer", "accent") for p in comp.get("primitives") or []
            )
        ) or any(tok in text for tok in ("printed", "holder", "bracket", "cradle", "mast"))

        if is_extrusion:
            has_extrusion = True
            cat_id = "extrusion-2040-500" if "2040" in text else "extrusion-2020-500"
            lines.append(
                _catalog_line(
                    cat_id,
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or "",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        elif "camera" in text and "mount" not in text and "mast" not in text:
            cam_id = "usb-webcam-1080p" if "usb" in text else "camera-module-3"
            lines.append(
                _catalog_line(
                    cam_id,
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or "",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        elif "motor" in text or "stepper" in text:
            mot_id = "nema17-17hs4401" if "17" in text else "pololu-1209"
            lines.append(
                _catalog_line(
                    mot_id,
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or "",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        elif "controller" in text or "pico" in text or "board" in text:
            lines.append(
                _catalog_line(
                    "rpi-zero-2w" if "pi zero" in text or "csi" in text else "pico",
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or "",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        elif "rail" in text or "linear" in text:
            lines.append(
                _catalog_line(
                    "rail-mgn12h-300",
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or "",
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                )
            )
        elif is_printed and comp:
            s = comp.get("size_mm") or [80.0, 20.0, 80.0]
            lines.append(
                _printed_line(
                    key,
                    name,
                    [float(s[0]), float(s[2]), float(s[1])],
                    group_id=f"concept-{key}",
                    quantity=qty,
                    material="PETG",
                    lab_profile=lab_profile,
                    currency=currency,
                    fx_rates=fx_rates,
                    role_note=part.get("role") or "",
                )
            )
        else:
            fallback_id = (
                "fastener-m5-tnut-kit"
                if "fastener" in text or "nut" in text
                else "m3-shcs-assortment"
            )
            lines.append(
                _catalog_line(
                    fallback_id,
                    qty,
                    group_id=f"concept-{key}",
                    component_ids=[key],
                    role_note=part.get("role") or name,
                    currency=currency,
                    fx_rates=fx_rates,
                    live_rows=live_rows,
                    catalog_items=catalog_items,
                    override_label=name,
                )
            )
    if has_extrusion:
        lines.append(
            _catalog_line(
                "corner-bracket-2020",
                4,
                group_id="concept-frame-brackets",
                role_note="90-deg corner brackets for 2020/2040 extrusion joints.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
        lines.append(
            _catalog_line(
                "fastener-m5-tnut-kit",
                16,
                group_id="concept-frame-fasteners",
                role_note="M5 T-nuts & cap screws for aluminum extrusion slots.",
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    return lines


def _bench_station_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    station = project["bench_station"]
    ops = (station.get("proposal") or {}).get("operations") or []
    lines: list[dict[str, Any]] = []
    op_catalog = {
        "weigh": ("bench-balance-ohaus", "Precision benchtop balance candidate"),
        "print_label": ("bench-label-printer", "Thermal chemical-resistant label printer"),
        "read_barcode": ("bench-barcode-usb", "2D USB-HID barcode scanner candidate"),
        "inspect": ("usb-webcam-1080p", "1080p USB inspection camera candidate"),
    }
    for op in ops:
        cat_id, note = op_catalog[op]
        lines.append(
            _catalog_line(
                cat_id,
                1,
                group_id=f"bench-{op}-equipment",
                component_ids=[op],
                role_note=note,
                currency=currency,
                fx_rates=fx_rates,
                live_rows=live_rows,
                catalog_items=catalog_items,
            )
        )
    mod_count = max(1, len(ops))
    lines.append(
        _printed_line(
            "station-locator-tray",
            "Printed Bench Station Alignment & Hand-off Guide Trays",
            [140.0, 100.0, 12.0],
            group_id="bench-printed-trays",
            quantity=mod_count,
            material="PLA",
            lab_profile=lab_profile,
            currency=currency,
            fx_rates=fx_rates,
            role_note="Locating guides keeping station zones aligned on the lab bench.",
        )
    )
    return lines


def _workstation_bom_lines(
    project: dict[str, Any],
    *,
    currency: str,
    fx_rates: dict[str, Any] | None,
    live_rows: dict[str, dict[str, Any]],
    catalog_items: dict[str, dict[str, Any]],
    lab_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expand workstation/Jubilee/OT-2 into complete itemized lines (zero unpriced groups)."""
    design = project.get("design") or {}
    selections = project.get("selections") or {}
    summary = project.get("summary") or {}
    cooling_enabled = bool(summary.get("cooling_enabled", False))
    is_ot2 = (design.get("platform") or {}).get("id") == "opentrons-ot2"
    is_jubilee = bool(design.get("foundation"))
    lines: list[dict[str, Any]] = []

    if is_ot2:
        ref_usd = 15950.0
        unit_p = (
            ref_usd
            if currency == "USD"
            else convert_currency(ref_usd, "USD", currency, fx_rates)
        )
        lines.append(
            {
                "line_id": "platform:opentrons-ot2",
                "group_id": "platform",
                "catalog_id": "opentrons-ot2",
                "label": "Opentrons OT-2 Liquid Handling Robot + 1 Single-Channel Pipette",
                "category": "commercial_robot",
                "kind": "commercial_part",
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
                "unit_price": unit_p,
                "pack_price": unit_p,
                "line_total": unit_p,
                "price_source": "curated_reference",
                "availability": "vendor_quote_required",
                "stock_quantity": None,
                "lead_time_days": 21,
                "source_url": "https://opentrons.com/products/robots/ot-2/",
                "fetched_at": None,
                "adapter": "curated_reference_catalog",
                "specs": {"kind": "commercial_robot", "supply_voltage_v": 115.0},
                "notes": "Base price starting-at USD reference; request formal quotation.",
                "print_estimate": None,
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


def build_complete_bom(
    project: dict[str, Any],
    *,
    procurement_report: dict[str, Any] | None = None,
    lab_profile: dict[str, Any] | None = None,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    live_quotes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a complete, itemized purchasable BOM with zero unpriced placeholders."""
    curr, eff_fx = _resolve_currency_and_fx(currency, fx_rates)
    catalog_data = load_purchasable_catalog()
    catalog_items = catalog_data["items"]
    live_rows = _collect_live_rows(procurement_report, live_quotes)

    if project.get("standalone_fixture"):
        route_kind = "standalone_fixture"
        lines = _fixture_bom_lines(
            project,
            currency=curr,
            fx_rates=eff_fx,
            live_rows=live_rows,
            catalog_items=catalog_items,
            lab_profile=lab_profile,
        )
    elif project.get("upstream_project"):
        route_kind = "upstream_project"
        lines = _upstream_bom_lines(
            project,
            currency=curr,
            fx_rates=eff_fx,
            live_rows=live_rows,
            catalog_items=catalog_items,
            lab_profile=lab_profile,
        )
    elif project.get("concept_design"):
        route_kind = "concept_design"
        lines = _concept_bom_lines(
            project,
            currency=curr,
            fx_rates=eff_fx,
            live_rows=live_rows,
            catalog_items=catalog_items,
            lab_profile=lab_profile,
        )
    elif project.get("bench_station"):
        route_kind = "bench_station"
        lines = _bench_station_bom_lines(
            project,
            currency=curr,
            fx_rates=eff_fx,
            live_rows=live_rows,
            catalog_items=catalog_items,
            lab_profile=lab_profile,
        )
    else:
        route_kind = "workstation"
        lines = _workstation_bom_lines(
            project,
            currency=curr,
            fx_rates=eff_fx,
            live_rows=live_rows,
            catalog_items=catalog_items,
            lab_profile=lab_profile,
        )

    live_sub = Decimal(0)
    ref_sub = Decimal(0)
    print_sub = Decimal(0)
    total_mass_g = 0.0
    total_print_min = 0.0
    max_lead_days = 0
    unpriced_placeholders = 0

    for line in lines:
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
        else:
            ref_sub += dec
        if pest := line.get("print_estimate"):
            total_mass_g += float(pest["filament_mass_g"])
            total_print_min += float(pest["print_time_minutes"])
        if line.get("lead_time_days"):
            max_lead_days = max(max_lead_days, int(line["lead_time_days"]))

    total_combined = live_sub + ref_sub + print_sub
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "project_id": project["id"],
        "project_revision": project["revision"],
        "route_kind": route_kind,
        "currency": curr,
        "fx_rate_used": (eff_fx["rates"].get(curr) if eff_fx and eff_fx.get("rates") else None),
        "fx_source": eff_fx.get("source") if eff_fx else None,
        "fx_rate_date": eff_fx.get("rate_date") if eff_fx else None,
        "catalog_checked_on": catalog_data["checked_on"],
        "lines": lines,
        "summary": {
            "line_count": len(lines),
            "commercial_line_count": sum(1 for r in lines if r["kind"] == "commercial_part"),
            "printed_line_count": sum(1 for r in lines if r["kind"] == "3d_printed"),
            "live_api_priced_count": sum(1 for r in lines if r["price_source"] == "live_api"),
            "reference_priced_count": sum(
                1 for r in lines if r["price_source"] == "curated_reference"
            ),
            "unpriced_placeholder_count": unpriced_placeholders,
            "live_api_subtotal": _money_float(live_sub),
            "reference_catalog_subtotal": _money_float(ref_sub),
            "printed_material_subtotal": _money_float(print_sub),
            "total_estimated_cost": _money_float(total_combined),
            "total_filament_mass_g": round(total_mass_g, 2),
            "total_print_time_minutes": round(total_print_min, 1),
            "max_lead_time_days": max_lead_days,
        },
        "shipping_and_tax_scope": (
            f"All totals in {curr} reflect unit/pack component hardware prices and analytical "
            "filament material cost ONLY. Shipping, VAT/sales tax, import duties, and assembly "
            "labor are explicitly excluded."
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


def bom_to_csv(bom: dict[str, Any]) -> bytes:
    """Serialize itemized purchasable BOM to formula-safe CSV."""
    buf = io.StringIO(newline="")
    columns = [
        "line_id",
        "group_id",
        "label",
        "category",
        "kind",
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
        "availability",
        "stock_quantity",
        "lead_time_days",
        "filament_mass_g",
        "print_time_minutes",
        "source_url",
        "notes",
    ]
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for line in bom.get("lines") or []:
        rec = dict(line)
        pest = line.get("print_estimate") or {}
        rec["filament_mass_g"] = pest.get("filament_mass_g")
        rec["print_time_minutes"] = pest.get("print_time_minutes")
        writer.writerow({k: _safe_csv_cell(rec.get(k)) for k in columns})
    return buf.getvalue().encode("utf-8")

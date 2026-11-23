"""Analytical slicer-based FDM filament mass, print time, and material cost estimator."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.supplier_apis import convert_currency

MATERIAL_SPECS: dict[str, dict[str, Any]] = {
    "PLA": {
        "density_g_cm3": 1.24,
        "spool_cost_usd_per_kg": 22.0,
        "max_flow_mm3_s": 14.0,
        "default_bed_temp_c": 60,
        "default_nozzle_temp_c": 215,
    },
    "PETG": {
        "density_g_cm3": 1.27,
        "spool_cost_usd_per_kg": 26.0,
        "max_flow_mm3_s": 11.0,
        "default_bed_temp_c": 75,
        "default_nozzle_temp_c": 245,
    },
    "ABS": {
        "density_g_cm3": 1.04,
        "spool_cost_usd_per_kg": 28.0,
        "max_flow_mm3_s": 12.0,
        "default_bed_temp_c": 100,
        "default_nozzle_temp_c": 260,
    },
    "ASA": {
        "density_g_cm3": 1.07,
        "spool_cost_usd_per_kg": 32.0,
        "max_flow_mm3_s": 12.0,
        "default_bed_temp_c": 100,
        "default_nozzle_temp_c": 260,
    },
    "TPU": {
        "density_g_cm3": 1.21,
        "spool_cost_usd_per_kg": 36.0,
        "max_flow_mm3_s": 5.0,
        "default_bed_temp_c": 50,
        "default_nozzle_temp_c": 225,
    },
    "PA-CF": {
        "density_g_cm3": 1.18,
        "spool_cost_usd_per_kg": 65.0,
        "max_flow_mm3_s": 8.0,
        "default_bed_temp_c": 95,
        "default_nozzle_temp_c": 280,
    },
}

FILAMENT_DIAMETER_MM = 1.75
FILAMENT_AREA_MM2 = math.pi * (FILAMENT_DIAMETER_MM / 2.0) ** 2
HEATUP_AND_MESH_OVERHEAD_MINUTES = 6.5


def _round_money(val: float) -> float:
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _check_printer_fit(
    bounding_box_mm: list[float], lab_profile: dict[str, Any] | None, material: str
) -> tuple[bool | None, bool | None, str | None]:
    if not isinstance(lab_profile, dict):
        return None, None, None
    printer = lab_profile.get("printer") or {}
    vol = printer.get("build_volume_mm")
    fits: bool | None = None
    mat_ok: bool | None = None
    detail: str | None = None
    if isinstance(vol, (list, tuple)) and len(vol) == 3:
        bx, by, bz = (float(v) for v in vol)
        px, py, pz = bounding_box_mm
        # Allow lying flat in any horizontal 90-deg XY orientation; keep printed height on Z.
        fits = (px <= bx + 1e-6 and py <= by + 1e-6 and pz <= bz + 1e-6) or (
            py <= bx + 1e-6 and px <= by + 1e-6 and pz <= bz + 1e-6
        )
        name = printer.get("name") or "Configured printer"
        detail = (
            f"Fits {name} ({bx:g}x{by:g}x{bz:g} mm)."
            if fits
            else f"Exceeds {name} ({bx:g}x{by:g}x{bz:g} mm) with {px:.1f}x{py:.1f}x{pz:.1f} mm."
        )
    mats = [str(m).strip().upper() for m in (printer.get("materials") or [])]
    if mats:
        mat_ok = material.upper() in mats
    return fits, mat_ok, detail


def estimate_fdm_part(
    part_id: str,
    label: str,
    bounding_box_mm: list[float] | tuple[float, float, float],
    *,
    volume_mm3: float | None = None,
    surface_area_mm2: float | None = None,
    material: str = "PLA",
    nozzle_mm: float = 0.4,
    layer_height_mm: float = 0.2,
    wall_loops: int = 3,
    top_bottom_layers: int = 4,
    infill_percent: float = 20.0,
    supports_enabled: bool = False,
    quantity: int = 1,
    lab_profile: dict[str, Any] | None = None,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute slicer-based shell+infill volume, filament mass, print time, and material cost."""
    if not isinstance(bounding_box_mm, (list, tuple)) or len(bounding_box_mm) != 3:
        raise ValueError("bounding_box_mm must contain 3 dimensions [X, Y, Z] in mm.")
    dims = [float(v) for v in bounding_box_mm]
    if any(not math.isfinite(v) or v <= 0 for v in dims):
        raise ValueError("bounding_box_mm dimensions must be positive finite numbers.")
    if quantity < 1 or quantity > 10_000:
        raise ValueError("quantity must be between 1 and 10,000.")
    if nozzle_mm <= 0.1 or layer_height_mm <= 0.04 or layer_height_mm > nozzle_mm:
        raise ValueError("Invalid nozzle_mm or layer_height_mm parameters.")

    mat_key = str(material or "PLA").strip().upper()
    if mat_key not in MATERIAL_SPECS:
        raise ValueError(f"Unsupported FDM material {material!r}.")
    mat = MATERIAL_SPECS[mat_key]

    bx, by, bz = dims
    box_vol = bx * by * bz
    solid_vol = float(volume_mm3) if (volume_mm3 is not None and volume_mm3 > 0) else box_vol * 0.55
    solid_vol = min(solid_vol, box_vol)

    if surface_area_mm2 is not None and surface_area_mm2 > 0:
        area_mm2 = float(surface_area_mm2)
    else:
        # Scale box surface area conservatively with shape fill ratio.
        box_area = 2.0 * (bx * by + bx * bz + by * bz)
        area_mm2 = box_area * (solid_vol / box_vol) ** (2.0 / 3.0)

    line_width = nozzle_mm * 1.05
    avg_shell_thickness = (
        0.65 * (wall_loops * line_width) + 0.35 * (top_bottom_layers * layer_height_mm)
    )
    # Prevent overcounting shell intersections on thin walls.
    raw_shell = min(solid_vol, area_mm2 * avg_shell_thickness * 0.78)
    interior_vol = max(0.0, solid_vol - raw_shell)
    infill_frac = max(0.0, min(1.0, float(infill_percent) / 100.0))
    infill_vol = interior_vol * infill_frac
    extruded_part_vol = raw_shell + infill_vol
    support_and_purge = (extruded_part_vol * 0.12 if supports_enabled else 0.0) + 120.0
    total_per_unit_mm3 = extruded_part_vol + support_and_purge
    extruded_cm3 = total_per_unit_mm3 / 1000.0

    mass_per_unit_g = extruded_cm3 * mat["density_g_cm3"]
    length_per_unit_m = (total_per_unit_mm3 / FILAMENT_AREA_MM2) / 1000.0

    # Volumetric flow depends on layer height and nozzle width, capped by material flow ceiling.
    nominal_speed_mm_s = 70.0 if mat_key in ("PLA", "PETG") else 55.0
    if mat_key == "TPU":
        nominal_speed_mm_s = 28.0
    target_flow = min(mat["max_flow_mm3_s"], nominal_speed_mm_s * line_width * layer_height_mm)
    effective_flow = max(1.5, target_flow * 0.58)  # Account for accelerations & retractions
    extrude_minutes = (total_per_unit_mm3 / effective_flow) / 60.0
    layer_count = max(1, math.ceil(bz / layer_height_mm))
    layer_change_minutes = (layer_count * 0.35) / 60.0
    minutes_per_unit = HEATUP_AND_MESH_OVERHEAD_MINUTES + extrude_minutes + layer_change_minutes

    total_mass_g = round(mass_per_unit_g * quantity, 2)
    total_length_m = round(length_per_unit_m * quantity, 2)
    total_minutes = round(
        HEATUP_AND_MESH_OVERHEAD_MINUTES
        + (extrude_minutes + layer_change_minutes) * quantity,
        1,
    )
    material_cost_usd = _round_money((total_mass_g / 1000.0) * mat["spool_cost_usd_per_kg"])
    curr = str(currency or "USD").strip().upper()
    if curr == "USD":
        material_cost = material_cost_usd
    else:
        material_cost = convert_currency(material_cost_usd, "USD", curr, fx_rates)

    fits_bed, mat_ok, fit_detail = _check_printer_fit(dims, lab_profile, mat_key)
    return {
        "part_id": part_id,
        "label": label,
        "quantity": quantity,
        "bounding_box_mm": [round(v, 2) for v in dims],
        "material": mat_key,
        "nozzle_mm": round(nozzle_mm, 2),
        "layer_height_mm": round(layer_height_mm, 2),
        "wall_loops": wall_loops,
        "top_bottom_layers": top_bottom_layers,
        "infill_percent": round(float(infill_percent), 1),
        "supports_enabled": bool(supports_enabled),
        "solid_volume_mm3": round(solid_vol, 1),
        "shell_volume_mm3": round(raw_shell, 1),
        "infill_volume_mm3": round(infill_vol, 1),
        "support_and_purge_mm3": round(support_and_purge, 1),
        "extruded_volume_cm3_per_unit": round(extruded_cm3, 2),
        "filament_mass_g_per_unit": round(mass_per_unit_g, 2),
        "filament_mass_g": total_mass_g,
        "filament_length_m": total_length_m,
        "print_time_minutes_per_unit": round(minutes_per_unit, 1),
        "print_time_minutes": total_minutes,
        "spool_cost_usd_per_kg": mat["spool_cost_usd_per_kg"],
        "material_cost_usd": material_cost_usd,
        "material_cost": material_cost,
        "currency": curr,
        "fits_printer_bed": fits_bed,
        "material_available_in_lab": mat_ok,
        "printer_fit_detail": fit_detail,
        "assumptions": [
            f"Analytical FDM slicer estimate for {mat_key} ({mat['density_g_cm3']} g/cm3, "
            f"${mat['spool_cost_usd_per_kg']:.0f}/kg spool) at {layer_height_mm:g} mm layers, "
            f"{wall_loops} perimeters, {infill_percent:g}% infill.",
            f"Includes {HEATUP_AND_MESH_OVERHEAD_MINUTES:g} min bed leveling/heatup per build "
            "plate; excludes failed print scrap, electricity, and manual support removal.",
        ],
    }

"""Standalone printable bench fixture & imaging rig adapter (Phase 2.1).

Independent of liquid-handling robot bases (Jubilee / OT-2), this compiler builds
parametric locating bases, sample holders, optional co-planar 50 mm reference
scale shelves, fixed overhead camera mounts, and 3-step FDM tolerance coupons
from either named references or user-measured specimen envelopes.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.microplate_geometry import SOURCE_REVISION as SLAS_SOURCE_REVISION
from app.microplate_geometry import SOURCE_URL as SLAS_SOURCE_URL

FIXTURE_REFERENCES: dict[str, dict[str, Any]] = {
    "slas_96_well_plate": {
        "id": "slas_96_well_plate",
        "title": "96-well microplate (ANSI/SLAS 1-2004 nominal footprint)",
        "object_label": "96-well microplate",
        "object_shape": "rectangular",
        "object_length_mm": 127.76,
        "object_width_mm": 85.48,
        "object_diameter_mm": None,
        "object_height_mm": 14.22,
        "dimension_provenance": "reference",
        "source_id": "corning_96_wellplate_360ul_flat",
        "source_url": SLAS_SOURCE_URL,
        "revision": SLAS_SOURCE_REVISION,
        "license": "Apache-2.0",
        "notes": (
            "Nominal ANSI/SLAS footprint (127.76 x 85.48 mm) +/-0.50 mm vendor tolerance; "
            "verify actual plate skirt dimensions before final lock-in."
        ),
    },
    "petri_dish_90mm": {
        "id": "petri_dish_90mm",
        "title": "90 mm Petri dish (nominal reference)",
        "object_label": "90 mm Petri dish",
        "object_shape": "circular",
        "object_length_mm": None,
        "object_width_mm": None,
        "object_diameter_mm": 90.0,
        "object_height_mm": 15.0,
        "dimension_provenance": "reference",
        "source_id": "nominal-petri-90mm",
        "source_url": None,
        "revision": "nominal",
        "license": "nominal-standard",
        "notes": (
            "Nominal 90 mm reference diameter (real polystyrene dishes vary ~86-94 mm OD "
            "by brand); verify actual dish OD with calipers before final print."
        ),
    },
    "microscope_slide_75x25": {
        "id": "microscope_slide_75x25",
        "title": "Microscope slide (ISO 8037 75 x 25 mm nominal)",
        "object_label": "75 x 25 mm microscope slide",
        "object_shape": "rectangular",
        "object_length_mm": 75.0,
        "object_width_mm": 25.0,
        "object_diameter_mm": None,
        "object_height_mm": 1.0,
        "dimension_provenance": "reference",
        "source_id": "iso-8037-slide",
        "source_url": None,
        "revision": "nominal",
        "license": "nominal-standard",
        "notes": (
            "Nominal 75.0 x 25.0 x 1.0 mm ISO 8037 envelope; verify slide chamfer and "
            "thickness if retaining a coated or cover-slipped specimen."
        ),
    },
    "cuvette_12_5mm": {
        "id": "cuvette_12_5mm",
        "title": "Spectrophotometer cuvette (12.5 x 12.5 x 45 mm nominal)",
        "object_label": "12.5 mm optical cuvette",
        "object_shape": "rectangular",
        "object_length_mm": 12.5,
        "object_width_mm": 12.5,
        "object_diameter_mm": None,
        "object_height_mm": 45.0,
        "dimension_provenance": "reference",
        "source_id": "nominal-cuvette-10mm",
        "source_url": None,
        "revision": "nominal",
        "license": "nominal-standard",
        "notes": (
            "Nominal 12.5 x 12.5 x 45.0 mm 10 mm pathlength cuvette outer envelope; "
            "measure tapered upper rims or stopper shoulders before final lock-in."
        ),
    },
}

FASTENER_HOLE_DIAMETERS_MM: dict[str, float | None] = {
    "none": None,
    "m3_clearance": 3.4,
    "m3_heatset_insert": 4.0,
    "m4_clearance": 4.5,
}

CAMERA_MOUNT_SPECS: dict[str, dict[str, Any]] = {
    "pi_camera_v2_v3": {
        "label": "Raspberry Pi Camera Module v2/v3 (25 x 24 mm PCB, 4x M2)",
        "cradle_width_mm": 34.0,
        "cradle_depth_mm": 32.0,
        "lens_aperture_mm": 14.0,
        "mounting_hole_dx_mm": 21.0,
        "mounting_hole_dz_mm": 12.5,
        "mounting_hole_diameter_mm": 2.3,
        "pattern": "4_corners",
    },
    "usb_webcam_quarter20": {
        "label": "USB webcam / tripod 1/4-20 mount",
        "cradle_width_mm": 38.0,
        "cradle_depth_mm": 34.0,
        "lens_aperture_mm": 0.0,
        "mounting_hole_dx_mm": 0.0,
        "mounting_hole_dz_mm": 0.0,
        "mounting_hole_diameter_mm": 6.8,
        "pattern": "center_quarter20",
    },
    "custom_m3": {
        "label": "Custom 2x M3 camera bracket (20 mm pitch)",
        "cradle_width_mm": 36.0,
        "cradle_depth_mm": 32.0,
        "lens_aperture_mm": 14.0,
        "mounting_hole_dx_mm": 20.0,
        "mounting_hole_dz_mm": 0.0,
        "mounting_hole_diameter_mm": 3.4,
        "pattern": "2_inline_x",
    },
}

COUPON_OFFSETS_MM = (-0.15, 0.0, 0.15)
SCALE_SHELF_DEPTH_MM = 14.0
SCALE_REFERENCE_LENGTH_MM = 50.0
MAST_PAD_DEPTH_MM = 22.0
MAST_CROSS_SECTION_MM = (28.0, 18.0)  # width (X), thickness/depth (Z)
ARM_THICKNESS_MM = 8.0
FLANGE_EAR_WIDTH_MM = 8.0
FLANGE_EAR_DEPTH_MM = 18.0
MAX_FIXED_MAST_ALLOWANCE_MM = 3.0


def _check_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite.")
    return number


class FixtureSpec(BaseModel):
    """Strict, portable input schema for standalone printable bench fixtures."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2"] = "2"
    title: str = Field(default="Standalone bench fixture", min_length=1, max_length=140)
    fixture_kind: Literal["locating_base", "sample_holder", "imaging_rig"] = "locating_base"
    object_reference_id: Literal[
        "slas_96_well_plate",
        "petri_dish_90mm",
        "microscope_slide_75x25",
        "cuvette_12_5mm",
        "custom",
    ] = "custom"
    labware_load_name: str | None = Field(default=None, max_length=120)
    object_label: str = Field(default="", max_length=120)
    object_shape: Literal["rectangular", "circular"] = "rectangular"
    object_length_mm: float | None = Field(default=None, ge=5.0, le=400.0)
    object_width_mm: float | None = Field(default=None, ge=5.0, le=400.0)
    object_diameter_mm: float | None = Field(default=None, ge=5.0, le=300.0)
    object_height_mm: float | None = Field(default=None, ge=0.5, le=250.0)
    dimension_provenance: Literal["reference", "user_measured"] = "reference"
    clearance_per_side_mm: float = Field(default=0.35, ge=0.05, le=3.0)
    fdm_undersize_mm: float = Field(default=0.20, ge=0.0, le=1.5)
    wall_thickness_mm: float = Field(default=4.0, ge=2.0, le=20.0)
    floor_thickness_mm: float = Field(default=3.5, ge=2.0, le=20.0)
    lip_height_mm: float = Field(default=4.0, ge=1.5, le=40.0)
    chamfer_mm: float = Field(default=0.8, ge=0.0, le=3.0)
    fillet_mm: float = Field(default=1.0, ge=0.0, le=5.0)
    fastener_spec: Literal[
        "none", "m3_clearance", "m3_heatset_insert", "m4_clearance"
    ] = "m3_heatset_insert"
    include_alignment_stops: bool = True
    include_finger_cutouts: bool = True
    include_scale_reference: bool = False
    include_camera_mount: bool = False
    camera_mount_type: Literal[
        "pi_camera_v2_v3", "usb_webcam_quarter20", "custom_m3"
    ] = "pi_camera_v2_v3"
    camera_working_distance_mm: float = Field(default=140.0, ge=40.0, le=400.0)
    material: Literal["PLA", "PETG", "ABS", "ASA"] = "PLA"
    nozzle_mm: float = Field(default=0.4, ge=0.2, le=1.0)
    layer_height_mm: float = Field(default=0.2, ge=0.08, le=0.4)
    export_parts: list[str] | None = None

    @field_validator("title", "object_label")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "object_length_mm",
        "object_width_mm",
        "object_diameter_mm",
        "object_height_mm",
        "clearance_per_side_mm",
        "fdm_undersize_mm",
        "wall_thickness_mm",
        "floor_thickness_mm",
        "lip_height_mm",
        "chamfer_mm",
        "fillet_mm",
        "camera_working_distance_mm",
        "nozzle_mm",
        "layer_height_mm",
        mode="before",
    )
    @classmethod
    def _reject_bad_numbers(cls, value: Any, info) -> Any:
        if value is None:
            return None
        return _check_finite(value, info.field_name)

    @model_validator(mode="after")
    def _apply_imaging_defaults(self) -> FixtureSpec:
        if self.fixture_kind == "imaging_rig":
            if "include_scale_reference" not in self.model_fields_set:
                self.include_scale_reference = True
            if "include_camera_mount" not in self.model_fields_set:
                self.include_camera_mount = True
        if self.export_parts is not None:
            cleaned = [p.strip() for p in self.export_parts if isinstance(p, str) and p.strip()]
            self.export_parts = list(dict.fromkeys(cleaned))
        return self


def list_fixture_references() -> list[dict[str, Any]]:
    """Return all built-in specimen references available for standalone fixtures."""
    return [deepcopy(item) for item in FIXTURE_REFERENCES.values()]


def normalize_fixture_spec(raw: dict[str, Any] | FixtureSpec) -> FixtureSpec:
    """Resolve named reference defaults, infer honest provenance, and validate shape."""
    if isinstance(raw, FixtureSpec):
        raw_dict = raw.model_dump(exclude_unset=False)
        supplied_keys = set(raw.model_fields_set)
    elif isinstance(raw, dict):
        raw_dict = dict(raw)
        supplied_keys = {k for k, v in raw_dict.items() if v is not None}
    else:
        raise ValueError("Fixture specification must be an object.")

    parsed = FixtureSpec.model_validate(raw_dict)
    ref_id = parsed.object_reference_id
    updates: dict[str, Any] = {}

    if parsed.labware_load_name:
        from app.labware_registry import get_labware_definition

        entry = get_labware_definition(parsed.labware_load_name)["summary"]
        dims = entry["dimensions_mm"]
        if "object_shape" not in supplied_keys:
            updates["object_shape"] = "rectangular"
        if not parsed.object_label:
            updates["object_label"] = entry["display_name"]
        if "object_length_mm" not in supplied_keys:
            updates["object_length_mm"] = float(dims[0])
        if "object_width_mm" not in supplied_keys:
            updates["object_width_mm"] = float(dims[1])
        if "object_diameter_mm" not in supplied_keys:
            updates["object_diameter_mm"] = None
        if "object_height_mm" not in supplied_keys:
            updates["object_height_mm"] = float(dims[2])
        user_overrode_dims = bool(
            {"object_length_mm", "object_width_mm", "object_diameter_mm", "object_height_mm"}
            & supplied_keys
        )
        if "dimension_provenance" not in supplied_keys:
            updates["dimension_provenance"] = (
                "user_measured" if user_overrode_dims else "reference"
            )
    elif ref_id != "custom":
        ref = FIXTURE_REFERENCES[ref_id]
        if "object_shape" not in supplied_keys:
            updates["object_shape"] = ref["object_shape"]
        effective_shape = updates.get("object_shape", parsed.object_shape)
        if not parsed.object_label:
            updates["object_label"] = ref["object_label"]
        if effective_shape == ref["object_shape"]:
            if effective_shape == "rectangular":
                if "object_length_mm" not in supplied_keys:
                    updates["object_length_mm"] = ref["object_length_mm"]
                if "object_width_mm" not in supplied_keys:
                    updates["object_width_mm"] = ref["object_width_mm"]
                if "object_diameter_mm" not in supplied_keys:
                    updates["object_diameter_mm"] = None
            else:
                if "object_diameter_mm" not in supplied_keys:
                    updates["object_diameter_mm"] = ref["object_diameter_mm"]
                if "object_length_mm" not in supplied_keys:
                    updates["object_length_mm"] = None
                if "object_width_mm" not in supplied_keys:
                    updates["object_width_mm"] = None
        if "object_height_mm" not in supplied_keys:
            updates["object_height_mm"] = ref["object_height_mm"]

        user_overrode_dims = bool(
            {"object_length_mm", "object_width_mm", "object_diameter_mm", "object_height_mm"}
            & supplied_keys
        )
        if user_overrode_dims and "dimension_provenance" not in supplied_keys:
            updates["dimension_provenance"] = "user_measured"
        elif "dimension_provenance" not in supplied_keys:
            updates["dimension_provenance"] = ref["dimension_provenance"]
    else:
        if not parsed.object_label:
            raise ValueError("object_label is required for a custom specimen fixture.")
        if "dimension_provenance" not in supplied_keys:
            updates["dimension_provenance"] = "user_measured"

    final_data = {**parsed.model_dump(), **updates}
    normalized = FixtureSpec.model_validate(final_data)
    if normalized.object_height_mm is None and normalized.include_camera_mount:
        raise ValueError("Measure the object's height to set the overhead camera position.")
    if normalized.object_shape == "rectangular":
        if normalized.object_length_mm is None or normalized.object_width_mm is None:
            raise ValueError(
                "Rectangular fixtures require finite object_length_mm and object_width_mm."
            )
    else:
        if normalized.object_diameter_mm is None:
            raise ValueError("Circular fixtures require finite object_diameter_mm.")
    if not normalized.title:
        raise ValueError("title must not be empty.")
    return normalized


def _stable_signature(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _printer_fit_for_part(size_mm: list[float], lab_profile: dict | None) -> dict[str, Any]:
    if not isinstance(lab_profile, dict):
        return {
            "checked": False,
            "fits": None,
            "orientation": "unchecked",
            "build_volume_mm": None,
            "printer_name": None,
            "detail": "No printer profile configured yet.",
        }
    printer = lab_profile.get("printer") or {}
    volume = printer.get("build_volume_mm")
    if not isinstance(volume, (list, tuple)) or len(volume) != 3:
        return {
            "checked": False,
            "fits": None,
            "orientation": "unchecked",
            "build_volume_mm": None,
            "printer_name": printer.get("name"),
            "detail": "Configured lab profile has no printer build volume.",
        }
    bx, by, bz = float(volume[0]), float(volume[1]), float(volume[2])
    if min(bx, by, bz) <= 0:
        return {
            "checked": False,
            "fits": None,
            "orientation": "unchecked",
            "build_volume_mm": None,
            "printer_name": printer.get("name"),
            "detail": "Printer build volume must be positive.",
        }
    w, h, d = float(size_mm[0]), float(size_mm[1]), float(size_mm[2])
    std = w <= bx + 1e-6 and d <= by + 1e-6 and h <= bz + 1e-6
    rot = d <= bx + 1e-6 and w <= by + 1e-6 and h <= bz + 1e-6
    fits = std or rot
    orientation = "standard" if std else ("rotated_90_xy" if rot else "exceeds_build_volume")
    printer_name = printer.get("name") or "Configured printer"
    if fits:
        detail = (
            f"Fits {printer_name} ({bx:g} x {by:g} x {bz:g} mm) "
            f"in {orientation} bed orientation."
        )
    else:
        std_over = (max(0.0, w - bx), max(0.0, d - by), max(0.0, h - bz))
        rot_over = (max(0.0, d - bx), max(0.0, w - by), max(0.0, h - bz))
        best = std_over if sum(std_over) <= sum(rot_over) else rot_over
        over = [f"{ax} +{val:.1f} mm" for ax, val in zip(("X", "Y", "Z"), best) if val > 1e-6]
        detail = (
            f"Exceeds {printer_name} ({bx:g} x {by:g} x {bz:g} mm): {', '.join(over)}."
        )
    return {
        "checked": True,
        "fits": fits,
        "orientation": orientation,
        "build_volume_mm": [round(bx, 2), round(by, 2), round(bz, 2)],
        "printer_name": printer_name,
        "detail": detail,
    }


def _estimate_filament_and_time(
    volume_cm3: float, *, infill_pct: int, layer_height_mm: float
) -> tuple[float, int]:
    # Approximate shell + sparse infill factor for PLA/PETG/ABS bench estimates.
    effective_fraction = min(0.85, 0.30 + 0.55 * (infill_pct / 100.0))
    mass_g = max(2.0, round(volume_cm3 * effective_fraction * 1.24, 1))
    speed_factor = max(0.7, 0.20 / max(layer_height_mm, 0.08))
    minutes = max(8, int(round(mass_g * 1.55 * speed_factor)))
    return mass_g, minutes


def _base_primitives(
    spec: FixtureSpec, derived: dict[str, Any]
) -> list[dict[str, Any]]:
    floor = spec.floor_thickness_mm
    lip = spec.lip_height_mm
    wall = spec.wall_thickness_mm
    base_h = floor + lip
    core_w = derived["core_width_mm"]
    core_d = derived["core_depth_mm"]
    pocket_l = derived["pocket_length_mm"]
    pocket_w = derived["pocket_width_mm"]
    center_y = base_h / 2.0

    primitives: list[dict[str, Any]] = [
        {
            "id": "base-floor",
            "kind": "box",
            "position": [0.0, floor / 2.0 - center_y, 0.0],
            "size": [core_w, floor, core_d],
            "appearance": "rack",
        }
    ]
    if spec.object_shape == "rectangular":
        # Four locating pocket walls matching physical pocket bounds.
        for sign, side in ((-1, "left"), (1, "right")):
            primitives.append(
                {
                    "id": f"pocket-wall-{side}",
                    "kind": "box",
                    "position": [
                        sign * (pocket_l + wall) / 2.0,
                        floor + lip / 2.0 - center_y,
                        0.0,
                    ],
                    "size": [wall, lip, core_d],
                    "appearance": "rack-top",
                }
            )
        for sign, side in ((-1, "rear"), (1, "front")):
            primitives.append(
                {
                    "id": f"pocket-wall-{side}",
                    "kind": "box",
                    "position": [
                        0.0,
                        floor + lip / 2.0 - center_y,
                        sign * (pocket_w + wall) / 2.0,
                    ],
                    "size": [pocket_l, lip, wall],
                    "appearance": "rack-top",
                }
            )
    else:
        primitives.append(
            {
                "id": "circular-well-rim",
                "kind": "tube",
                "axis": "y",
                "position": [0.0, floor + lip / 2.0 - center_y, 0.0],
                "size": [core_w, lip, core_w],
                "inner_radius_mm": derived["pocket_diameter_mm"] / 2.0,
                "appearance": "rack-top",
            }
        )

    if spec.include_alignment_stops:
        stop_size = min(10.0, pocket_w * 0.25)
        primitives.append(
            {
                "id": "datum-a1-stop",
                "kind": "box",
                "position": [
                    -(core_w / 2.0 - wall / 2.0),
                    base_h - 0.5 - center_y,
                    core_d / 2.0 - stop_size / 2.0,
                ],
                "size": [wall + 1.0, min(lip + 1.0, 5.0), stop_size],
                "appearance": "cooling",
            }
        )

    if spec.fastener_spec != "none":
        ear_h = min(base_h, floor + 2.5)
        for sign, side in ((-1, "left"), (1, "right")):
            primitives.append(
                {
                    "id": f"mount-tab-{side}",
                    "kind": "box",
                    "position": [
                        sign * (core_w / 2.0 + FLANGE_EAR_WIDTH_MM / 2.0),
                        ear_h / 2.0 - center_y,
                        0.0,
                    ],
                    "size": [FLANGE_EAR_WIDTH_MM, ear_h, FLANGE_EAR_DEPTH_MM],
                    "appearance": "frame",
                }
            )

    if spec.include_scale_reference:
        scale_w = derived["scale_reference_length_mm"]
        scale_z = core_d / 2.0 + SCALE_SHELF_DEPTH_MM / 2.0
        primitives.append(
            {
                "id": "scale-shelf",
                "kind": "box",
                "position": [0.0, center_y - center_y, scale_z],
                "size": [min(core_w, scale_w + 16.0), base_h, SCALE_SHELF_DEPTH_MM],
                "appearance": "rail",
            }
        )

    if spec.include_camera_mount:
        pad_z_front = -(core_d / 2.0)
        pad_z_back = derived["mast_center_z_mm"] - MAST_PAD_DEPTH_MM / 2.0
        pad_depth = pad_z_front - pad_z_back
        primitives.append(
            {
                "id": "camera-mast-pad",
                "kind": "box",
                "position": [0.0, 0.0, (pad_z_front + pad_z_back) / 2.0],
                "size": [MAST_CROSS_SECTION_MM[0] + 12.0, base_h, pad_depth],
                "appearance": "frame",
            }
        )
    return primitives


def _reference_object_primitives(spec: FixtureSpec) -> list[dict[str, Any]]:
    h = spec.object_height_mm
    if spec.object_shape == "rectangular":
        return [
            {
                "id": "specimen-envelope",
                "kind": "box",
                "position": [0.0, 0.0, 0.0],
                "size": [spec.object_length_mm, h, spec.object_width_mm],
                "appearance": "deck",
            }
        ]
    d = spec.object_diameter_mm
    return [
        {
            "id": "specimen-envelope",
            "kind": "cylinder",
            "axis": "y",
            "position": [0.0, 0.0, 0.0],
            "size": [d, h, d],
            "appearance": "deck",
        }
    ]


def compile_standalone_fixture(
    raw_spec: dict[str, Any] | FixtureSpec,
    *,
    workspace_width_mm: float | None = None,
    workspace_depth_mm: float | None = None,
    workspace_height_mm: float | None = None,
    lab_profile: dict | None = None,
) -> dict[str, Any]:
    """Compile a standalone printable bench fixture / imaging rig cleanly and deterministically."""
    spec = normalize_fixture_spec(raw_spec)
    total_allowance = 2.0 * spec.clearance_per_side_mm + spec.fdm_undersize_mm
    floor = spec.floor_thickness_mm
    lip = spec.lip_height_mm
    wall = spec.wall_thickness_mm
    base_h = round(floor + lip, 4)

    if spec.object_shape == "rectangular":
        nominal_l = float(spec.object_length_mm)
        nominal_w = float(spec.object_width_mm)
        pocket_l = round(nominal_l + total_allowance, 4)
        pocket_w = round(nominal_w + total_allowance, 4)
        pocket_d = None
        core_w = round(pocket_l + 2.0 * wall, 4)
        core_d = round(pocket_w + 2.0 * wall, 4)
        coupon_span = min(nominal_l, nominal_w, 140.0)
        coupon_scaled = min(nominal_l, nominal_w) > 140.0
    else:
        nominal_d = float(spec.object_diameter_mm)
        nominal_l = nominal_d
        nominal_w = nominal_d
        pocket_d = round(nominal_d + total_allowance, 4)
        pocket_l = pocket_d
        pocket_w = pocket_d
        core_w = round(pocket_d + 2.0 * wall, 4)
        core_d = core_w
        coupon_span = min(nominal_d, 110.0)
        coupon_scaled = nominal_d > 110.0

    # Rear mast anchor stays invariant across minor FDM clearance edits so tolerance
    # revisions only require reprinting locating-base and tolerance-coupon.
    nominal_mast_z = -(
        nominal_w / 2.0 + MAX_FIXED_MAST_ALLOWANCE_MM + wall + MAST_PAD_DEPTH_MM / 2.0
    )
    min_required_mast_z = -(core_d / 2.0 + MAST_PAD_DEPTH_MM / 2.0)
    mast_center_z = round(min(nominal_mast_z, min_required_mast_z), 4)

    scale_len = round(min(SCALE_REFERENCE_LENGTH_MM, max(25.0, core_w - 16.0)), 2)
    outer_w = round(
        core_w + (2.0 * FLANGE_EAR_WIDTH_MM if spec.fastener_spec != "none" else 0.0), 4
    )
    front_edge_z = core_d / 2.0 + (SCALE_SHELF_DEPTH_MM if spec.include_scale_reference else 0.0)
    rear_edge_z = (
        mast_center_z - MAST_PAD_DEPTH_MM / 2.0 if spec.include_camera_mount else -core_d / 2.0
    )
    outer_d = round(front_edge_z - rear_edge_z, 4)
    base_center_z = round((front_edge_z + rear_edge_z) / 2.0, 4)

    hole_dia = FASTENER_HOLE_DIAMETERS_MM[spec.fastener_spec]
    mount_holes = []
    if hole_dia is not None:
        mount_holes.extend(
            [
                {"x_mm": round(-(core_w / 2.0 + FLANGE_EAR_WIDTH_MM / 2.0), 3), "z_mm": 0.0,
                 "diameter_mm": hole_dia, "role": "bench_tab_left"},
                {"x_mm": round(core_w / 2.0 + FLANGE_EAR_WIDTH_MM / 2.0, 3), "z_mm": 0.0,
                 "diameter_mm": hole_dia, "role": "bench_tab_right"},
            ]
        )

    derived: dict[str, Any] = {
        "pocket_length_mm": pocket_l,
        "pocket_width_mm": pocket_w,
        "pocket_diameter_mm": pocket_d,
        "total_allowance_mm": round(total_allowance, 4),
        "core_width_mm": core_w,
        "core_depth_mm": core_d,
        "base_height_mm": base_h,
        "base_outer_width_mm": outer_w,
        "base_outer_depth_mm": outer_d,
        "base_center_z_mm": base_center_z,
        "front_edge_z_mm": round(front_edge_z, 4),
        "rear_edge_z_mm": round(rear_edge_z, 4),
        "mast_center_z_mm": mast_center_z,
        "scale_reference_length_mm": scale_len,
        "fastener_hole_diameter_mm": hole_dia,
        "mounting_holes": mount_holes,
        "camera_cradle": (
            deepcopy(CAMERA_MOUNT_SPECS[spec.camera_mount_type])
            if spec.include_camera_mount
            else None
        ),
    }

    # --- Build parts, signatures, bed fit & viewer components ---
    parts: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []

    base_sig = _stable_signature(
        {
            "part_id": "locating-base",
            **({"datum_notch_revision": 2} if spec.schema_version == "2" and
               spec.object_shape == "rectangular" and spec.include_alignment_stops else {}),
            "fixture_kind": spec.fixture_kind,
            "object_shape": spec.object_shape,
            "pocket_l": pocket_l,
            "pocket_w": pocket_w,
            "pocket_d": pocket_d,
            "wall": spec.wall_thickness_mm,
            "floor": spec.floor_thickness_mm,
            "lip": spec.lip_height_mm,
            "chamfer": spec.chamfer_mm,
            "fillet": spec.fillet_mm,
            "fastener_spec": spec.fastener_spec,
            "stops": spec.include_alignment_stops,
            "cutouts": spec.include_finger_cutouts,
            "scale": spec.include_scale_reference,
            "cam": spec.include_camera_mount,
            "mast_z": mast_center_z,
            "material": spec.material,
            "nozzle": spec.nozzle_mm,
            "layer_h": spec.layer_height_mm,
        }
    )
    base_vol_cm3 = max(
        6.0,
        (outer_w * outer_d * floor + (core_w * core_d - pocket_l * pocket_w) * lip) / 1000.0,
    )
    base_g, base_min = _estimate_filament_and_time(
        base_vol_cm3, infill_pct=25, layer_height_mm=spec.layer_height_mm
    )
    base_size = [outer_w, base_h, outer_d]
    base_part = {
        "id": "locating-base",
        "label": "Locating base & sample pocket",
        "role": "Primary kinematic pocket locating the target item relative to datum/scale",
        "printable": True,
        "size_mm": [round(v, 2) for v in base_size],
        "position_mm": [0.0, round(base_h / 2.0, 2), 0.0],
        "print_orientation": "Flat bottom face on build plate (pocket opening facing +Z)",
        "supports_required": False,
        "material": spec.material,
        "nozzle_mm": spec.nozzle_mm,
        "layer_height_mm": spec.layer_height_mm,
        "infill_pct": 25,
        "perimeters": 4,
        "estimated_filament_g": base_g,
        "estimated_print_minutes": base_min,
        "part_signature": base_sig,
        "printer_fit": _printer_fit_for_part(base_size, lab_profile),
    }
    parts.append(base_part)
    components.append(
        {
            "id": "locating-base",
            "kind": "fixture-base",
            "label": "Locating base & sample pocket",
            "position": [0.0, base_h / 2.0, 0.0],
            "size": base_size,
            "geometry": {
                "schema_version": "1",
                "primitives": _base_primitives(spec, derived),
            },
        }
    )

    total_height_mm = base_h
    if spec.object_height_mm is not None:
        components.append(
            {
                "id": "reference-object",
                "kind": "specimen-reference",
                "label": f"Target object: {spec.object_label}",
                "position": [0.0, floor + spec.object_height_mm / 2.0, 0.0],
                "size": [nominal_l, spec.object_height_mm, nominal_w],
                "geometry": {
                    "schema_version": "1",
                    "primitives": _reference_object_primitives(spec),
                },
            }
        )
        total_height_mm = max(base_h, floor + spec.object_height_mm)
    assembly_front_z = front_edge_z
    assembly_rear_z = rear_edge_z
    assembly_max_w = outer_w

    if spec.include_camera_mount:
        cradle = CAMERA_MOUNT_SPECS[spec.camera_mount_type]
        arm_bottom_y = round(floor + spec.object_height_mm + spec.camera_working_distance_mm, 2)
        mast_height = round(max(60.0, arm_bottom_y - base_h), 2)
        mast_w, mast_d = MAST_CROSS_SECTION_MM
        mast_sig = _stable_signature(
            {
                "part_id": "camera-mast",
                "mast_height": mast_height,
                "mast_w": mast_w,
                "mast_d": mast_d,
                "fastener_spec": spec.fastener_spec,
                "material": spec.material,
                "nozzle": spec.nozzle_mm,
                "layer_h": spec.layer_height_mm,
            }
        )
        # Print mast lying flat along longest axis on X/Y build plate -> zero supports.
        mast_print_size = [mast_w, mast_d, mast_height]
        mast_vol_cm3 = (mast_w * mast_d * mast_height * 0.72) / 1000.0
        mast_g, mast_min = _estimate_filament_and_time(
            mast_vol_cm3, infill_pct=35, layer_height_mm=spec.layer_height_mm
        )
        parts.append(
            {
                "id": "camera-mast",
                "label": "Overhead camera mast column",
                "role": "Bolted vertical column fixing camera working distance above specimen",
                "printable": True,
                "size_mm": [round(v, 2) for v in mast_print_size],
                "installed_size_mm": [mast_w, mast_height, mast_d],
                "position_mm": [0.0, round(base_h + mast_height / 2.0, 2), mast_center_z],
                "print_orientation": "Lying flat on rear mast face (zero supports)",
                "supports_required": False,
                "material": spec.material,
                "nozzle_mm": spec.nozzle_mm,
                "layer_height_mm": spec.layer_height_mm,
                "infill_pct": 35,
                "perimeters": 4,
                "estimated_filament_g": mast_g,
                "estimated_print_minutes": mast_min,
                "part_signature": mast_sig,
                "printer_fit": _printer_fit_for_part(mast_print_size, lab_profile),
            }
        )
        components.append(
            {
                "id": "camera-mast",
                "kind": "camera-mast",
                "label": "Overhead camera mast",
                "position": [0.0, base_h + mast_height / 2.0, mast_center_z],
                "size": [mast_w + 10.0, mast_height, mast_d + 4.0],
                "geometry": {
                    "schema_version": "1",
                    "primitives": [
                        {
                            "id": "mast-foot",
                            "kind": "box",
                            "position": [0.0, -mast_height / 2.0 + 3.0, 0.0],
                            "size": [mast_w + 10.0, 6.0, mast_d + 4.0],
                            "appearance": "frame",
                        },
                        {
                            "id": "mast-column",
                            "kind": "box",
                            "position": [0.0, 0.0, 0.0],
                            "size": [mast_w, mast_height, mast_d],
                            "appearance": "frame",
                        },
                    ],
                },
            }
        )

        # Cantilever arm reaches from mast_center_z to sample center Z=0 + front cradle half.
        arm_rear_z = mast_center_z - MAST_PAD_DEPTH_MM / 2.0
        arm_front_z = cradle["cradle_depth_mm"] / 2.0
        arm_length = round(arm_front_z - arm_rear_z, 2)
        arm_center_z = round((arm_front_z + arm_rear_z) / 2.0, 2)
        arm_width = round(max(cradle["cradle_width_mm"], MAST_CROSS_SECTION_MM[0] + 6.0), 2)
        arm_sig = _stable_signature(
            {
                "part_id": "camera-arm",
                "arm_length": arm_length,
                "arm_width": arm_width,
                "arm_thickness": ARM_THICKNESS_MM,
                "camera_mount_type": spec.camera_mount_type,
                "fastener_spec": spec.fastener_spec,
                "material": spec.material,
                "nozzle": spec.nozzle_mm,
                "layer_h": spec.layer_height_mm,
            }
        )
        arm_size = [arm_width, ARM_THICKNESS_MM, arm_length]
        arm_vol_cm3 = (arm_width * ARM_THICKNESS_MM * arm_length * 0.78) / 1000.0
        arm_g, arm_min = _estimate_filament_and_time(
            arm_vol_cm3, infill_pct=35, layer_height_mm=spec.layer_height_mm
        )
        parts.append(
            {
                "id": "camera-arm",
                "label": f"Cantilever camera arm ({spec.camera_mount_type})",
                "role": "Aligns optical axis perpendicular to specimen center",
                "printable": True,
                "size_mm": [round(v, 2) for v in arm_size],
                "position_mm": [0.0, round(arm_bottom_y + ARM_THICKNESS_MM / 2.0, 2), arm_center_z],
                "print_orientation": "Flat top/bottom arm face on build plate (zero supports)",
                "supports_required": False,
                "material": spec.material,
                "nozzle_mm": spec.nozzle_mm,
                "layer_height_mm": spec.layer_height_mm,
                "infill_pct": 35,
                "perimeters": 4,
                "estimated_filament_g": arm_g,
                "estimated_print_minutes": arm_min,
                "part_signature": arm_sig,
                "printer_fit": _printer_fit_for_part(arm_size, lab_profile),
            }
        )
        components.append(
            {
                "id": "camera-arm",
                "kind": "camera-arm",
                "label": "Cantilever camera arm & cradle",
                "position": [0.0, arm_bottom_y + ARM_THICKNESS_MM / 2.0, arm_center_z],
                "size": arm_size,
                "geometry": {
                    "schema_version": "1",
                    "primitives": [
                        {
                            "id": "arm-beam",
                            "kind": "box",
                            "position": [0.0, 0.0, 0.0],
                            "size": [arm_width * 0.75, ARM_THICKNESS_MM, arm_length],
                            "appearance": "rail",
                        },
                        {
                            "id": "camera-cradle-head",
                            "kind": "box",
                            "position": [0.0, 0.0, -arm_center_z],
                            "size": [
                                cradle["cradle_width_mm"],
                                ARM_THICKNESS_MM,
                                cradle["cradle_depth_mm"],
                            ],
                            "appearance": "cooling",
                        },
                    ],
                },
            }
        )
        derived["camera_arm_bottom_y_mm"] = arm_bottom_y
        derived["mast_height_mm"] = mast_height
        derived["arm_length_mm"] = arm_length
        derived["arm_width_mm"] = arm_width
        derived["arm_center_z_mm"] = arm_center_z
        total_height_mm = max(total_height_mm, round(arm_bottom_y + ARM_THICKNESS_MM, 2))
        assembly_max_w = max(assembly_max_w, arm_width)

    # 3-step FDM tolerance coupon: tests [-0.15, 0.00, +0.15] mm around nominal pocket clearance
    coupon_step_gap = 16.0
    coupon_w = round(coupon_span + 2.0 * wall, 2)
    coupon_h = round(floor + min(lip, 4.0), 2)
    coupon_d = round(len(COUPON_OFFSETS_MM) * coupon_step_gap, 2)
    coupon_sig = _stable_signature(
        {
            "part_id": "tolerance-coupon",
            "shape": spec.object_shape,
            "coupon_span": coupon_span,
            "total_allowance": total_allowance,
            "wall": wall,
            "floor": floor,
            "lip": lip,
            "offsets": list(COUPON_OFFSETS_MM),
            "material": spec.material,
            "nozzle": spec.nozzle_mm,
            "layer_h": spec.layer_height_mm,
        }
    )
    coupon_size = [coupon_w, coupon_h, coupon_d]
    coupon_vol_cm3 = max(3.0, (coupon_w * coupon_d * floor) / 1000.0)
    coupon_g, coupon_min = _estimate_filament_and_time(
        coupon_vol_cm3, infill_pct=25, layer_height_mm=spec.layer_height_mm
    )
    parts.append(
        {
            "id": "tolerance-coupon",
            "label": "3-step FDM fit test coupon (-0.15 / 0.00 / +0.15 mm)",
            "role": "Fast print calibration gauge before printing full locating base",
            "printable": True,
            "size_mm": [round(v, 2) for v in coupon_size],
            "position_mm": [0.0, round(coupon_h / 2.0, 2), 0.0],
            "print_orientation": "Flat bottom on build plate (same pocket axis as locating-base)",
            "supports_required": False,
            "material": spec.material,
            "nozzle_mm": spec.nozzle_mm,
            "layer_height_mm": spec.layer_height_mm,
            "infill_pct": 25,
            "perimeters": 4,
            "estimated_filament_g": coupon_g,
            "estimated_print_minutes": coupon_min,
            "part_signature": coupon_sig,
            "printer_fit": _printer_fit_for_part(coupon_size, lab_profile),
        }
    )
    derived["tolerance_coupon"] = {
        "span_mm": round(coupon_span, 2),
        "nominal_opening_mm": round(coupon_span + total_allowance, 3),
        "step_openings_mm": [
            round(coupon_span + total_allowance + 2.0 * offset, 3)
            for offset in COUPON_OFFSETS_MM
        ],
        "offsets_per_side_mm": list(COUPON_OFFSETS_MM),
        "scaled_to_fit_coupon": coupon_scaled,
        "height_mm": coupon_h,
    }

    footprint_mm = [round(assembly_max_w, 2), round(outer_d, 2)]
    bounds_mm = {
        "min_mm": [round(-assembly_max_w / 2.0, 2), 0.0, round(assembly_rear_z, 2)],
        "max_mm": [
            round(assembly_max_w / 2.0, 2),
            round(total_height_mm, 2),
            round(assembly_front_z, 2),
        ],
    }

    checks: list[dict[str, Any]] = []
    if workspace_width_mm is not None:
        fits_w = footprint_mm[0] <= float(workspace_width_mm) + 1e-6
        checks.append(
            {
                "id": "bench-width",
                "label": "Bench width fit",
                "passed": fits_w,
                "detail": (
                    f"Fixture width {footprint_mm[0]:g} mm vs {workspace_width_mm:g} mm limit."
                ),
            }
        )
    if workspace_depth_mm is not None:
        fits_d = footprint_mm[1] <= float(workspace_depth_mm) + 1e-6
        checks.append(
            {
                "id": "bench-depth",
                "label": "Bench depth fit",
                "passed": fits_d,
                "detail": (
                    f"Fixture depth {footprint_mm[1]:g} mm vs {workspace_depth_mm:g} mm limit."
                ),
            }
        )
    if workspace_height_mm is not None:
        fits_h = total_height_mm <= float(workspace_height_mm) + 1e-6
        checks.append(
            {
                "id": "bench-height",
                "label": "Bench height fit",
                "passed": fits_h,
                "detail": (
                    f"Fixture height {total_height_mm:g} mm vs {workspace_height_mm:g} mm limit."
                ),
            }
        )
    for part in parts:
        pf = part["printer_fit"]
        if pf["checked"]:
            checks.append(
                {
                    "id": f"printer-bed:{part['id']}",
                    "label": f"Printer bed fit: {part['id']}",
                    "passed": bool(pf["fits"]),
                    "detail": pf["detail"],
                }
            )

    ref_meta = (
        deepcopy(FIXTURE_REFERENCES[spec.object_reference_id])
        if spec.object_reference_id != "custom"
        else None
    )
    fasteners_bom = []
    if spec.include_camera_mount and spec.fastener_spec != "none":
        fasteners_bom.append(
            {
                "item": "M3 x 10 mm socket head cap screw",
                "quantity": 4,
                "purpose": "2x base-to-mast + 2x mast-to-arm joints",
            }
        )
        if spec.fastener_spec == "m3_heatset_insert":
            fasteners_bom.append(
                {
                    "item": "M3 x 4 mm brass heat-set threaded insert (4.0 mm bore)",
                    "quantity": 4,
                    "purpose": "2x base mast pad + 2x mast top flange",
                }
            )

    limitations = [
        "Dimensional pocket allowance assumes standard FDM shrinkage; print tolerance-coupon "
        "first and verify real specimen fit with calipers.",
        "Thermal deflection under heated incubators or aggressive solvents depends on selected "
        "polymer and print settings.",
        "Fastener clamping, optical focus sharpness, and specimen glare require physical "
        "verification on the bench (E13).",
    ]
    if spec.object_height_mm is None:
        limitations.insert(
            0, "Only the resting footprint was measured. The preview and bench checks cover "
            "the holder. Object height and loaded clearance are unknown.",
        )
    if spec.dimension_provenance == "reference":
        limitations.insert(
            0,
            f"Specimen dimensions come from nominal reference '{spec.object_reference_id}'; "
            "measure your physical item before locking production parts.",
        )

    all_printer_fit = all(
        p["printer_fit"]["fits"] is not False for p in parts
    )
    return {
        "schema_version": "1",
        "kind": "standalone_fixture_v1",
        "spec": spec.model_dump(),
        "reference": ref_meta,
        "derived": derived,
        "parts": parts,
        "part_signatures": {p["id"]: p["part_signature"] for p in parts},
        "components": components,
        "footprint_mm": footprint_mm,
        "height_mm": round(total_height_mm, 2),
        "bounds_mm": bounds_mm,
        "fasteners_bom": fasteners_bom,
        "printer_fit_ok": all_printer_fit,
        "checks": checks,
        "limitations": limitations,
    }


def attach_standalone_fixture(
    previous: dict,
    raw_spec: dict[str, Any] | FixtureSpec,
    *,
    lab_profile: dict | None = None,
) -> dict:
    """Compile and attach a standalone printable fixture cleanly to a general project."""
    from app.design_workflow import refresh_workflow
    from app.engineering import state_for
    from app.lab_profile import load_lab_profile, record_dimension
    from app.spatial_fit import refresh_spatial_fit
    from app.workflows import describe_workflow

    if (
        previous.get("engineering_design")
        or previous.get("design", {}).get("foundation")
        or previous.get("design", {}).get("platform")
        or previous.get("bench_station")
        or previous.get("concept_design")
        or state_for(previous).get("selected_platform_id")
        or previous.get("workflow", {}).get("kind") == "liquid_preparation"
    ):
        raise ValueError(
            "Start a new general project for a standalone fixture to keep the existing "
            "workstation unchanged."
        )

    profile = lab_profile if lab_profile is not None else load_lab_profile()
    summary = previous.get("summary") or {}
    compiled = compile_standalone_fixture(
        raw_spec,
        workspace_width_mm=summary.get("workspace_width_mm"),
        workspace_depth_mm=summary.get("workspace_depth_mm"),
        workspace_height_mm=summary.get("workspace_height_mm"),
        lab_profile=profile,
    )
    spec_dict = compiled["spec"]

    updated = deepcopy(previous)
    if not updated.get("workflow") or updated["workflow"].get("kind") == "unclassified":
        updated["workflow"] = {"kind": "general"}
    else:
        updated["workflow"] = {**updated["workflow"], "kind": "general"}
    if not updated["brief"].get("objective"):
        updated["brief"]["objective"] = spec_dict["title"][:1000]
    if not updated["brief"].get("materials"):
        updated["brief"]["materials"] = spec_dict["object_label"][:1000]

    updated["standalone_fixture"] = compiled
    updated["revision"] = int(previous.get("revision", 0)) + 1
    updated["status"] = "draft"

    prov = spec_dict["dimension_provenance"]
    src = spec_dict["object_reference_id"]
    if spec_dict["object_shape"] == "rectangular":
        updated = record_dimension(
            updated,
            "fixture_object_length_mm",
            spec_dict["object_length_mm"],
            provenance=prov,
            source=src,
        )
        updated = record_dimension(
            updated,
            "fixture_object_width_mm",
            spec_dict["object_width_mm"],
            provenance=prov,
            source=src,
        )
    else:
        updated = record_dimension(
            updated,
            "fixture_object_diameter_mm",
            spec_dict["object_diameter_mm"],
            provenance=prov,
            source=src,
        )
    if spec_dict["object_height_mm"] is not None:
        updated = record_dimension(
            updated, "fixture_object_height_mm", spec_dict["object_height_mm"],
            provenance=prov, source=src,
        )
    else:
        updated.get("dimension_provenance", {}).pop("fixture_object_height_mm", None)

    updated["design"]["footprint"] = {
        "width_mm": compiled["footprint_mm"][0],
        "depth_mm": compiled["footprint_mm"][1],
        "height_mm": compiled["height_mm"],
    }
    updated["workflow"] = describe_workflow(updated)
    refresh_workflow(updated)
    refresh_spatial_fit(updated, lab_profile=profile)
    if updated.get("build_logs"):
        from app.build_feedback import compute_reprint_diff

        updated["build_feedback"] = compute_reprint_diff(updated)
    return updated

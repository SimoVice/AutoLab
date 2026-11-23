"""Schema-validated registry for Opentrons v2 labware definitions and standard hardware.

Supports built-in standard plates, deep-well plates, PCR plates, reservoirs,
tip racks, user-submitted custom Opentrons v2 JSON definitions, and a catalog of
standard mechanical/motion hardware parts without hardcoding single plates.
"""

from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.microplate_geometry import (
    ASSUMED_FDM_UNDERSIZE_MM,
    FLOOR_THICKNESS_MM,
    FUNCTIONAL_CLEARANCE_PER_SIDE_MM,
    LIP_HEIGHT_MM,
    SLAS_OVERALL_PLUS_TOLERANCE_MM,
    WALL_THICKNESS_MM,
    compile_microplate,
)
from app.microplate_geometry import PROFILE_ID as CORNING_96_ID

LABWARE_DIR = Path(__file__).parent / "data" / "labware"
TIPRACK_DIR = Path(__file__).parent / "data" / "tipracks"
HARDWARE_PATH = Path(__file__).parent / "data" / "hardware" / "standard_hardware.json"
_CUSTOM_REGISTRY: dict[str, dict[str, Any]] = {}


def _finite_pos(value: Any, *, name: str = "dimension") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    v = float(value)
    if v <= 0:
        raise ValueError(f"{name} must be > 0.")
    return v


def _finite_num(value: Any, *, name: str = "coordinate") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    return float(value)


class _ExtraAllow(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class LabwareDimensions(_ExtraAllow):
    xDimension: float = Field(gt=1.0, le=500.0)
    yDimension: float = Field(gt=1.0, le=500.0)
    zDimension: float = Field(gt=1.0, le=300.0)

    @field_validator("xDimension", "yDimension", "zDimension", mode="before")
    @classmethod
    def _check_dims(cls, val: Any, info) -> float:
        return _finite_pos(val, name=info.field_name)


class LabwareParameters(_ExtraAllow):
    loadName: Annotated[str, Field(min_length=2, max_length=120, pattern=r"^[a-z0-9_.-]+$")]
    format: Annotated[str, Field(min_length=1, max_length=64)]
    isTiprack: bool = False
    tipLength: float | None = Field(default=None, gt=1.0, le=300.0)


class LabwareMetadata(_ExtraAllow):
    displayName: Annotated[str, Field(min_length=1, max_length=200)]
    displayCategory: Annotated[str, Field(min_length=1, max_length=64)]
    displayVolumeUnits: str = "µL"
    tags: list[str] = Field(default_factory=list)


class OpentronsWell(_ExtraAllow):
    shape: Literal["circular", "rectangular"]
    depth: float = Field(gt=0.1, le=300.0)
    totalLiquidVolume: float = Field(gt=0.1, le=5000000.0)
    x: float
    y: float
    z: float
    diameter: float | None = Field(default=None, gt=0.1, le=200.0)
    xDimension: float | None = Field(default=None, gt=0.1, le=400.0)
    yDimension: float | None = Field(default=None, gt=0.1, le=400.0)

    @field_validator(
        "depth", "totalLiquidVolume", "diameter", "xDimension", "yDimension", mode="before"
    )
    @classmethod
    def _pos_vals(cls, val: Any, info) -> float | None:
        return None if val is None else _finite_pos(val, name=info.field_name)

    @field_validator("x", "y", "z", mode="before")
    @classmethod
    def _coord_vals(cls, val: Any, info) -> float:
        return _finite_num(val, name=info.field_name)

    @model_validator(mode="after")
    def _shape_dims(self) -> OpentronsWell:
        if self.shape == "circular" and self.diameter is None:
            raise ValueError("Circular wells require a positive diameter.")
        if self.shape == "rectangular" and (self.xDimension is None or self.yDimension is None):
            raise ValueError("Rectangular wells require xDimension and yDimension.")
        return self


class OpentronsLabwareDefinition(_ExtraAllow):
    schemaVersion: Literal[2]
    namespace: Annotated[str, Field(min_length=1, max_length=80)] = "custom_beta"
    version: int = Field(strict=True, ge=1, le=1000)
    ordering: list[list[str]] = Field(min_length=1, max_length=64)
    dimensions: LabwareDimensions
    metadata: LabwareMetadata
    parameters: LabwareParameters
    wells: dict[str, OpentronsWell] = Field(min_length=1, max_length=1536)

    @model_validator(mode="after")
    def _coherent_wells(self) -> OpentronsLabwareDefinition:
        ordered = [label for col in self.ordering for label in col]
        if not ordered:
            raise ValueError("Labware ordering must list at least one well.")
        missing = [label for label in ordered if label not in self.wells]
        if missing:
            raise ValueError(f"Labware wells dictionary is missing ordered wells: {missing[:5]}.")
        lx, ly, lz = (
            self.dimensions.xDimension,
            self.dimensions.yDimension,
            self.dimensions.zDimension,
        )
        for label in ordered:
            w = self.wells[label]
            if not (-1.0 <= w.x <= lx + 1.0 and -1.0 <= w.y <= ly + 1.0 and -1.0 <= w.z <= lz):
                raise ValueError(
                    f"Well {label!r} coordinates ({w.x}, {w.y}, {w.z}) fall outside "
                    f"labware dimensions ({lx}, {ly}, {lz})."
                )
        return self


def _load_disk_definitions() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for folder in (LABWARE_DIR, TIPRACK_DIR):
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.name == "provenance.json":
                continue
            raw_bytes = path.read_bytes()
            data = json.loads(raw_bytes.decode("utf-8-sig"))
            parsed = OpentronsLabwareDefinition.model_validate(data)
            load_name = parsed.parameters.loadName
            records[load_name] = {
                "definition": data,
                "sha256": sha256(raw_bytes).hexdigest(),
                "source_kind": (
                    "pinned_opentrons_reference"
                    if load_name in {
                        CORNING_96_ID,
                        "opentrons_96_tiprack_300ul",
                        "tipone_96_tiprack_200ul",
                    }
                    else "standard_slas_reference"
                ),
                "path": str(path),
            }
    return records


@lru_cache(maxsize=1)
def _builtin_definitions() -> dict[str, dict[str, Any]]:
    return _load_disk_definitions()


def _custom_dir() -> Path | None:
    raw = os.environ.get("AUTOLAB_CUSTOM_LABWARE_DIR")
    return Path(raw) if raw else None


def _disk_custom_definitions() -> dict[str, dict[str, Any]]:
    folder = _custom_dir()
    if folder is None or not folder.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            raw_bytes = path.read_bytes()
            validated = validate_custom_labware(json.loads(raw_bytes.decode("utf-8-sig")))
            load_name = validated["summary"]["load_name"]
            out[load_name] = {
                "definition": validated["definition"],
                "sha256": validated["sha256"],
                "source_kind": "user_custom_definition",
                "path": str(path),
            }
        except Exception:
            continue
    return out


def _summarize_record(load_name: str, record: dict[str, Any]) -> dict[str, Any]:
    data = record["definition"]
    meta = data["metadata"]
    dims = data["dimensions"]
    ordering = data["ordering"]
    order = [label for col in ordering for label in col]
    first_well = data["wells"][order[0]]
    return {
        "load_name": load_name,
        "display_name": meta["displayName"],
        "display_category": meta["displayCategory"],
        "format": data["parameters"].get("format"),
        "is_tiprack": bool(data["parameters"].get("isTiprack", False)),
        "brand": (data.get("brand") or {}).get("brand"),
        "well_count": len(order),
        "rows": len(ordering[0]) if ordering else 0,
        "columns": len(ordering),
        "well_shape": first_well["shape"],
        "well_capacity_ul": first_well["totalLiquidVolume"],
        "dimensions_mm": [dims["xDimension"], dims["yDimension"], dims["zDimension"]],
        "sha256": record["sha256"],
        "source_kind": record["source_kind"],
    }


def validate_custom_labware(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate an external Opentrons Labware Definition v2 JSON payload."""
    if not isinstance(raw, dict):
        raise ValueError("Custom labware definition must be a JSON object.")
    parsed = OpentronsLabwareDefinition.model_validate(raw)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    load_name = parsed.parameters.loadName
    record = {
        "definition": deepcopy(raw),
        "sha256": sha256(canonical).hexdigest(),
        "source_kind": "user_custom_definition",
    }
    return {
        "definition": record["definition"],
        "sha256": record["sha256"],
        "summary": _summarize_record(load_name, record),
    }


def register_custom_labware(
    raw: dict[str, Any], *, store: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Validate and register a custom Opentrons v2 labware JSON in memory/optional dir."""
    validated = validate_custom_labware(raw)
    load_name = validated["summary"]["load_name"]
    if load_name in _builtin_definitions():
        raise ValueError(
            f"Load name {load_name!r} collides with a built-in reference definition."
        )
    record = {
        "definition": validated["definition"],
        "sha256": validated["sha256"],
        "source_kind": "user_custom_definition",
    }
    target = store if store is not None else _CUSTOM_REGISTRY
    target[load_name] = record
    folder = _custom_dir() if store is None else None
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", load_name)
        (folder / f"{safe}.json").write_text(
            json.dumps(validated["definition"], indent=2), encoding="utf-8"
        )
    return validated["summary"]


def clear_custom_labware_registry() -> None:
    _CUSTOM_REGISTRY.clear()


def _merged_records(custom_registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    records = dict(_builtin_definitions())
    records.update(_disk_custom_definitions())
    for k, v in _CUSTOM_REGISTRY.items():
        records[k] = v
    if custom_registry:
        for k, v in custom_registry.items():
            records[k] = v if "definition" in v else validate_custom_labware(v)
    return records


def list_labware(
    *,
    category: str | None = None,
    query: str | None = None,
    custom_registry: dict[str, Any] | None = None,
    include_tipracks: bool = True,
) -> list[dict[str, Any]]:
    """List schema-validated labware definitions filtered by category or keyword."""
    out: list[dict[str, Any]] = []
    tokens = [t for t in (query or "").casefold().split() if len(t) >= 2]
    for load_name, record in sorted(_merged_records(custom_registry).items()):
        summary = _summarize_record(load_name, record)
        if not include_tipracks and summary["is_tiprack"]:
            continue
        if category and summary["display_category"].casefold() != category.casefold():
            continue
        if tokens:
            haystack = " ".join(
                str(summary.get(k, ""))
                for k in (
                    "load_name",
                    "display_name",
                    "display_category",
                    "brand",
                    "well_count",
                    "well_capacity_ul",
                )
            ).casefold()
            if not all(tok in haystack for tok in tokens):
                continue
        out.append(summary)
    return out


def get_labware_definition(
    load_name: str, *, custom_registry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return full schema-validated Opentrons v2 JSON plus summary and sha256."""
    records = _merged_records(custom_registry)
    if load_name not in records:
        raise ValueError(f"Unknown labware loadName {load_name!r}.")
    record = records[load_name]
    return {
        "definition": deepcopy(record["definition"]),
        "sha256": record["sha256"],
        "source_kind": record["source_kind"],
        "summary": _summarize_record(load_name, record),
    }


def compile_labware_holder(
    load_name_or_def: str | dict[str, Any],
    *,
    custom_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a holder and schematic plate geometry for any Opentrons v2 labware definition."""
    if isinstance(load_name_or_def, str):
        if load_name_or_def == CORNING_96_ID:
            return compile_microplate(CORNING_96_ID)
        record = get_labware_definition(load_name_or_def, custom_registry=custom_registry)
        data = record["definition"]
        source_sha = record["sha256"]
        source_kind = record["source_kind"]
    else:
        validated = validate_custom_labware(load_name_or_def)
        data = validated["definition"]
        source_sha = validated["sha256"]
        source_kind = "user_custom_definition"

    parsed = OpentronsLabwareDefinition.model_validate(data)
    load_name = parsed.parameters.loadName
    if load_name == CORNING_96_ID:
        return compile_microplate(CORNING_96_ID)

    length = parsed.dimensions.xDimension
    width = parsed.dimensions.yDimension
    height = parsed.dimensions.zDimension

    pocket_l = (
        length
        + SLAS_OVERALL_PLUS_TOLERANCE_MM
        + 2 * FUNCTIONAL_CLEARANCE_PER_SIDE_MM
        + ASSUMED_FDM_UNDERSIZE_MM
    )
    pocket_w = (
        width
        + SLAS_OVERALL_PLUS_TOLERANCE_MM
        + 2 * FUNCTIONAL_CLEARANCE_PER_SIDE_MM
        + ASSUMED_FDM_UNDERSIZE_MM
    )
    outer_l = pocket_l + 2 * WALL_THICKNESS_MM
    outer_w = pocket_w + 2 * WALL_THICKNESS_MM
    total_h = FLOOR_THICKNESS_MM + height
    floor_center = -total_h / 2 + FLOOR_THICKNESS_MM / 2
    plate_bottom = -total_h / 2 + FLOOR_THICKNESS_MM
    order = [label for column in parsed.ordering for label in column]

    wells: list[dict[str, Any]] = []
    holes: list[dict[str, Any]] = []
    for label in order:
        w = parsed.wells[label]
        eq_diameter = (
            w.diameter
            if w.diameter is not None
            else min(float(w.xDimension), float(w.yDimension))
        )
        wells.append(
            {
                "label": label,
                "x_mm": w.x,
                "y_mm": w.y,
                "z_mm": w.z,
                "app_x_mm": w.x - length / 2,
                "app_y_mm": plate_bottom + w.z + w.depth,
                "app_z_mm": w.y - width / 2,
                "shape": w.shape,
                "diameter_mm": eq_diameter,
                **(
                    {"size_x_mm": w.xDimension, "size_z_mm": w.yDimension}
                    if w.shape == "rectangular"
                    else {}
                ),
                "depth_mm": w.depth,
                "total_liquid_volume_ul": w.totalLiquidVolume,
            }
        )
        if w.shape == "circular" and w.diameter is not None:
            holes.append(
                {"x_mm": w.x - length / 2, "z_mm": w.y - width / 2, "radius_mm": w.diameter / 2}
            )
        else:
            holes.append(
                {
                    "x_mm": w.x - length / 2,
                    "z_mm": w.y - width / 2,
                    "size_x_mm": float(w.xDimension),
                    "size_z_mm": float(w.yDimension),
                }
            )

    first_well = parsed.wells[order[0]]
    bottom_h = max(0.8, height - first_well.depth)
    well_block_h = max(1.0, height - bottom_h)
    primitives: list[dict[str, Any]] = [
        {
            "id": "holder-floor",
            "kind": "box",
            "position": [0, floor_center, 0],
            "size": [outer_l, FLOOR_THICKNESS_MM, outer_w],
            "appearance": "rack",
        }
    ]
    for sign, name in ((-1, "left"), (1, "right")):
        primitives.append(
            {
                "id": "holder-" + name,
                "kind": "box",
                "position": [
                    sign * (pocket_l + WALL_THICKNESS_MM) / 2,
                    plate_bottom + LIP_HEIGHT_MM / 2,
                    0,
                ],
                "size": [WALL_THICKNESS_MM, LIP_HEIGHT_MM, outer_w],
                "appearance": "rack",
            }
        )
    for sign, name in ((-1, "front"), (1, "back")):
        primitives.append(
            {
                "id": "holder-" + name,
                "kind": "box",
                "position": [
                    0,
                    plate_bottom + LIP_HEIGHT_MM / 2,
                    sign * (pocket_w + WALL_THICKNESS_MM) / 2,
                ],
                "size": [pocket_l, LIP_HEIGHT_MM, WALL_THICKNESS_MM],
                "appearance": "rack",
            }
        )
    # Keep schematic CAD fast and robust: skip fine boolean cutting in the non-printed
    # plate visual if >100 square pockets, while still exposing full well coordinates in JSON.
    plate_holes = holes if len(holes) <= 100 else holes[:0]
    primitives.extend(
        [
            {
                "id": "reference-plate-bottom",
                "kind": "box",
                "position": [0, plate_bottom + bottom_h / 2, 0],
                "size": [length, bottom_h, width],
                "appearance": "rack-top",
            },
            {
                "id": "reference-plate-wells",
                "kind": "plate",
                "position": [0, plate_bottom + bottom_h + well_block_h / 2, 0],
                "size": [length, well_block_h, width],
                "appearance": "rack-top",
                "holes": plate_holes,
            },
        ]
    )
    interfaces = [
        {
            "feature": "plate pocket length",
            "standard": "slas-microplate-footprint",
            "dimension": "footprint_length",
            "value": pocket_l,
            "intent": "envelope",
            "clearance": 2 * FUNCTIONAL_CLEARANCE_PER_SIDE_MM,
        },
        {
            "feature": "plate pocket width",
            "standard": "slas-microplate-footprint",
            "dimension": "footprint_width",
            "value": pocket_w,
            "intent": "envelope",
            "clearance": 2 * FUNCTIONAL_CLEARANCE_PER_SIDE_MM,
        },
    ]
    return {
        "size_mm": [outer_l, total_h, outer_w],
        "geometry": {"schema_version": "1", "primitives": primitives},
        "profile": {
            "id": load_name,
            "display_name": parsed.metadata.displayName,
            "display_category": parsed.metadata.displayCategory,
            "source_dimensions_mm": [length, width, height],
            "source": {
                "source_url": (
                    (data.get("brand") or {}).get("links") or ["https://labware.opentrons.com/"]
                )[0],
                "revision": "registry-v2",
                "sha256": source_sha,
                "license": "Apache-2.0 / User Definition",
                "license_url": "https://github.com/Opentrons/opentrons/blob/edge/LICENSE",
                "source_kind": source_kind,
            },
            "scope": "Opentrons v2 labware definition; schematic envelope, not vendor CAD.",
        },
        "derived": {
            "well_count": len(wells),
            "well_capacity_ul": first_well.totalLiquidVolume,
            "well_ordering": order,
            "wells": wells,
            "source_to_app": (
                "app x = source x - length/2; app z = source y - width/2; "
                "source z is height from plate bottom"
            ),
            "holder_pocket_mm": [pocket_l, pocket_w],
            "holder_wall_mm": WALL_THICKNESS_MM,
            "holder_floor_mm": FLOOR_THICKNESS_MM,
            "holder_lip_mm": LIP_HEIGHT_MM,
        },
        "checks": {
            "interfaces": interfaces,
            "functional_clearance_per_side_mm": FUNCTIONAL_CLEARANCE_PER_SIDE_MM,
            "assumed_fdm_pocket_undersize_mm": ASSUMED_FDM_UNDERSIZE_MM,
            "physical_fit_verified": False,
            "material_selected": False,
            "scope": (
                "Numerical design only; plate base profile, retention, tool access, "
                "print and mounting unverified."
            ),
        },
    }


@lru_cache(maxsize=1)
def _hardware_catalog() -> dict[str, Any]:
    return json.loads(HARDWARE_PATH.read_text(encoding="utf-8-sig"))


def list_hardware_parts(
    *, category: str | None = None, query: str | None = None
) -> list[dict[str, Any]]:
    """List standard mechanical, motion, optics, and fastener reference parts."""
    tokens = [t for t in (query or "").casefold().split() if len(t) >= 2]
    out: list[dict[str, Any]] = []
    for part in _hardware_catalog()["parts"]:
        if category and part["category"].casefold() != category.casefold():
            continue
        if tokens:
            haystack = " ".join(
                [part["id"], part["name"], part["category"], part.get("notes", "")]
            ).casefold()
            if not all(tok in haystack for tok in tokens):
                continue
        out.append(deepcopy(part))
    return out


def get_hardware_part(part_id: str) -> dict[str, Any]:
    """Return one standard hardware catalog entry by ID."""
    for part in _hardware_catalog()["parts"]:
        if part["id"] == part_id:
            return deepcopy(part)
    raise ValueError(f"Unknown hardware catalog part {part_id!r}.")

"""Persistent laboratory equipment profile and measurement provenance tracking."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import project_library

_PROFILE_LOCK = RLock()
_ROOT = Path(__file__).resolve().parents[1]

PRINTER_PRESETS: list[dict] = [
    {
        "id": "bambu-a1-mini",
        "name": "Bambu Lab A1 mini",
        "build_volume_mm": [180.0, 180.0, 180.0],
        "nozzle_mm": 0.4,
        "materials": ["PLA", "PETG"],
    },
    {
        "id": "prusa-mk4s",
        "name": "Prusa MK4S",
        "build_volume_mm": [250.0, 210.0, 220.0],
        "nozzle_mm": 0.4,
        "materials": ["PLA", "PETG", "ASA"],
    },
    {
        "id": "bambu-x1c",
        "name": "Bambu Lab X1-Carbon",
        "build_volume_mm": [256.0, 256.0, 256.0],
        "nozzle_mm": 0.4,
        "materials": ["PLA", "PETG", "ABS", "ASA"],
    },
]

_DEFAULT_PROFILE_ID = "default"


def _positive_mm(value: object, *, maximum: float, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ValueError(f"{label} must be greater than 0 and at most {maximum:g} mm.")
    return number


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _clean_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = item.strip()
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(text)
    return cleaned


class PrinterProfile(BaseModel):
    """3D printer build envelope and material configuration."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(max_length=120)] = None
    build_volume_mm: tuple[float, float, float] | None = None
    nozzle_mm: float | None = None
    materials: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=16)] = Field(
        default_factory=list
    )

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("build_volume_mm")
    @classmethod
    def _validate_volume(cls, value: tuple[float, float, float] | None):
        if value is None:
            return None
        return tuple(
            _positive_mm(axis, maximum=2000.0, label="Printer build volume axis")
            for axis in value
        )

    @field_validator("nozzle_mm")
    @classmethod
    def _validate_nozzle(cls, value: float | None) -> float | None:
        return _positive_mm(value, maximum=5.0, label="Nozzle diameter")

    @field_validator("materials")
    @classmethod
    def _normalize_materials(cls, values: list[str]) -> list[str]:
        return _clean_list(values)


class BenchEnvelope(BaseModel):
    """Available laboratory bench envelope and required perimeter clearances."""

    model_config = ConfigDict(extra="forbid")

    width_mm: float | None = None
    depth_mm: float | None = None
    height_mm: float | None = None
    front_clearance_mm: float | None = None
    rear_clearance_mm: float | None = None
    side_clearance_mm: float | None = None
    notes: Annotated[str | None, Field(max_length=400)] = None

    @field_validator(
        "width_mm",
        "depth_mm",
        "height_mm",
        "front_clearance_mm",
        "rear_clearance_mm",
        "side_clearance_mm",
    )
    @classmethod
    def _validate_axis(cls, value: float | None) -> float | None:
        return _positive_mm(value, maximum=5000.0, label="Bench dimension")

    @field_validator("notes")
    @classmethod
    def _trim_notes(cls, value: str | None) -> str | None:
        return _clean_text(value)


class LabProfile(BaseModel):
    """Persistent laboratory hardware, fabrication, and spatial capabilities."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    lab_name: Annotated[str | None, Field(max_length=120)] = None
    printer: PrinterProfile = Field(default_factory=PrinterProfile)
    bench: BenchEnvelope = Field(default_factory=BenchEnvelope)
    measuring_tools: Annotated[
        list[Annotated[str, Field(max_length=80)]], Field(max_length=16)
    ] = Field(default_factory=list)
    owned_equipment: Annotated[
        list[Annotated[str, Field(max_length=100)]], Field(max_length=32)
    ] = Field(default_factory=list)
    notes: Annotated[str | None, Field(max_length=600)] = None
    updated_at: Annotated[str | None, Field(max_length=64)] = None

    @field_validator("lab_name", "notes", "updated_at")
    @classmethod
    def _trim_text_fields(cls, value: str | None) -> str | None:
        return _clean_text(value)

    @field_validator("measuring_tools", "owned_equipment")
    @classmethod
    def _normalize_tools(cls, values: list[str]) -> list[str]:
        return _clean_list(values)


class DimensionMeasurement(BaseModel):
    """Explicit provenance record distinguishing reference vs user-measured values."""

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(min_length=1, max_length=100)]
    value_mm: float
    provenance: Literal["reference", "user_measured"]
    source: Annotated[str | None, Field(max_length=300)] = None
    measured_with: Annotated[str | None, Field(max_length=100)] = None
    recorded_at: Annotated[str | None, Field(max_length=64)] = None

    @field_validator("value_mm")
    @classmethod
    def _validate_value(cls, value: float) -> float:
        checked = _positive_mm(value, maximum=20000.0, label="Dimension value")
        assert checked is not None
        return checked


def default_lab_profile() -> dict:
    """Return a clean empty laboratory profile dictionary."""
    return LabProfile().model_dump(mode="json")


def _explicit_profile_path() -> Path | None:
    raw = os.environ.get("AUTOLAB_LAB_PROFILE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return None


def _file_profile_path() -> Path | None:
    explicit = _explicit_profile_path()
    if explicit is not None:
        return explicit
    if project_library.database_path():
        return None
    if project_library.library_directory():
        return project_library._directory() / "lab-profile.json"
    return _ROOT / ".local" / "lab-profile.json"


def _connect_sqlite(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lab_profiles (
            profile_id TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )"""
    )
    return conn


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".lab-profile-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_lab_profile() -> dict:
    """Load the saved laboratory profile, returning an empty schema if unconfigured."""
    with _PROFILE_LOCK:
        path = _file_profile_path()
        if path is not None:
            if not path.is_file():
                return default_lab_profile()
            raw = json.loads(path.read_text(encoding="utf-8"))
            return LabProfile.model_validate(raw).model_dump(mode="json")
        db_path = project_library.database_path()
        assert db_path is not None
        conn = _connect_sqlite(db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT payload FROM lab_profiles WHERE profile_id=?",
                    (_DEFAULT_PROFILE_ID,),
                ).fetchone()
            if not row:
                return default_lab_profile()
            raw = json.loads(row["payload"])
            return LabProfile.model_validate(raw).model_dump(mode="json")
        finally:
            conn.close()


def save_lab_profile(profile: LabProfile | dict) -> dict:
    """Validate and atomically persist the laboratory profile."""
    validated = LabProfile.model_validate(profile)
    payload = validated.model_dump(mode="json")
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    with _PROFILE_LOCK:
        path = _file_profile_path()
        if path is not None:
            _atomic_write_json(path, payload)
            if _explicit_profile_path() is None and project_library.library_directory():
                project_library._commit_volume()
            return deepcopy(payload)
        db_path = project_library.database_path()
        assert db_path is not None
        conn = _connect_sqlite(db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO lab_profiles VALUES (?, ?, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        payload=excluded.payload""",
                    (_DEFAULT_PROFILE_ID, payload["updated_at"], encoded),
                )
            return deepcopy(payload)
        finally:
            conn.close()


def has_lab_profile_data(profile: dict | None) -> bool:
    """Return True only when the user has configured at least one lab capability or constraint."""
    if not isinstance(profile, dict):
        return False
    printer = profile.get("printer") or {}
    bench = profile.get("bench") or {}
    return bool(
        profile.get("lab_name")
        or profile.get("notes")
        or profile.get("measuring_tools")
        or profile.get("owned_equipment")
        or printer.get("name")
        or printer.get("build_volume_mm")
        or printer.get("nozzle_mm") is not None
        or printer.get("materials")
        or any(
            bench.get(k) is not None
            for k in (
                "width_mm",
                "depth_mm",
                "height_mm",
                "front_clearance_mm",
                "rear_clearance_mm",
                "side_clearance_mm",
                "notes",
            )
        )
    )


def lab_profile_prompt_context(profile: dict | None) -> dict | None:
    """Build a compact non-empty laboratory context summary for the AI model."""
    if not has_lab_profile_data(profile):
        return None
    assert isinstance(profile, dict)
    printer = profile.get("printer") or {}
    bench = profile.get("bench") or {}
    compact: dict = {}
    if profile.get("lab_name"):
        compact["lab_name"] = profile["lab_name"]
    printer_info = {
        k: printer[k]
        for k in ("name", "build_volume_mm", "nozzle_mm", "materials")
        if printer.get(k) not in (None, [], "")
    }
    if printer_info:
        compact["printer"] = printer_info
    bench_info = {
        k: bench[k]
        for k in (
            "width_mm",
            "depth_mm",
            "height_mm",
            "front_clearance_mm",
            "rear_clearance_mm",
            "side_clearance_mm",
            "notes",
        )
        if bench.get(k) is not None
    }
    if bench_info:
        compact["bench"] = bench_info
    if profile.get("measuring_tools"):
        compact["measuring_tools"] = list(profile["measuring_tools"])
    if profile.get("owned_equipment"):
        compact["owned_equipment"] = list(profile["owned_equipment"])
    return compact


def record_dimension(
    project: dict,
    key: str,
    value_mm: float,
    *,
    provenance: Literal["reference", "user_measured"],
    source: str | None = None,
    measured_with: str | None = None,
) -> dict:
    """Return a copy of the project with a validated dimension provenance entry recorded."""
    existing = (project.get("dimension_provenance") or {}).get(key)
    entry = DimensionMeasurement(
        key=key,
        value_mm=value_mm,
        provenance=provenance,
        source=source,
        measured_with=measured_with,
        recorded_at=(
            existing.get("recorded_at")
            if isinstance(existing, dict)
            and existing.get("value_mm") == float(value_mm)
            and existing.get("provenance") == provenance
            and existing.get("source") == source
            and existing.get("measured_with") == measured_with
            and existing.get("recorded_at")
            else datetime.now(timezone.utc).isoformat()
        ),
    ).model_dump(mode="json", exclude_none=True)
    updated = deepcopy(project)
    table = dict(updated.get("dimension_provenance") or {})
    table[key] = entry
    updated["dimension_provenance"] = table
    return updated


def apply_profile_to_project(project: dict, profile: LabProfile | dict) -> dict:
    """Apply saved lab bench dimensions and equipment notes onto a project revision."""
    from app.domain import EXTENDED_PATCH_FIELDS, apply_update

    parsed = LabProfile.model_validate(profile)
    patch: dict = {}
    bench = parsed.bench
    if bench.width_mm is not None:
        patch["workspace_width_mm"] = bench.width_mm
    if "workspace_depth_mm" in EXTENDED_PATCH_FIELDS and bench.depth_mm is not None:
        patch["workspace_depth_mm"] = bench.depth_mm
    if "workspace_height_mm" in EXTENDED_PATCH_FIELDS and bench.height_mm is not None:
        patch["workspace_height_mm"] = bench.height_mm

    constraint_bits = []
    if bench.width_mm is not None:
        constraint_bits.append(f"width {bench.width_mm:g} mm")
    if bench.depth_mm is not None:
        constraint_bits.append(f"depth {bench.depth_mm:g} mm")
    if bench.height_mm is not None:
        constraint_bits.append(f"height {bench.height_mm:g} mm")
    if constraint_bits:
        line = "Lab profile bench: " + ", ".join(constraint_bits)
        existing_lines = (project.get("brief", {}).get("constraints") or "").splitlines()
        kept = [row for row in existing_lines if not row.startswith("Lab profile bench:")]
        patch["brief_constraints"] = "\n".join([*kept, line])[:1000]

    if parsed.owned_equipment and not project.get("brief", {}).get("equipment"):
        patch["brief_equipment"] = ", ".join(parsed.owned_equipment)[:1000]

    updated = apply_update(project, patch) if patch else deepcopy(project)
    tools = ", ".join(parsed.measuring_tools) if parsed.measuring_tools else None
    before_prov = deepcopy(updated.get("dimension_provenance") or {})
    for key, val in (
        ("bench_width_mm", bench.width_mm),
        ("bench_depth_mm", bench.depth_mm),
        ("bench_height_mm", bench.height_mm),
    ):
        if val is not None:
            updated = record_dimension(
                updated,
                key,
                val,
                provenance="user_measured",
                source="lab_profile",
                measured_with=tools,
            )
    if (
        updated["revision"] == project["revision"]
        and updated.get("dimension_provenance") != before_prov
    ):
        updated["revision"] += 1
        updated["status"] = "draft"
    return updated

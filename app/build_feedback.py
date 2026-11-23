"""Physical build feedback loop: logs, E13 metrics, caliper deltas & reprint diffs (Phase 2.3)."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite.")
    return number


class MeasuredFeature(BaseModel):
    """Single caliper or optical feature measurement against design nominal."""

    model_config = ConfigDict(extra="forbid")

    feature: str = Field(min_length=1, max_length=80)
    axis: Literal["length", "width", "diameter", "height", "bore", "other"] = "length"
    nominal_mm: float = Field(gt=0.0, le=1000.0)
    measured_mm: float = Field(gt=0.0, le=1000.0)
    error_mm: float | None = None

    @field_validator("nominal_mm", "measured_mm", mode="before")
    @classmethod
    def _validate_finite(cls, value: Any, info) -> float:
        return _finite_number(value, info.field_name)

    @model_validator(mode="after")
    def _compute_error(self) -> MeasuredFeature:
        self.feature = self.feature.strip()
        self.error_mm = round(self.measured_mm - self.nominal_mm, 3)
        return self


class PlacementTrialEntry(BaseModel):
    """Single physical insertion/removal repeatability trial for E13."""

    model_config = ConfigDict(extra="forbid")

    trial: int = Field(ge=1, le=50)
    seated_ok: bool
    orientation_match: bool
    displacement_mm: float | None = Field(default=None, ge=0.0, le=100.0)
    notes: str = Field(default="", max_length=200)

    @field_validator("displacement_mm", mode="before")
    @classmethod
    def _validate_disp(cls, value: Any) -> float | None:
        if value is None:
            return None
        return _finite_number(value, "displacement_mm")


class PhotoAttachmentRef(BaseModel):
    """Non-executable reference metadata for a physical inspection photograph."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    uri: str = Field(min_length=1, max_length=500)


class BuildLogCreate(BaseModel):
    """Input payload recording one physical print and bench check."""

    model_config = ConfigDict(extra="forbid")

    parts_printed: list[str] = Field(min_length=1, max_length=12)
    printer_name: str = Field(default="Unspecified printer", min_length=1, max_length=100)
    material: str = Field(default="PLA", min_length=1, max_length=40)
    nozzle_mm: float = Field(default=0.4, ge=0.1, le=2.0)
    layer_height_mm: float = Field(default=0.2, ge=0.05, le=1.0)
    fit_result: Literal["pass", "too_tight", "too_loose", "warped", "failed_print"]
    measured_features: list[MeasuredFeature] = Field(default_factory=list, max_length=20)
    placement_trials: list[PlacementTrialEntry] = Field(default_factory=list, max_length=50)
    photos: list[PhotoAttachmentRef] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=1000)

    @field_validator("parts_printed")
    @classmethod
    def _clean_parts(cls, value: list[str]) -> list[str]:
        cleaned = [p.strip() for p in value if isinstance(p, str) and p.strip()]
        if not cleaned:
            raise ValueError("Specify at least one printed part ID.")
        return list(dict.fromkeys(cleaned))


class BuildLogEntry(BuildLogCreate):
    """Stored build log record bound to exact project revision and part signatures."""

    id: str = Field(min_length=1, max_length=40)
    revision: int = Field(ge=0)
    recorded_at: str | None = Field(default=None, max_length=64)
    part_signatures: dict[str, str] = Field(default_factory=dict)
    e13_summary: dict[str, Any] | None = None
    applied_revision: int | None = None


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _active_parts_info(project: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({part_id: signature}, {part_id: label}) for active printable components."""
    if fixture := project.get("standalone_fixture"):
        sigs = {k: str(v) for k, v in (fixture.get("part_signatures") or {}).items()}
        labels = {p["id"]: p.get("label") or p["id"] for p in fixture.get("parts") or []}
        if sigs:
            return sigs, labels
    if rack := project.get("labware_rack"):
        sig = _hash_payload({"spec": rack.get("spec"), "size_mm": rack.get("size_mm")})
        return (
            {"tube-rack": sig, "tube-rack-fit-coupon": sig},
            {
                "tube-rack": "Custom rim-supported tube rack",
                "tube-rack-fit-coupon": "Tube rack fit coupon",
            },
        )
    if plate := project.get("labware_plate"):
        sig = _hash_payload(
            {"profile_id": plate.get("profile_id"), "size_mm": plate.get("size_mm")}
        )
        return {"plate-holder": sig}, {"plate-holder": "Microplate holder"}
    return {}, {}


def _compute_e13_summary(trials: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not trials:
        return None
    count = len(trials)
    seated_count = sum(1 for t in trials if t.get("seated_ok"))
    orient_ok = all(bool(t.get("orientation_match")) for t in trials)
    displacements = [
        float(t["displacement_mm"]) for t in trials if t.get("displacement_mm") is not None
    ]
    max_disp = round(max(displacements), 3) if displacements else None
    passes = bool(
        count >= 10
        and seated_count == count
        and orient_ok
        and max_disp is not None
        and max_disp <= 1.0 + 1e-6
    )
    return {
        "trial_count": count,
        "seated_pass_count": seated_count,
        "all_orientations_correct": orient_ok,
        "max_displacement_mm": max_disp,
        "target_threshold_mm": 1.0,
        "passes_1mm_target": passes,
    }


def compute_reprint_diff(project: dict) -> dict[str, Any]:
    """Compare current part signatures against the latest physical build log."""
    active_sigs, labels = _active_parts_info(project)
    logs = list(project.get("build_logs") or [])
    current_rev = int(project.get("revision", 0))
    if not logs:
        return {
            "tested_revision": None,
            "current_revision": current_rev,
            "latest_log_id": None,
            "latest_fit_result": None,
            "parts_to_reprint": [],
            "parts_unchanged": [],
            "has_untested_changes": bool(active_sigs),
        }

    latest = logs[-1]
    tested_rev = int(latest["revision"])
    tested_sigs: dict[str, str] = latest.get("part_signatures") or {}
    printed_ever: set[str] = set()
    for item in logs:
        printed_ever.update(item.get("parts_printed") or [])

    to_reprint: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for pid, sig in active_sigs.items():
        old_sig = tested_sigs.get(pid)
        entry = {
            "id": pid,
            "label": labels.get(pid, pid),
            "previously_printed": pid in printed_ever,
        }
        if old_sig is None:
            to_reprint.append(
                {**entry, "reason": "New part added since the last tested print."}
            )
        elif sig != old_sig:
            to_reprint.append(
                {
                    **entry,
                    "reason": (
                        f"Geometry updated since tested rev {tested_rev} — reprint required."
                    ),
                }
            )
        else:
            unchanged.append(
                {
                    **entry,
                    "reason": (
                        f"Geometry unchanged since tested rev {tested_rev} — "
                        "reuse existing print."
                    ),
                }
            )

    return {
        "tested_revision": tested_rev,
        "current_revision": current_rev,
        "latest_log_id": latest["id"],
        "latest_fit_result": latest["fit_result"],
        "parts_to_reprint": to_reprint,
        "parts_unchanged": unchanged,
        "has_untested_changes": bool(to_reprint) or current_rev != tested_rev,
    }


def append_build_log(
    project: dict,
    entry: BuildLogCreate | dict[str, Any],
    *,
    recorded_at: str | None = None,
) -> tuple[dict, dict[str, Any]]:
    """Validate and record a physical print/measurement log onto a project copy."""
    active_sigs, _labels = _active_parts_info(project)
    if not active_sigs:
        raise ValueError(
            "Create a printable fixture or labware design before logging a physical print."
        )

    parsed = (
        entry
        if isinstance(entry, BuildLogCreate)
        else BuildLogCreate.model_validate(entry)
    )
    updated = deepcopy(project)
    existing = list(updated.get("build_logs") or [])
    rev = int(updated.get("revision", 0))
    log_id = f"log-{len(existing) + 1:02d}-r{rev}"
    trials = [t.model_dump(mode="json") for t in parsed.placement_trials]
    full = BuildLogEntry(
        **parsed.model_dump(),
        id=log_id,
        revision=rev,
        recorded_at=recorded_at,
        part_signatures=active_sigs,
        e13_summary=_compute_e13_summary(trials),
    ).model_dump(mode="json", exclude_none=True)
    existing.append(full)
    updated["build_logs"] = existing
    updated["build_feedback"] = compute_reprint_diff(updated)
    return updated, full


def _derive_deltas_from_log(log: dict[str, Any]) -> tuple[float, float, str]:
    """Return (fdm_undersize_delta_mm, clearance_delta_per_side_mm, rationale)."""
    features = log.get("measured_features") or []
    fit_errors = [
        float(f["error_mm"])
        for f in features
        if f.get("axis") in ("length", "width", "diameter", "bore")
        and f.get("error_mm") is not None
    ]
    if fit_errors:
        min_err = min(fit_errors)
        max_err = max(fit_errors)
        if min_err < -0.02:
            # Printed opening measured smaller than CAD nominal -> add undersize compensation.
            comp = round(abs(min_err), 3)
            return comp, 0.0, (
                f"Caliper opening measured {abs(min_err):.2f} mm below CAD nominal; "
                f"increasing FDM undersize compensation by +{comp:.2f} mm."
            )
        if max_err > 0.08 and log.get("fit_result") == "too_loose":
            comp = -round(min(max_err, 0.5), 3)
            return comp, 0.0, (
                f"Caliper opening measured {max_err:.2f} mm above CAD nominal; "
                f"reducing FDM undersize allowance by {comp:.2f} mm."
            )

    fit_result = log.get("fit_result")
    if fit_result == "too_tight":
        return 0.0, 0.15, (
            "Reported too-tight fit without shrinkage error value; "
            "widening clearance by +0.15 mm per side (+0.30 mm total)."
        )
    if fit_result == "too_loose":
        return 0.0, -0.10, (
            "Reported too-loose fit; tightening clearance by -0.10 mm per side."
        )
    raise ValueError(
        "Record caliper measured_features with an offset or fit_result 'too_tight'/'too_loose' "
        "before applying a clearance revision."
    )


def apply_build_log_revision(
    project: dict,
    log_id: str,
    *,
    clearance_delta_mm: float | None = None,
    fdm_undersize_delta_mm: float | None = None,
    lab_profile: dict | None = None,
) -> tuple[dict, dict[str, Any]]:
    """Revise fixture/rack tolerances from a recorded build log and highlight changed parts."""
    logs = deepcopy(project.get("build_logs") or [])
    target = next((item for item in logs if item.get("id") == log_id), None)
    if target is None:
        raise ValueError(f"Unknown build log '{log_id}'.")

    if clearance_delta_mm is not None or fdm_undersize_delta_mm is not None:
        d_undersize = float(fdm_undersize_delta_mm or 0.0)
        d_clearance = float(clearance_delta_mm or 0.0)
        rationale = "Applied explicit build-log clearance/undersize override."
    else:
        d_undersize, d_clearance, rationale = _derive_deltas_from_log(target)

    if fixture := project.get("standalone_fixture"):
        from app.fixture_adapter import attach_standalone_fixture
        from app.lab_profile import record_dimension

        spec = deepcopy(fixture["spec"])
        old_u = float(spec["fdm_undersize_mm"])
        old_c = float(spec["clearance_per_side_mm"])
        raw_u = old_u + d_undersize
        new_u = round(min(1.5, max(0.0, raw_u)), 3)
        spill = raw_u - new_u
        new_c = round(min(3.0, max(0.05, old_c + d_clearance + spill / 2.0)), 3)
        if abs(new_u - old_u) < 1e-6 and abs(new_c - old_c) < 1e-6:
            raise ValueError("Derived clearance parameters hit limits without changing geometry.")
        spec["fdm_undersize_mm"] = new_u
        spec["clearance_per_side_mm"] = new_c

        staged = deepcopy(project)
        updated = attach_standalone_fixture(staged, spec, lab_profile=lab_profile)
        updated = record_dimension(
            updated,
            "fixture_fdm_undersize_mm",
            new_u,
            provenance="user_measured",
            source=log_id,
            measured_with="calipers",
        )
        updated["design_workflow"]  # preserved by attach_standalone_fixture
    elif rack := project.get("labware_rack"):
        from app.labware import apply_labware_spec
        from app.labware_geometry import RADIAL_CLEARANCE_MM

        spec = deepcopy(rack["spec"])
        old_c = float(spec.get("radial_clearance_mm", RADIAL_CLEARANCE_MM))
        new_c = round(min(5.0, max(0.4, old_c + d_clearance + d_undersize / 2.0)), 3)
        if abs(new_c - old_c) < 1e-6:
            raise ValueError("Rack radial clearance is already at boundary.")
        spec["radial_clearance_mm"] = new_c
        updated = apply_labware_spec(deepcopy(project), spec)
    else:
        raise ValueError(
            "Build-log tolerance revision requires an active standalone fixture or parametric rack."
        )

    for item in logs:
        if item.get("id") == log_id:
            item["applied_revision"] = int(updated["revision"])
    updated["build_logs"] = logs
    diff = compute_reprint_diff(updated)
    updated["build_feedback"] = diff

    summary = {
        "log_id": log_id,
        "previous_revision": int(project.get("revision", 0)),
        "new_revision": int(updated["revision"]),
        "rationale": rationale,
        "parts_to_reprint": [p["id"] for p in diff["parts_to_reprint"]],
        "parts_unchanged": [p["id"] for p in diff["parts_unchanged"]],
        "build_feedback": diff,
    }
    return updated, summary

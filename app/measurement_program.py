"""Attach replay-only instrument steps to stable positions in a project's native sequence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.measurement_adapters import ADAPTERS, Input, Text
from app.measurement_replay import digest

Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
SUPPORTED_HOSTS = {"bench_station", "concept_design"}


class MeasurementStep(Input):
    key: Key
    label: Text
    after_operation: Annotated[str, Field(pattern=r"^op-[0-9]{3,}$")]
    component_id: Annotated[str, Field(min_length=1, max_length=360)]
    adapter_id: Key
    command: Key


class MeasurementSteps(Input):
    steps: list[MeasurementStep] = Field(max_length=16)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({s.key for s in self.steps}) != len(self.steps):
            raise ValueError("Give each measurement step a unique key.")
        return self


class PinnedStep(MeasurementStep):
    adapter_version: Annotated[int, Field(ge=1)] | None


class SavedMeasurements(MeasurementSteps):
    schema_version: Literal[1] = 1
    steps: list[PinnedStep] = Field(min_length=1, max_length=16)
    base_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def base_signature(plan):
    return digest(
        {
            key: plan[key]
            for key in ("project_id", "platform", "machine_signature", "bindings", "operations")
        }
    )


def native_plan(project):
    from app.operation_plan import project_operation_plan

    return project_operation_plan({**project, "measurement_program": None})


def configure(project, request: MeasurementSteps):
    if not request.steps:
        if "measurement_program" not in project:
            return project
        return {
            **{k: v for k, v in project.items() if k != "measurement_program"},
            "revision": project["revision"] + 1,
        }
    from app.operation_plan import selected_platform
    from app.project_assembly import component_instance_ids

    if selected_platform(project) not in SUPPORTED_HOSTS:
        raise ValueError(
            "Choose an inspection station or custom assembly for a recorded measurement."
        )
    base = native_plan(project)
    if base["scope"] != "approved":
        raise ValueError("Approve the current design before binding measurement steps.")
    anchors = {r["id"] for r in base["operations"]}
    parts = set(component_instance_ids(project).values())
    for step in request.steps:
        if step.after_operation not in anchors:
            raise ValueError(f"Choose a native operation from this revision for {step.key}.")
        if step.component_id not in parts:
            raise ValueError(f"Choose an existing assembly component for {step.key}.")
    pinned = [
        PinnedStep(
            **s.model_dump(),
            adapter_version=ADAPTERS[s.adapter_id].version if s.adapter_id in ADAPTERS else None,
        )
        for s in request.steps
    ]
    saved = SavedMeasurements(steps=pinned, base_sha256=base_signature(base)).model_dump()
    if project.get("measurement_program") == saved:
        return project
    return {**project, "revision": project["revision"] + 1, "measurement_program": saved}


def attach_measurements(project, plan):
    saved = project["measurement_program"]
    stale = saved["base_sha256"] != base_signature(plan) or plan["scope"] != "approved"
    anchors = {r["id"] for r in plan["operations"]}
    from app.project_assembly import component_instance_ids

    ids = component_instance_ids(project)
    modules = {
        ids.get(m["id"]): m["id"] for m in (project.get("bench_station") or {}).get("modules", [])
    }
    additions = {}
    for step in saved["steps"]:
        adapter = ADAPTERS.get(step["adapter_id"])
        command = adapter.commands.get(step["command"]) if adapter else None
        needs = []
        if stale:
            needs.append("Review the component and native operation against the current design.")
        if not adapter:
            needs.append(
                "Register an adapter for the named device and document its input and calibration."
            )
        else:
            if adapter.version != step["adapter_version"]:
                needs.append("Review the installed adapter version and bind this step again.")
            if (
                adapter.station_module
                and step["component_id"] in modules
                and modules[step["component_id"]] != adapter.station_module
            ):
                needs.append(
                    f"Choose the station's {adapter.station_module} component for this adapter."
                )
            if not command:
                needs.extend(adapter.integration_requirements)
        key = "measurement/" + step["key"]
        plan["bindings"][key] = {
            "component_id": step["component_id"],
            "capability": step["command"],
            "driver": None if needs else step["adapter_id"] + "." + step["command"],
            "unit": command.unit if command else None,
            "limits": {},
            "prerequisites": [command.calibration] if command else [],
            "adapter": {
                "id": step["adapter_id"],
                "version": step["adapter_version"],
                "command": step["command"],
                "execution": "recorded_input_only",
                "physical_execution": False,
                "supported_commands": sorted(adapter.commands) if adapter else [],
                "missing_capability": step["command"] if not command else None,
                "integration_requirements": needs,
            },
        }
        new_row = {
            "binding": key,
            "action": step["label"],
            "parameters": {},
            "request_id": step["key"],
            "mode": "missing_driver" if needs else "recorded_input",
        }
        anchor = step["after_operation"] if step["after_operation"] in anchors else None
        additions.setdefault(anchor, []).append(new_row)
    ordered = []
    for row in plan["operations"]:
        ordered.append({**row, "base_operation_id": row["id"]})
        ordered.extend(additions.get(row["id"], []))
    ordered.extend(additions.get(None, []))
    plan["operations"] = [{**r, "id": f"op-{i:03d}"} for i, r in enumerate(ordered, 1)]
    plan["hardware_blockers"].append("This sequence includes recorded instrument inputs.")

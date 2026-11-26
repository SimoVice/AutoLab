"""Replay a saved operation plan against a revision-bound recording, without opening devices."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from app.instrument_drivers import InstrumentDriverError
from app.measurement_adapters import ADAPTERS, Input, Text

MAX_RECORDING_BYTES = 8 * 1024 * 1024


class Recording(Input):
    schema_version: Literal[1]
    project_id: Text
    revision: Annotated[int, Field(ge=0)]
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    item_id: Text
    inputs: dict[str, dict]


class ManualInput(Input):
    note: Text


class ManualOutcome(ManualInput):
    outcome: Text


class StaleRecording(ValueError):
    pass


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def _resolve(binding, mode):
    declared = binding.get("adapter")
    if declared:
        adapter = ADAPTERS.get(declared["id"])
        if mode == "recorded_input" and adapter and adapter.version == declared["version"]:
            return adapter.commands.get(declared["command"])
    elif mode == "driver" and binding.get("driver"):
        return next(
            (
                cmd
                for adapter in ADAPTERS.values()
                for cmd in adapter.commands.values()
                if cmd.native_driver == binding["driver"]
            ),
            None,
        )
    return None


def recording_template(plan):
    inputs = {}
    for row in plan["operations"]:
        binding = plan["bindings"][row["binding"]]
        command = _resolve(binding, row["mode"])
        fields = command.template if command else {}
        if row["mode"] == "manual":
            fields = {"note": ""}
            if binding["limits"].get("outcomes"):
                fields["outcome"] = ""
        inputs[row["id"]] = deepcopy(fields)
    return {
        "schema_version": 1,
        "project_id": plan["project_id"],
        "revision": plan["revision"],
        "plan_sha256": plan["sha256"],
        "item_id": "",
        "inputs": inputs,
    }


def replay(plan, recording: Recording):
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("sha256") != digest({k: v for k, v in plan.items() if k != "sha256"})
    ):
        raise ValueError("The operation plan must match its issued digest.")
    if (recording.project_id, recording.revision, recording.plan_sha256) != (
        plan["project_id"],
        plan["revision"],
        plan["sha256"],
    ):
        raise StaleRecording(
            "Use a recording prepared for this project, revision and operation plan."
        )
    result = {
        "project_id": plan["project_id"],
        "revision": plan["revision"],
        "plan_sha256": plan["sha256"],
        "item_id": recording.item_id,
        "physical_execution": False,
        "events": [],
        "issues": [],
    }
    if plan["scope"] != "approved" or not plan["operations"]:
        return {
            **result,
            "status": "blocked",
            "issues": [{"reason": "Approve a design with bound operations."}],
        }
    inputs = []
    for row in plan["operations"]:
        binding = plan["bindings"][row["binding"]]
        command = _resolve(binding, row["mode"])
        if row["mode"] != "manual" and command is None:
            ref = binding.get("adapter") or {}
            result["issues"].append(
                {
                    "operation_id": row["id"],
                    "component_id": binding["component_id"],
                    "capability": ref.get("command", binding["capability"]),
                    "missing_capability": ref.get("missing_capability", binding["capability"]),
                    "reason": "This operation has no available recording adapter.",
                    "integration_requirements": ref.get("integration_requirements")
                    or ["Provide a recorded-input implementation for the requested command."],
                }
            )
        inputs.append((row, binding, command))
    if result["issues"]:
        return {**result, "status": "blocked"}
    if set(recording.inputs) != {r["id"] for r in plan["operations"]}:
        raise ValueError("Supply one input for every operation ID in this plan, with no extra IDs.")
    for row, binding, command in inputs:
        try:
            raw = recording.inputs[row["id"]]
            if command:
                observation = command.read(command.inputs.model_validate(raw), recording.item_id)
                origin = "recorded_input"
            else:
                outcomes = binding["limits"].get("outcomes")
                checkpoint = ManualOutcome if outcomes else ManualInput
                observation = checkpoint.model_validate(raw).model_dump()
                if outcomes and observation["outcome"] not in outcomes:
                    raise ValueError(f"Choose an operator outcome from {', '.join(outcomes)}.")
                origin = "recorded_operator_note"
        except ValidationError as exc:
            first = exc.errors(include_input=False, include_url=False)[0]
            field = ".".join(str(x) for x in first["loc"])
            raise ValueError(f"{row['id']} {field}: {first['msg']}") from None
        except (ValueError, InstrumentDriverError) as exc:
            raise ValueError(f"{row['id']}: {exc}") from None
        result["events"].append(
            {
                "operation_id": row["id"],
                "action": row["action"],
                "component_id": binding["component_id"],
                "capability": binding["capability"],
                "unit": command.unit if command else None,
                "provenance": origin,
                **observation,
            }
        )
        if origin == "recorded_operator_note" and observation.get("outcome") == "fail":
            result["issues"].append(
                {"operation_id": row["id"], "reason": "The operator recorded a failed check."}
            )
            return {**result, "status": "stopped"}
    return {**result, "status": "replayed"}


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    try:
        if args.recording.stat().st_size > MAX_RECORDING_BYTES:
            raise ValueError("The recording exceeds 8 MiB.")
        plan = json.loads(args.plan.read_bytes())
        result = replay(plan, Recording.model_validate_json(args.recording.read_bytes()))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "replayed" else 3


if __name__ == "__main__":
    raise SystemExit(main())

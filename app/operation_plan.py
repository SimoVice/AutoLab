"""Revision-bound operations for protocol previews, offline checks and device adapters."""

from __future__ import annotations

import hashlib
import json

from app.design_routes import needs_review
from app.design_workflow import workflow_state
from app.project_assembly import component_instance_ids


def selected_platform(project):
    design = project.get("design") or {}
    if project.get("bench_station"):
        return "bench_station"
    if project.get("concept_design"):
        return "concept_design"
    if project.get("standalone_fixture"):
        return "standalone_fixture"
    if project.get("upstream_project"):
        return "upstream_project"
    if design.get("foundation"):
        return "jubilee"
    return (design.get("platform") or {}).get("id")


def resolve_platform(project, requested="auto"):
    if requested != "auto":
        return requested
    selected = selected_platform(project)
    if selected:
        return selected
    summary = project.get("summary") or {}
    if (project.get("workflow") or {}).get("kind") == "liquid_preparation" and all(
        summary.get(k) for k in ("stock_concentration", "target_concentrations", "final_volume")
    ):
        # Older recipe-only projects can still preview the OT-2 example offline.
        return "opentrons-ot2"
    return "unselected"


def requested_operations(project):
    plan = project.get("design_plan") or {}
    if not plan:
        return []
    decision = plan.get("decision") or {}
    applied = decision.get("applied_revision") is not None and not needs_review(project)
    option = next((o for o in plan["options"] if applied and o["key"] == decision["key"]), {})
    mapping = {c["operation"]: c["capability"] for c in option.get("coverage", [])}
    return [{**op, "capability": mapping.get(op["key"])} for op in plan["operations"]]


def device(component, capability, driver, unit, limits, prerequisites):
    return {
        "component_id": component,
        "capability": capability,
        "driver": driver,
        "unit": unit,
        "limits": limits,
        "prerequisites": prerequisites,
    }


def operation(binding, action, parameters=None, *, mode="driver", request_id=None):
    return {
        "binding": binding,
        "action": action,
        "parameters": parameters or {},
        "mode": mode,
        "request_id": request_id,
    }


def _unimplemented(request, bindings):
    key = request["key"]
    manual = request["capability"] == "manual"
    binding = "request/" + key
    bindings[binding] = device(
        None,
        request["capability"] or "unassigned",
        None,
        None,
        {"required_performance": request.get("performance")},
        [
            request.get("interface")
            or ("Operator confirmation" if manual else "Bind a device and driver")
        ],
    )
    return operation(
        binding, request["action"], mode="manual" if manual else "missing_driver", request_id=key
    )


def make_plan(
    project, platform, bindings, operations, *, matches_inputs=True, requests=(), blockers=()
):
    approved = workflow_state(project)["machine_approved"]
    design = project.get("design_plan") or {}
    current = not design or (
        (design.get("decision") or {}).get("applied_revision") is not None
        and not needs_review(project)
    )
    bound = approved and current and selected_platform(project) == platform and matches_inputs
    rows = [{"id": f"op-{i:03d}", **op} for i, op in enumerate(operations, 1)]
    result = {
        "schema_version": 1,
        "project_id": project.get("lineage_id") or project["id"],
        "revision": project["revision"],
        "machine_signature": (project.get("machine_approval") or {}).get("machine_signature")
        if bound
        else None,
        "platform": platform,
        "scope": "approved" if bound else "illustrative",
        "approval_revision": (project.get("machine_approval") or {}).get("revision")
        if bound
        else None,
        "bindings": bindings,
        "operations": rows,
        "requests": list(requests),
        "execution_enabled": False,
        "hardware_blockers": list(blockers),
    }
    if project.get("measurement_program") and platform == selected_platform(project):
        from app.measurement_program import attach_measurements

        attach_measurements(project, result)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {**result, "sha256": hashlib.sha256(encoded).hexdigest()}


def liquid_plan(project, platform, recipe, bindings, operations, *, blockers=()):
    # Changing a recipe or choosing a different platform for comparison produces a preview.
    from app.protocol_generators import extract_or_build_protocol_plan

    try:
        saved = extract_or_build_protocol_plan(project, min_transfer_ul=recipe["min_transfer_ul"])
        matches = (saved["stock"], saved["target_steps"], saved["transfers"]) == (
            recipe["stock"],
            recipe["target_steps"],
            recipe["transfers"],
        )
    except ValueError:
        matches = False
    blockers = list(blockers)
    destination = (project.get("labware_plate") or {}).get("profile_id")
    if not destination:
        blockers.append("Verify the driver destination against the installed labware.")
    for binding in bindings.values():
        if not binding["component_id"]:
            blockers.append("Select and bind the pipette in the physical assembly.")
        if destination and binding["limits"].get("destination_labware") != destination:
            blockers.append("The driver destination differs from the selected labware.")
    if (project.get("labware_plate") or {}).get("compatibility", {}).get("blocking_issues"):
        blockers.append("Resolve the selected labware capacity or geometry conflict.")
    requests = requested_operations(project)
    # A platform selection covers the recipe once. Ancillary requests still require explicit
    # coverage and cannot silently acquire pipette commands.
    native_bindings = list(bindings)
    rows = []
    if requests:
        emitted = False
        for req in requests:
            if req["capability"] == platform:
                if not emitted:
                    rows.extend(operations)
                    emitted = True
                req["binding_keys"] = native_bindings
            else:
                rows.append(_unimplemented(req, bindings))
        if not emitted:
            matches = False
    else:
        rows = operations
    return make_plan(
        project,
        platform,
        bindings,
        rows,
        matches_inputs=matches,
        requests=requests,
        blockers=dict.fromkeys(blockers),
    )


BENCH_CAPABILITIES = {
    "read_barcode": (
        "BarcodeScannerDriver.scan",
        "identifier",
        {"expected": "item identifier"},
        "Verify scanner framing and identifier match.",
    ),
    "weigh": (
        "SerialBalanceDriver.read_weight",
        "g",
        {"minimum": 0, "maximum": None},
        "Tare the balance and verify capacity, calibration and a stable reading.",
    ),
    "print_label": (
        "LabelPrinterDriver.spool",
        "label",
        {"maximum_characters": 80},
        "Verify the label format and manually confirm printing and application.",
    ),
    "inspect": (
        None,
        "pass/fail",
        {"outcomes": ["pass", "fail"]},
        "An operator must review the visible feature against stated criteria.",
    ),
}


def bench_operation_plan(project):
    station = project.get("bench_station") or {}
    modules = {m["id"]: m for m in station.get("modules", [])}
    ids = component_instance_ids(project)
    requests = requested_operations(project)
    rows, bindings = [], {}
    sequence = requests or [
        {"key": op, "action": m["label"], "capability": op} for op, m in modules.items()
    ]
    for req in sequence:
        key = req["capability"]
        if key not in modules or key not in BENCH_CAPABILITIES:
            rows.append(_unimplemented(req, bindings))
            continue
        driver, unit, limits, prerequisite = BENCH_CAPABILITIES[key]
        bindings[key] = device(ids[key], key, driver, unit, limits, [prerequisite])
        rows.append(
            operation(
                key, req["action"], mode="driver" if driver else "manual", request_id=req["key"]
            )
        )
    return make_plan(
        project, selected_platform(project) or "unselected", bindings, rows, requests=requests
    )


def custom_operation_plan(project):
    design = project["concept_design"]
    ids = component_instance_ids(project)
    requests = requested_operations(project)
    bindings = {}
    rows = [_unimplemented(req, bindings) for req in requests]
    for index, step in enumerate(design["steps"]):
        key = f"step/{index + 1}"
        moving = step.get("moving_key")
        owner = moving or next(iter(step.get("component_keys", [])), None)
        rotation = step.get("rotation_deg")
        bindings[key] = device(
            ids.get(owner),
            "motion" if moving else "operator_review",
            None,
            ("deg" if rotation else "mm") if moving else None,
            {"preview_translation_mm": step.get("translation_mm"), "preview_rotation_deg": rotation}
            if moving
            else {},
            ["Commission the device mapping for this step." if moving else step["instruction"]],
        )
        rows.append(
            operation(
                key,
                step["name"],
                {"step_index": index},
                mode="missing_driver" if moving else "manual",
            )
        )
    return make_plan(project, "concept_design", bindings, rows, requests=requests)


def simulate_sequence(project):
    sequence = project_operation_plan(project)
    station = project.get("bench_station")
    if project.get("concept_design"):
        from app.concept_controller import run_sequence

        result = run_sequence(
            {
                "project_id": sequence["project_id"],
                "revision": sequence["revision"],
                "design": project["concept_design"],
                "operation_plan": sequence,
            }
        )
    elif station:
        from app.bench_station_runner import dry_run

        result = dry_run(
            {
                **station,
                "project_id": sequence["project_id"],
                "revision": project["revision"],
                "operation_plan": sequence,
            }
        )
    else:
        result = {"status": "not_executed", "events": [], "pending_human_checkpoints": []}
    return {
        **result,
        "ok": bool(sequence["operations"])
        and all(r["mode"] != "missing_driver" for r in sequence["operations"]),
        "verifier": "operator_sequence_dry_run",
        "plan_sha256": sequence["sha256"],
        "steps_executed": 0,
        "operation_ids": [r["id"] for r in sequence["operations"]],
        "missing_drivers": [
            r["id"] for r in sequence["operations"] if r["mode"] == "missing_driver"
        ],
    }


def project_operation_plan(project):
    platform = resolve_platform(project)
    if platform in ("jubilee", "opentrons-ot2"):
        from app.protocol_generators import generate_jubilee_protocol, generate_opentrons_protocol

        generator = (
            generate_jubilee_protocol if platform == "jubilee" else generate_opentrons_protocol
        )
        return generator(project)["operation_plan"]
    if platform == "concept_design":
        return custom_operation_plan(project)
    return bench_operation_plan(project)

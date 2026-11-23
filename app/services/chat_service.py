"""Chat turn orchestration service extracted from the API entrypoint."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.chat_actions import execute_chat_actions, extension_only_patch
from app.chat_activity import report_activity
from app.conversation_history import DISPLAY_HISTORY_LIMIT, MODEL_HISTORY_LIMIT
from app.design_sources import resolve_reference
from app.domain import apply_update
from app.engineering import EngineeringUpdate, apply_engineering_update, state_for
from app.labware import reapply_labware, update_labware_brief
from app.microplates import labware_chat_reply, reapply_plate
from app.model_client import ChatAction, ModelClientError, generate_turn
from app.procurement import project_with_report
from app.services.runtime import api_hook
from app.services.session_store import (
    ProjectSession,
    accept_project,
    check_revision,
    cooling_feedback,
    session_for,
)
from app.tip_racks import TipRackUpdate, declared_printer, reapply_tip_rack, update_tip_request
from app.workflows import (
    describe_workflow,
    has_workstation_design_context,
    workstation_context_question,
)


class MessageRequest(BaseModel):
    """Inbound chat message payload."""

    message: Annotated[str, Field(min_length=1, max_length=4000)]
    project_revision: int | None = Field(default=None, ge=0)

    @field_validator("message")
    @classmethod
    def trim_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a message before sending.")
        return value


async def custom_assembly_turn(
    session: ProjectSession,
    turn,
    message: str,
    usage: dict,
) -> dict:
    """Handle custom general-purpose assembly authoring/revision turns."""
    from app.concept_agent import attach_concept, develop_concept
    from app.concept_design import compile_concept

    apply_update_fn = api_hook("apply_update", apply_update)
    accept_fn = api_hook("_accept_project", accept_project)

    previous = session.project
    design = previous.get("concept_design")
    actions = {a.kind for a in turn.actions}
    updated = previous
    calls = []
    status = "ready"
    reply = turn.assistant_message
    if (
        previous.get("bench_station")
        or previous.get("engineering_design")
        or state_for(previous).get("selected_platform_id")
        or previous["workflow"]["kind"] == "liquid_preparation"
    ):
        reply = "Start a new project for a custom assembly so you can keep this workstation."
    elif turn.workflow_kind == "liquid_preparation" and design:
        reply = "Start a new project for a liquid-handling workstation to keep this assembly."
    elif actions & {"develop_concept", "develop_layout"}:
        patch = {
            k: v
            for k, v in turn.patch().items()
            if k.startswith("brief_") or k == "workspace_width_mm"
        }
        if not (
            previous["brief"].get("objective")
            or patch.get("brief_objective")
            or previous["brief"].get("process")
            or patch.get("brief_process")
        ):
            patch["brief_objective"] = message[:1000]
        candidate = apply_update_fn(previous, {**patch, "workflow_kind": "general"})
        try:
            report_activity("assembling")
            compiled, reply, calls = await develop_concept(
                candidate, message, history=session.history
            )
        except ModelClientError as exc:
            if exc.failure_kind != "concept_validation":
                raise
            compiled, reply = None, str(exc)
            calls = getattr(exc, "model_calls", [])
        if compiled is not None:
            updated = attach_concept(previous, candidate, compiled)
            status = "accepted"
        else:
            status = "needs_input"
            if not design:
                updated = candidate
    elif design and turn.workspace_width_mm is not None:
        candidate = apply_update_fn(previous, {"workspace_width_mm": turn.workspace_width_mm})
        compiled = compile_concept(design["proposal"], turn.workspace_width_mm)
        updated = attach_concept(previous, candidate, compiled)
        status = "accepted"
        reply = "The assembly layout and downloads now use the updated bench width."
    elif design and "confirm" in actions:
        from app.design_workflow import approve_machine, require_approval_request

        try:
            require_approval_request(message)
            if any(value is not None for value in turn.patch().values()):
                raise ValueError(
                    "Review hardware changes separately before approving the machine."
                )
            updated = approve_machine(previous)
            status = "accepted"
            reply = "Machine approved. Review the assembly software and commissioning guide."
        except ValueError as exc:
            status, reply = "rejected", str(exc)
    elif design and "show_files" in actions:
        reply += " Download the complete package in the design workspace to use it offline."
    from app.chat_sourcing import complete_component_changes

    reply = complete_component_changes(previous, updated, reply)
    if updated is not previous:
        accept_fn(session, updated)
    session.history.extend(
        [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    )
    session.history = session.history[-DISPLAY_HISTORY_LIMIT:]
    return {
        "project": deepcopy(updated),
        "assistant_message": reply,
        "usage": usage,
        "recommendations": [],
        "action_runs": [
            {
                "kind": "develop_concept",
                "status": status,
                "message": reply,
                "model_calls": calls,
            }
        ],
    }


def _handle_cold_transfer_turn(
    session: ProjectSession,
    current: dict,
    cold_intent: str,
    message: str,
    usage: dict,
) -> dict:
    """Apply or discuss cold-transfer changes inside an active project lock."""
    from app.chat_sourcing import cold_component_reply
    from app.cold_transfer import (
        apply_cold_transfer,
        clear_cold_transfer,
        require_supported_payload_request,
    )

    accept_fn = api_hook("_accept_project", accept_project)
    updated = deepcopy(current)
    status = "ready"
    if cold_intent == "add":
        try:
            require_supported_payload_request(message)
            if current.get("cold_transfer_extension"):
                reply = (
                    "The dedicated refrigerator and plate gripper are already "
                    "in this design. Select Preview plate transfer to watch it."
                )
            else:
                updated = apply_cold_transfer(current)
                status = "accepted"
                reply = (
                    "Added a dedicated refrigerator and plate gripper. Select "
                    "Preview plate transfer to follow the 96-well plate from its "
                    "holder to the cabinet shelf. Grip, clearances and cooling "
                    "performance "
                    "still need physical verification."
                )
        except ValueError as exc:
            status, reply = "rejected", str(exc)
    elif cold_intent == "remove":
        updated = clear_cold_transfer(current)
        if current.get("cold_transfer_extension"):
            updated["revision"] += 1
            updated["status"] = "draft"
            status = "accepted"
            reply = "Removed the dedicated refrigerator and transfer gripper."
        else:
            reply = "This design already has no cold-transfer module."
    elif cold_intent == "clarify":
        reply = (
            "A refrigerator and transfer mechanism can be developed as a separate "
            "subsystem. The current transfer preview handles a rigid 96-well "
            "plate; it cannot pick individual tubes or a rack. Should the "
            "stored payload be individual tubes, their rack, or a plate?"
        )
    else:
        reply = (
            "A separate refrigerator and transfer mechanism can be added. "
            "The current Jubilee motion adapter transfers a selected rigid "
            "96-well plate; tubes need a separately designed grip or rack "
            "interface. This discussion has preserved the design."
        )
    if cold_intent != "remove" and not (
        cold_intent == "add" and current.get("cold_transfer_extension")
    ):
        reply += cold_component_reply(updated, session.procurement_report, session.cost_estimate)
    if status == "accepted":
        accept_fn(session, updated, preserve_cold=cold_intent != "remove")
    session.history.extend(
        [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    )
    session.history = session.history[-DISPLAY_HISTORY_LIMIT:]
    return {
        "project": deepcopy(updated),
        "assistant_message": reply,
        "usage": usage,
        "recommendations": [],
        "action_runs": [{"kind": "cold_transfer", "status": status, "message": reply}],
        "assistant_message_origin": "canonical_cold_transfer_state",
    }


async def handle_chat_message(project_id: str, request: MessageRequest) -> dict | JSONResponse:
    """Process one user chat turn inside the project's mutation lock."""
    session = api_hook("session_for", session_for)(project_id)
    generate_turn_fn = api_hook("generate_turn", generate_turn)
    apply_update_fn = api_hook("apply_update", apply_update)
    execute_actions_fn = api_hook("execute_chat_actions", execute_chat_actions)
    describe_workflow_fn = api_hook("describe_workflow", describe_workflow)
    accept_fn = api_hook("_accept_project", accept_project)

    async with session.model_lock():
        current = session.project
        if request.project_revision is not None:
            check_revision(current, request.project_revision)
        from app.cold_transfer import (
            contextual_cold_transfer_intent,
            offline_cold_transfer_intent,
            semantic_cold_transfer_intent,
        )

        try:
            turn = None
            usage = {}
            try:
                report_activity("interpreting")
                turn, usage = await generate_turn_fn(
                    project_with_report(current, session.procurement_report, session.cost_estimate),
                    request.message,
                    list(session.history[-MODEL_HISTORY_LIMIT:]),
                )
                from app.bench_station_chat import clarification_turn

                turn = clarification_turn(turn, request.message, current)
                if current.get("concept_design") or any(
                    action.kind == "develop_concept" for action in turn.actions
                ):
                    return await custom_assembly_turn(session, turn, request.message, usage)
                cold_intent = contextual_cold_transfer_intent(
                    turn, request.message, current, session.history
                ) or semantic_cold_transfer_intent(turn, request.message, current)
            except ModelClientError as exc:
                cold_intent = (
                    offline_cold_transfer_intent(request.message)
                    if exc.failure_kind == "provider_unavailable"
                    else None
                )
                if cold_intent is None:
                    raise
            if cold_intent:
                return _handle_cold_transfer_turn(
                    session, current, cold_intent, request.message, usage
                )
            if turn.labware_selection.profile_id is not None:
                turn.normalize_container_volume_scope(request.message)
            actions = list(turn.actions)
            tip_update = getattr(turn, "tip_rack", TipRackUpdate())
            tip_change = (
                tip_update.profile_id is not None
                or bool(tip_update.spec.model_dump(exclude_none=True))
                or bool(
                    current.get("tip_rack_request", {}).get("profile_id")
                    and (
                        tip_update.printer_available is not None
                        or current.get("tip_rack_request", {}).get("awaiting_printer")
                        or current.get("tip_rack_request", {}).get("awaiting_placement")
                    )
                )
            )
            if tip_change and not any(action.kind == "select_tip_rack" for action in actions):
                actions.append(ChatAction(kind="select_tip_rack", source=request.message[:1000]))
            elif (
                current.get("tip_rack_request", {}).get("awaiting_printer")
                and declared_printer(request.message, contextual=True) is not None
                and not any(action.kind == "show_tip_rack_options" for action in actions)
            ):
                actions.append(
                    ChatAction(kind="show_tip_rack_options", source=request.message[:1000])
                )
            if turn.labware_selection.profile_id is not None and not any(
                action.kind == "select_labware" for action in actions
            ):
                actions.append(
                    ChatAction(kind="select_labware", source=turn.labware_selection.source)
                )
            patch = extension_only_patch(turn.patch(), actions)
            if (
                turn.labware_selection.profile_id is not None
                and current.get("engineering_design")
                and not patch
                and not turn.engineering.requirements
                and not turn.engineering.options
                and turn.engineering.platform_choice is None
            ):
                actions = [action for action in actions if action.kind != "develop_layout"]
            from app.design_workflow import RECIPE_FIELDS, workflow_state

            recipe_changed = any(
                key in patch and patch[key] != current["summary"].get(key) for key in RECIPE_FIELDS
            )
            recipe_only = bool(patch) and set(patch).issubset(RECIPE_FIELDS)
            if recipe_only and current.get("engineering_design"):
                actions = [action for action in actions if action.kind != "develop_layout"]
            updated = apply_update_fn(current, patch)
            if recipe_changed and workflow_state(current)["machine_approved"] and (
                updated.get("plan", {}).get("status") != "ready"
            ):
                raise ValueError(updated["plan"]["message"])
            engineering = getattr(turn, "engineering", EngineeringUpdate())
            updated = apply_engineering_update(
                updated,
                engineering,
                request.message,
                bump=updated["revision"] == current["revision"],
            )
            updated = update_labware_brief(updated, turn.labware, request.message)
            updated = update_tip_request(updated, tip_update, request.message)
            if (
                turn.labware.patch()
                and current.get("labware_rack")
                and not any(
                    action.kind in ("develop_labware", "remove_labware") for action in actions
                )
            ):
                index = next(
                    (i for i, action in enumerate(actions) if action.kind == "confirm"),
                    len(actions),
                )
                actions.insert(
                    index, ChatAction(kind="develop_labware", source=request.message[:1000])
                )
            new_choice = state_for(updated)["selected_platform_id"]
            preserve_layout = any(
                phrase in request.message.casefold()
                for phrase in (
                    "keep the machine layout unchanged",
                    "keep the layout unchanged",
                    "do not change the layout",
                    "lascia invariata la disposizione",
                )
            )
            if (
                preserve_layout
                and current.get("engineering_design")
                and not engineering.platform_choice
                and all(
                    current["summary"].get(k) == updated["summary"].get(k)
                    for k in ("workspace_width_mm", "cooling_enabled", "cooling_target_c")
                )
            ):
                actions = [action for action in actions if action.kind != "develop_layout"]
            context_runs = []
            if (
                engineering.platform_choice
                and new_choice
                and not actions
                and updated["workflow"]["kind"] == "liquid_preparation"
            ):
                if has_workstation_design_context(updated):
                    actions = [ChatAction(kind="develop_layout", source=engineering.choice_source)]
                else:
                    context_runs = [
                        {
                            "kind": "develop_layout",
                            "status": "ready",
                            "needs_context": True,
                            "message": workstation_context_question(updated),
                        }
                    ]
            if (
                not actions
                and current.get("engineering_design")
                and any(
                    key in patch and current["summary"].get(key) != updated["summary"].get(key)
                    for key in ("workspace_width_mm", "cooling_enabled", "cooling_target_c")
                )
            ):
                actions = [ChatAction(kind="develop_layout", source=request.message[:1000])]
            from app.bench_station_chat import update_station

            updated, station_run = update_station(
                current, updated, actions, request.message, turn.workflow_kind
            )
            if station_run:
                actions = [a for a in actions if a.kind != "develop_bench_station"]
            action_runs = [station_run] if station_run else context_runs
            if updated.get("bench_station"):
                bench_messages = {
                    "show_build_plan": "Work through the station follows this revision's ordered "
                    "operator steps and manual handoffs. Download the plan JSON and assisted "
                    "Python runner together; the operator handles the instruments. Equipment "
                    "selection, bench access, compatibility and performance need verification.",
                    "show_sources": "Modules and sourcing references lists this station's "
                    "equipment candidates and original source links. These are not selected "
                    "purchases or verified compatibility. Refresh US prices for supplier data.",
                    "show_components": "Modules and sourcing references lists the current station "
                    "equipment and supplies. Project cost separates supplier observations "
                    "from model estimates in USD.",
                    "show_files": "Download layout (SVG), plan (JSON) and assisted runner (Python) "
                    "from the station panel. These are schematic planning and human-operated "
                    "checkpoints, not fabrication CAD or hardware control.",
                }
                action_runs.extend(
                    {
                        "kind": action.kind,
                        "status": "ready",
                        "bench_station": True,
                        "message": bench_messages[action.kind],
                    }
                    for action in actions
                    if action.kind in bench_messages
                )
                actions = [action for action in actions if action.kind not in bench_messages]
            if actions:
                if any(action.kind in ("select_labware", "select_tip_rack") for action in actions):
                    report_activity("applying")
                    updated, action_runs = await execute_actions_fn(
                        updated, actions, request.message, turn.labware_selection, tip_update
                    )
                else:
                    updated, action_runs = await execute_actions_fn(
                        updated, actions, request.message
                    )
                if any(run["status"] in ("failed", "rejected") for run in action_runs):
                    updated = deepcopy(current)
            if updated.get("labware_rack"):
                updated = reapply_labware(updated, require_accepted=False)
            if updated.get("labware_plate"):
                updated = reapply_plate(updated, require_accepted=False)
            if updated.get("tip_rack"):
                updated = reapply_tip_rack(updated, require_accepted=False)
            if recipe_changed and workflow_state(current)["machine_approved"] and (
                updated.get("plan", {}).get("status") != "ready"
            ):
                raise ValueError(updated["plan"]["message"])
        except ModelClientError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": str(exc),
                    "failure_kind": getattr(exc, "failure_kind", "model_request"),
                    "usage": {"model": session.model, **getattr(exc, "usage", {})},
                },
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{exc} The project was not changed."
            ) from None

        canonical_reply = labware_chat_reply(current, updated, action_runs, patch)
        if any(run.get("bench_station") for run in action_runs):
            canonical_reply = "\n\n".join(run["message"] for run in action_runs)
        tip_runs = [
            run
            for run in action_runs
            if run["kind"] in ("select_tip_rack", "show_tip_rack_options")
        ]
        if tip_runs:
            canonical_reply = "\n\n".join(run["message"] for run in tip_runs)
        reply = canonical_reply if canonical_reply is not None else turn.assistant_message
        from app.platform_profiles import engineering_guidance

        if not updated.get("bench_station") and (
            state_for(updated)["selected_platform_id"]
            or any(run["kind"] == "show_build_plan" for run in action_runs)
        ):
            updated["engineering_guidance"] = engineering_guidance(updated)
        action_outcome_reply = any(
            run.get("needs_context") or run.get("status") in ("failed", "rejected")
            for run in action_runs
        )
        if action_runs:
            if action_outcome_reply and canonical_reply is None:
                reply = "\n\n".join(run["message"] for run in action_runs)
            elif canonical_reply is None:
                reply += "\n\n" + "\n".join(run["message"] for run in action_runs)
        recommendations = []
        for recommendation in turn.recommendations:
            ref = resolve_reference(recommendation.reference_id)
            recommendations.append({"reference": ref, "reason": recommendation.reason})
        workflow = describe_workflow_fn(updated)
        updated["workflow"] = workflow
        design_available = bool(workflow.get("design_available"))
        if design_available and not action_outcome_reply:
            plan = updated.get("plan")
            previous_plan = current.get("plan")
            if plan is not None and plan != previous_plan:
                if plan["status"] == "invalid":
                    if canonical_reply is None:
                        reply += f"\n\nThe preparation plan needs a correction: {plan['message']}"
                elif plan["status"] == "ready":
                    reply += "\n\nYour preparation plan is updated in the workspace."
            if "workspace_width_mm" in patch and not action_runs:
                width = updated["summary"]["workspace_width_mm"]
                if updated["design"]["constraint_status"] == "infeasible":
                    reply += (
                        f"\n\nA {width:g} mm width is below this concept's supported minimum. "
                        "The previous geometry is retained; "
                        "revise the constraint before confirming."
                    )
                else:
                    reply += (
                        f"\n\nRecorded a {width:g} mm maximum width. "
                        "Develop a fresh layout with OpenAI to check the arrangement; "
                        "this message has not generated a new machine."
                    )
            reply += cooling_feedback(patch, updated)

        from app.chat_sourcing import (
            complete_component_changes,
            complete_equipment_question,
            complete_platform_comparison,
        )

        reply = complete_platform_comparison(current, updated, request.message, reply)
        reply = complete_component_changes(current, updated, reply)
        reply = complete_equipment_question(current, updated, reply)
        display_reply = reply
        if recommendations:
            reply += "\n\nReviewed references:\n" + "\n".join(
                f"{r['reference']['title']} [{r['reference']['id']}]: {r['reason']}"
                for r in recommendations
            )
        accept_fn(session, updated)
        session.history.extend(
            [{"role": "user", "content": request.message}, {"role": "assistant", "content": reply}]
        )
        session.history = session.history[-DISPLAY_HISTORY_LIMIT:]
        return {
            "project": deepcopy(updated),
            "assistant_message": display_reply,
            "usage": usage,
            "recommendations": recommendations,
            "action_runs": action_runs,
            "assistant_message_origin": (
                "canonical_labware_state"
                if canonical_reply is not None
                else "canonical_action_state"
                if action_outcome_reply
                else "model"
            ),
        }

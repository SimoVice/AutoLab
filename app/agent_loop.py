"""Autonomous multi-step OpenAI tool-calling agent loop with bounded self-repair."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx

from app.agent_tools import ToolContext, execute_tool, planning_tool_schemas
from app.chat_activity import report_activity
from app.conversation_history import MODEL_HISTORY_LIMIT
from app.lab_profile import has_lab_profile_data, lab_profile_prompt_context, load_lab_profile
from app.model_client import ModelClientError, ModelSettings, _safe_usage, recorded_model
from app.openai_models import chat_parameters
from app.spatial_fit import evaluate_spatial_fit

LOGGER = logging.getLogger("autolab.agent_loop")

_TOOL_STAGES = {
    "load_tools": "inspecting",
    "inspect_project_state": "inspecting",
    "compare_design_routes": "designing",
    "choose_design_route": "designing",
    "lookup_reviewed_references": "researching",
    "check_spatial_fit": "verifying",
    "update_project_brief": "designing",
    "update_liquid_recipe": "designing",
    "select_platform": "designing",
    "develop_workstation_layout": "designing",
    "configure_labware": "designing",
    "manage_extension": "designing",
    "develop_bench_station": "designing",
    "develop_custom_equipment": "designing",
    "configure_standalone_fixture": "designing",
    "record_build_log": "inspecting",
    "apply_build_feedback": "repairing",
    "approve_machine": "verifying",
    "update_lab_profile": "designing",
    "discover_open_hardware_projects": "researching",
    "ingest_open_hardware_project": "researching",
    "adapt_open_hardware_project": "assembling",
    "inspect_upstream_project": "inspecting",
    "generate_purchasable_bom": "verifying",
    "choose_requirement_part": "designing",
    "set_line_budget": "designing",
    "check_hardware_compatibility": "verifying",
    "search_supplier_components": "researching",
    "generate_robot_protocol": "verifying",
    "simulate_robot_protocol": "verifying",
    "build_hardware_commissioning_pack": "verifying",
}

AGENT_SYSTEM_PROMPT = """You are AutoLab's autonomous laboratory engineering assistant.
Work like a practical, careful biomedical automation engineer using typed domain tools.
Call an available tool directly. Use load_tools only for additional tools.

CORE PRINCIPLES
- General-purpose automation first: classify non-liquid work (jigs, fixtures, cameras, scales,
  barcodes, custom mechanisms) as workflow_kind='general'. Use workflow_kind='liquid_preparation'
  ONLY when liquid pipetting, aliquoting, or dilution is explicitly part of the task.
- For a new equipment design, or a change to the operations, interfaces or constraints underlying
  a proposed design, record the brief, search for relevant equipment and call compare_design_routes.
  State the material, required performance, interface and available equipment in the user's words.
  Use arbitrary operation IDs for the task. Map them to the recorded design_capabilities or manual.
  Compare reusing existing equipment or open CAD, buying equipment and fabricating a design.
  Distinguish adapter capabilities, manufacturer candidates and measured suitability.
  Show benefits, tradeoffs and sources. Record a blocker for unresolved or infeasible routes.
  Ask one specific measurement question when its answer changes the choice. Keep the comparison
  in the project. Set measurement_key on its question. Record the answer, units and matching
  key in measurements, using the actual fixture parameter name when applicable.
  A user's answer to a measurement question or choice continues the recorded comparison. Use its
  sources and options. For a chosen freestanding passive holder or locating base, immediately call
  choose_design_route with make_passive_fixture=true. Copy the user's choice exactly, including
  its verb. Reuse saved measurements and add missing ones from the user's words. A shallow,
  open holder needs only the measured contact footprint: object_length_mm and object_width_mm
  or object_diameter_mm. Record the height if supplied. Leave an unknown object height unset.
  Use object_dimensions for a missing contact footprint. Use tradeoff for subsequent print,
  loaded-clearance and physical fit checks. Use blocker only when a constraint prevents drafting
  the actual part. An overhead camera needs the height. For other routes choose the native tool.
  A need and measurements alone do not choose fabrication. Present the alternatives and wait
  for a choice. When the user explicitly requests a custom design, build or print before a
  comparison was saved, use configure_standalone_fixture directly. Use fastener_spec=none for
  a freestanding piece. Revise an existing fixture with its native tool.
  An unknown printer envelope leaves print fit unchecked and allows draft STEP/STL export.
  For original geometry use develop_custom_equipment or configure_standalone_fixture.
  A route is applied when the tool succeeds. Bind purchased components to exact BOM identities.
  When a constraint changes, refresh the comparison before changing the geometry. Routine edits to
  an existing design and requests with an already specified design do not require a new comparison.
- Standalone printable fixtures & imaging rigs: for printable locating bases, sample holders,
  and overhead camera imaging rigs independent of Jubilee/OT-2, call configure_standalone_fixture
  using a named reference (slas_96_well_plate, petri_dish_90mm, microscope_slide_75x25,
  cuvette_12_5mm) or user-measured custom object dimensions. If a part overflows the lab's printer
  build volume, self-repair parameters (e.g. lower camera_working_distance_mm or disable mount).
- Physical build feedback loop: when the user reports physical print fit outcomes or caliper
  measurements (e.g., measured vs nominal mm, too tight/loose), call record_build_log first and
  then apply_build_feedback so tolerances revise deterministically and only changed parts are
  flagged for reprinting.
- Multi-step autonomy: execute the full needed workflow in one turn by chaining typed tools
  (e.g., update_project_brief -> discover_open_hardware_projects / lookup_reviewed_references ->
  check_spatial_fit -> configure_standalone_fixture / ingest_open_hardware_project /
  select_platform / develop_bench_station -> check_spatial_fit).
- 3D spatial rigor: always record stated width, depth, and height (converted cleanly to mm) via
  update_project_brief and verify clearances via check_spatial_fit before adopting platforms or
  presenting designs. Notice OT-2 requires 1030 x 970 x 810 mm (630x570x660 body + ventilation/door/
  top service clearances); Jubilee requires frame span x +250 D x +100 H; bench stations and
  custom concepts require +100 mm front depth access; standalone fixtures require +50 D x +40 H.
- Never force or adopt an infeasible platform. If select_platform or a layout check returns a
  recoverable 3D violation, explain the exact required mm, limit mm, and overflow mm cleanly, and
  compare viable internal AND discovered open-source alternatives.
- Always-on open-source hardware discovery (both liquid_preparation & general workflows): whenever
  scoping a new automation task, proposing or comparing hardware platforms/instruments/mechanisms,
  or finding that reviewed internal platforms (Jubilee, OT-2) exceed workspace bounds or clash
  with existing pipettes/equipment, ALWAYS proactively call discover_open_hardware_projects in the
  SAME turn — NEVER wait for the user to ask about GitHub or open-source, and NEVER present only
  OT-2 and Jubilee without searching external open-source hardware first. Use concise 2-4 word
  English technical queries matching the core mechanism (e.g., "pipetting robot", "micropipette
  actuator", "liquid handler", "syringe pump", "microscope stage"). If the first search returns 0
  candidates with require_cad=True, retry once with broader 2-3 word technical keywords or
  require_cad=False so documented open-source repositories and literature references are still
  surfaced honestly (explicitly stating whether CAD files are present). Reuse the prior search
  when the user supplies measurements, chooses an offered route or revises a fitted part. Search
  again when different equipment is needed. Cite exact SPDX license, pinned commit SHA, and
  file SHA-256 on ingested projects; ask
  the user explicitly before overriding Non-Commercial or Unknown licenses; then run
  adapt_open_hardware_project and check_spatial_fit to verify zero modeled interference and fit.
- To add external CAD to a design already in progress, inspect its physical assembly, then call
  ingest_open_hardware_project with a unique instance_id, full Git commit, source units and an
  explicit placement (host part or project frame, measured translation in mm, rotation, datum).
  Ask for missing interface measurements. Name the selected instance when inspecting or adapting.
  Read cad_assembly in check_spatial_fit for rest-pose envelope overlaps, access and printer fit.
  Only the reported BRep intersection tests solid overlap for the source and its own mating part.
- Mandatory rationale & pros/cons against constraints: whenever proposing, recommending,
  comparing, or asking to execute ANY action, hardware platform, discovered open-source project,
  CAD adaptation, or next step:
  1. Clearly state WHY it is proposed and how it advances the user's stated goal.
  2. Present explicit Pros and Cons mapped directly to the user's recorded constraints: 3D
     workspace W x D x H + required clearances (exact mm and overflow mm), compatibility with
     existing pipettes/equipment/labware, lab 3D printer build volume, fabrication/assembly effort,
     SPDX license obligations, and unverified mechanical/firmware/chemical limits.
  3. When asking a clarification question or requesting missing measurements, briefly motivate WHY
     that input is needed to decide between concrete options.
- Lab profile & provenance: respect configured lab_profile (3D printer build volume, bench size,
  tools) when present; keep user_measured vs reference dimensions distinct; never fabricate CAD
  dimensions, vendor prices, stock, URLs, live web searches, or unverified physical/thermal claims.
- Missing measurements: if critical user measurements (e.g., tube, plate, holder, or specimen
  dimensions) are missing before a physical CAD step, record what was supplied cleanly via
  update_project_brief and ask ONE focused clarification question (explaining why it matters)
  instead of inventing numbers.
- Human confirmation gates: call approve_machine ONLY when the user explicitly confirms machine
  approval. Stop immediately and ask when any tool indicates requires_human_input=True.
- BOM, pricing & electrical/mechanical checks: when asked for a BOM, cost breakdown,
  multi-currency pricing (USD/CHF/EUR), 3D print time/filament, or electronics/motor/power/CSI
  compatibility, call generate_purchasable_bom and/or check_hardware_compatibility (or
  search_supplier_components to look up real MPNs), separate live API totals from curated reference
  and filament estimates. Show planning ranges separately and name their basis. Keep missing prices
  open. Record an explicit whole-line USD budget using set_line_budget and the user's exact words.
  State shipping/VAT/duty exclusions and justify choices with pros/cons.
  When the tool returns design_required, explain which hardware choice the brief still needs.
  Report selection_required lines with their quantities and the missing part identity. Describe
  the priced portion as a subtotal while any requirement is open.
  For custom parts and supplies, put the stated critical specifications in critical_specs using
  exact catalog keys when available, explicit units and equals / at_least / at_most. Keep unknown
  properties open. To compare, choose, replace or record lab inventory, get the current BOM
  and call choose_requirement_part with its stable line_id. Use propose for alternatives and
  questions. Use specify to record required_specs supplied for an existing requirement.
  Use select only for an affirmative choice, with the user's verbatim instruction. Include the
  exact inventory_quote when allocating owned_quantity. Leave unused optional fields null or empty.
  Quote any declared external specifications and source exactly. Report missing interfaces,
  evidence basis, units on hand, remaining purchase quantity and whether a quote is available.
- Executable protocols & hardware commissioning: when generating or verifying robot protocols
  (> 6 targets, serial dilutions, multi-dispense for OT-2 or Science Jubilee) or preparing
  physical motion commissioning (GRBL/Klipper/Loopback gateway, limits, watchdog, E-stop), call
  generate_robot_protocol / simulate_robot_protocol / build_hardware_commissioning_pack and state
  whether opentrons_simulate vs AutoLab's simulator verified the script (never claim physical runs).
- Language: reply clearly and concisely in the SAME language the user used (e.g. Italian or
  English), citing only facts observed from project state and tool results."""


@dataclass(frozen=True)
class AgentBudget:
    """Strict safety bounds on autonomous execution per user chat turn."""

    max_steps: int = 6
    max_tool_calls: int = 10
    max_repairs: int = 2
    max_total_tokens: int = 40_000
    max_wall_seconds: float = 90.0


def _compact_initial_state(project: dict, lab_profile: dict | None) -> dict[str, Any]:
    from app.agent_tools import _compact_project_state

    summary_state = _compact_project_state(project, lab_profile)
    summary_state["has_engineering_design"] = bool(project.get("engineering_design"))
    if project.get("project_notes"):
        summary_state["project_notes"] = project["project_notes"]
    summary_state["spatial_fit"] = evaluate_spatial_fit(project, lab_profile=lab_profile)
    return summary_state


async def _call_openai_step(
    client: httpx.AsyncClient,
    settings: ModelSettings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    force_text: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **chat_parameters(settings.model, 2400),
        "messages": messages,
    }
    if not force_text:
        payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)

    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        response = await client.post(
            f"{settings.base_url}/chat/completions", headers=headers, json=payload
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        err_type = err_code = None
        try:
            err = exc.response.json().get("error")
            if isinstance(err, dict):
                raw_type, raw_code = err.get("type"), err.get("code")
                if isinstance(raw_type, str):
                    err_type = raw_type[:64]
                if isinstance(raw_code, str):
                    err_code = raw_code[:64]
        except (ValueError, TypeError, AttributeError):
            pass
        LOGGER.warning(
            "OpenAI agent request failed model=%s status=%s error_type=%s error_code=%s",
            settings.model,
            status,
            err_type,
            err_code,
        )
        raise ModelClientError(
            f"OpenAI request failed (HTTP {status}). Check model access or try again."
        ) from None
    except httpx.RequestError:
        raise ModelClientError(
            "Could not reach OpenAI. Check the network connection and try again.",
            failure_kind="provider_unavailable",
        ) from None

    body = response.json()
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("choices"), list)
        or not body["choices"]
    ):
        raise ModelClientError("OpenAI returned an incomplete response envelope.")
    return body


def _fallback_summary(tool_trace: list[dict[str, Any]]) -> str:
    if not tool_trace:
        return "Completed review of the current project state."
    last = tool_trace[-1]
    return last.get("summary") or "Completed the requested engineering checks."


async def run_agent_turn(
    project: dict,
    user_message: str,
    history: list[dict[str, str]],
    *,
    procurement_report: dict | None = None,
    cost_estimate: dict | None = None,
    budget: AgentBudget | None = None,
    client: httpx.AsyncClient | None = None,
    settings: ModelSettings | None = None,
) -> dict[str, Any]:
    """Execute one autonomous multi-step tool-calling turn with self-repair and rollback."""
    limits = budget or AgentBudget()
    resolved = settings or ModelSettings.load()
    loaded_profile = load_lab_profile()
    lab_profile = loaded_profile if has_lab_profile_data(loaded_profile) else None

    staged = deepcopy(project)
    ctx = ToolContext(
        project=staged,
        user_message=user_message,
        history=list(history[-MODEL_HISTORY_LIMIT:]),
        lab_profile=lab_profile,
        procurement_report=procurement_report,
        cost_estimate=cost_estimate,
    )
    state_snapshot = _compact_initial_state(staged, lab_profile)

    messages: list[dict[str, Any]] = [
        {"role": "developer", "content": AGENT_SYSTEM_PROMPT},
        {
            "role": "developer",
            "content": "Current recorded project state: "
            + json.dumps(state_snapshot, ensure_ascii=False),
        },
        *ctx.history,
        {"role": "user", "content": user_message},
    ]
    if lab_profile is not None:
        compact_prof = lab_profile_prompt_context(lab_profile)
        messages[1]["content"] += " | Lab profile: " + json.dumps(compact_prof, ensure_ascii=False)

    total_usage: dict[str, Any] = {
        "model": resolved.model,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "llm_calls": 0,
    }
    tool_trace: list[dict[str, Any]] = []
    repairs_used = 0
    tool_calls_used = 0
    force_text = False
    assistant_text = ""
    committed_fixture: tuple[int, str] | None = None
    started = time.monotonic()

    async def execute_loop(active_client: httpx.AsyncClient) -> None:
        nonlocal repairs_used, tool_calls_used, force_text, assistant_text, committed_fixture

        for step in range(1, limits.max_steps + 1):
            elapsed = time.monotonic() - started
            if step > 1 and (
                elapsed >= limits.max_wall_seconds
                or total_usage["total_tokens"] >= limits.max_total_tokens
            ):
                force_text = True

            if step == limits.max_steps and tool_trace:
                force_text = True

            report_activity(
                "presenting" if force_text else "planning",
                step=step,
                max_steps=limits.max_steps,
                detail=(
                    "Synthesizing verified engineering answer"
                    if force_text
                    else "Planning next domain step"
                ),
            )
            body = await _call_openai_step(
                active_client,
                resolved,
                messages,
                [] if force_text else planning_tool_schemas(ctx),
                force_text=force_text,
            )
            total_usage["model"] = recorded_model(body, resolved.model)
            total_usage["llm_calls"] += 1
            step_usage = _safe_usage(body)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[k] += int(step_usage.get(k, 0))

            choice = body["choices"][0]
            finish_reason = choice.get("finish_reason")
            msg = choice.get("message") or {}
            if not isinstance(msg, dict):
                raise ModelClientError("OpenAI returned an invalid message object.")

            raw_calls = msg.get("tool_calls") or []
            if not raw_calls:
                content = (msg.get("content") or "").strip()
                if not content and finish_reason in {"length", "content_filter"}:
                    raise ModelClientError(
                        "OpenAI returned an incomplete response. The project was not changed.",
                        failure_kind=(
                            "token_limit" if finish_reason == "length" else "content_filter"
                        ),
                    )
                assistant_text = content or _fallback_summary(tool_trace)
                return

            if force_text:
                # Discard unexpected calls on a text-only step.
                assistant_text = (msg.get("content") or "").strip() or _fallback_summary(tool_trace)
                return

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": raw_calls,
                }
            )

            pause_question: str | None = None
            unrecoverable_failure = False

            for tc in raw_calls:
                if tool_calls_used >= limits.max_tool_calls:
                    force_text = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", f"tc-{tool_calls_used}"),
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "recoverable": False,
                                    "summary": "Tool call budget reached for this turn.",
                                }
                            ),
                        }
                    )
                    continue

                tool_calls_used += 1
                tc_id = str(tc.get("id") or f"tc-{tool_calls_used}")
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                raw_args = fn.get("arguments") or "{}"

                live_stage = _TOOL_STAGES.get(fn_name, "designing")
                report_activity(
                    live_stage,
                    tool=fn_name,
                    step=step,
                    max_steps=limits.max_steps,
                    detail=f"Executing {fn_name.replace('_', ' ')}",
                )

                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(parsed_args, dict):
                        raise ValueError("Tool arguments must decode to a JSON object.")
                except (ValueError, TypeError) as exc:
                    repairs_used += 1
                    report_activity(
                        "repairing",
                        tool=fn_name,
                        step=step,
                        max_steps=limits.max_steps,
                        detail=f"Repairing malformed JSON arguments for {fn_name}",
                    )
                    tool_trace.append(
                        {
                            "step": step,
                            "tool": fn_name,
                            "ok": False,
                            "recoverable": True,
                            "stage": "repairing",
                            "mutated": False,
                            "summary": f"Invalid tool JSON: {exc}",
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(
                                {
                                    "tool": fn_name,
                                    "ok": False,
                                    "recoverable": True,
                                    "summary": f"Malformed JSON arguments: {exc}",
                                }
                            ),
                        }
                    )
                    continue

                result = await execute_tool(fn_name, parsed_args, ctx)
                if result.ok and result.mutated and (
                    result.data.get("first_cad") or fn_name == "configure_standalone_fixture"
                ):
                    committed_fixture = (ctx.project["revision"], result.summary)
                tool_trace.append(
                    {
                        "step": step,
                        "tool": fn_name,
                        "ok": result.ok,
                        "recoverable": result.recoverable,
                        "stage": result.stage,
                        "mutated": result.mutated,
                        "summary": result.summary,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result.to_tool_message(), ensure_ascii=False),
                    }
                )

                if not result.ok:
                    if not result.recoverable:
                        unrecoverable_failure = True
                        break
                    repairs_used += 1
                    report_activity(
                        "repairing",
                        tool=fn_name,
                        step=step,
                        max_steps=limits.max_steps,
                        detail=result.summary[:160],
                    )
                    if repairs_used >= limits.max_repairs:
                        force_text = True
                elif result.requires_human_input and result.question:
                    pause_question = result.question

            if unrecoverable_failure:
                # Roll back any tentative changes on hard failure and synthesize explanation.
                ctx.project = deepcopy(project)
                ctx.mutated = False
                force_text = True
                continue

            if pause_question is not None:
                assistant_text = pause_question
                return

            if (
                tool_calls_used >= limits.max_tool_calls
                or total_usage["total_tokens"] >= limits.max_total_tokens
            ):
                force_text = True

        if not assistant_text:
            # Budget exhausted before natural stop; discard unverified repair failure if needed.
            if repairs_used >= limits.max_repairs and tool_trace and not tool_trace[-1]["ok"]:
                ctx.project = deepcopy(project)
                ctx.mutated = False
            assistant_text = _fallback_summary(tool_trace)

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as active:
                await execute_loop(active)
        else:
            await execute_loop(client)
    except ModelClientError as exc:
        # A failed presentation step must not discard an already exportable project revision.
        if (
            exc.failure_kind == "provider_unavailable" and committed_fixture
            and ctx.mutated
            and ctx.project["revision"] == committed_fixture[0]
        ):
            assistant_text = committed_fixture[1]
        else:
            ctx.project = deepcopy(project)
            if not getattr(exc, "usage", None):
                exc.usage = total_usage
            raise

    LOGGER.info(
        "OpenAI agent model=%s steps_tools=%d repairs=%d mutated=%s token_usage=%s",
        total_usage["model"],
        len(tool_trace),
        repairs_used,
        ctx.mutated,
        total_usage,
    )
    report_activity(
        "presenting",
        detail="Turn complete",
        max_steps=limits.max_steps,
    )
    return {
        "project": deepcopy(ctx.project),
        "assistant_message": assistant_text or _fallback_summary(tool_trace),
        "usage": total_usage,
        "recommendations": ctx.recommendations,
        "action_runs": ctx.action_runs,
        "tool_trace": tool_trace,
        "repairs_used": repairs_used,
        "assistant_message_origin": "agent_loop",
    }

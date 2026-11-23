"""Autonomous multi-step OpenAI tool-calling agent loop with bounded self-repair."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx

from app.agent_tools import ToolContext, execute_tool, openai_tool_schemas
from app.chat_activity import report_activity
from app.conversation_history import MODEL_HISTORY_LIMIT
from app.lab_profile import has_lab_profile_data, lab_profile_prompt_context, load_lab_profile
from app.model_client import ModelClientError, ModelSettings, _safe_usage, recorded_model
from app.openai_models import chat_parameters
from app.spatial_fit import evaluate_spatial_fit

LOGGER = logging.getLogger("autolab.agent_loop")

_TOOL_STAGES = {
    "inspect_project_state": "inspecting",
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
}

AGENT_SYSTEM_PROMPT = """You are AutoLab's autonomous laboratory engineering assistant.
Work like a practical, careful biomedical automation engineer using typed domain tools.

CORE PRINCIPLES
- General-purpose automation first: classify non-liquid work (jigs, fixtures, cameras, scales,
  barcodes, custom mechanisms) as workflow_kind='general'. Use workflow_kind='liquid_preparation'
  ONLY when liquid pipetting, aliquoting, or dilution is explicitly part of the task.
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
  (e.g., update_project_brief -> lookup_reviewed_references / check_spatial_fit ->
  configure_standalone_fixture / select_platform / develop_bench_station -> check_spatial_fit).
- 3D spatial rigor: always record stated width, depth, and height (converted cleanly to mm) via
  update_project_brief and verify clearances via check_spatial_fit before adopting platforms or
  presenting designs. Notice OT-2 requires 1030 x 970 x 810 mm (630x570x660 body + ventilation/door/
  top service clearances); Jubilee requires frame span x +250 D x +100 H; bench stations and
  custom concepts require +100 mm front depth access; standalone fixtures require +50 D x +40 H.
- Never force or adopt an infeasible platform. If select_platform or a layout check returns a
  recoverable 3D violation, explain the exact required mm, limit mm, and overflow mm cleanly, or
  compare viable alternatives via lookup_reviewed_references.
- Open-source hardware discovery & adaptation: when the user asks to find, compare, import, or
  adapt existing open-source laboratory platforms/instruments/pumps/stages/fixtures beyond Jubilee
  and OT-2, call discover_open_hardware_projects first instead of guessing URLs or proposing only
  Jubilee/OT-2. Cite exact SPDX license, pinned commit SHA, and file SHA-256 on ingested projects;
  ask the user explicitly before overriding Non-Commercial or Unknown licenses; then run
  adapt_open_hardware_project and check_spatial_fit to verify zero modeled interference and fit.
- Lab profile & provenance: respect configured lab_profile (3D printer build volume, bench size,
  tools) when present; keep user_measured vs reference dimensions distinct; never fabricate CAD
  dimensions, vendor prices, stock, URLs, live web searches, or unverified physical/thermal claims.
- Missing measurements: if critical user measurements (e.g., tube, plate, holder, or specimen
  dimensions) are missing before a physical CAD step, record what was supplied cleanly via
  update_project_brief and ask ONE focused clarification question instead of inventing numbers.
- Human confirmation gates: call approve_machine ONLY when the user explicitly confirms machine
  approval. Stop immediately and ask when any tool indicates requires_human_input=True.
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
        "tools": tools,
        "tool_choice": "none" if force_text else "auto",
    }
    if not force_text:
        payload["parallel_tool_calls"] = False

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
    tools = openai_tool_schemas(staged, lab_profile)
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
    started = time.monotonic()

    async def execute_loop(active_client: httpx.AsyncClient) -> None:
        nonlocal repairs_used, tool_calls_used, force_text, assistant_text

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
                tools,
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
                # Defensive guard if a mock/provider ignores tool_choice="none".
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

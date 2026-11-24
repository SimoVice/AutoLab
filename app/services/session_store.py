"""In-memory session registry, project lock context, and persistence helpers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field

from fastapi import HTTPException

from app import project_library
from app.model_catalog import AGENT_MODELS
from app.model_client import configured_model, using_model
from app.services.runtime import api_hook


@dataclass
class ProjectSession:
    """Thread/async-safe per-project runtime state and associated sidecars."""

    project: dict
    history: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model: str = field(default_factory=configured_model)
    procurement_report: dict | None = None
    procurement_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cost_estimate: dict | None = None
    estimate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    functional_plan: dict | None = None

    @asynccontextmanager
    async def model_lock(self):
        async with self.lock:
            with using_model(self.model):
                try:
                    yield
                finally:
                    api_hook("_persist_session", persist_session)(self)


# Local mode remains process-local; configured hosted storage resumes trusted state.
_sessions: dict[str, ProjectSession] = {}


def persist_session(session: ProjectSession, *, title: str | None = None) -> dict | None:
    """Persist the current session snapshot when durable library storage is configured."""
    library = api_hook("project_library", project_library)
    return library.persist(
        {
            "project": session.project,
            "history": session.history,
            "model": session.model,
            "procurement_report": session.procurement_report,
            "cost_estimate": session.cost_estimate,
            "functional_plan": session.functional_plan,
        },
        title=title,
    )


def session_for(project_id: str) -> ProjectSession:
    """Resolve an active session or load it from persistent library storage."""
    library = api_hook("project_library", project_library)
    session = _sessions.get(project_id)
    if session is None:
        saved = library.load(project_id)
        if saved is not None:
            if saved.get("model") not in AGENT_MODELS:
                saved["model"] = configured_model()
            session = ProjectSession(**saved)
            _sessions[project_id] = session
    if session is None:
        raise HTTPException(status_code=404, detail="Project not found. Start a new project.")
    from app.design_workflow import approve_machine

    if (
        "machine_approval" not in session.project
        and session.project.get("status") == "confirmed"
        and session.project.get("engineering_design")
    ):
        previous_revision = session.project["revision"]
        session.project = approve_machine(session.project)
        session.project["revision"] = previous_revision
        session.project["machine_approval"]["revision"] = previous_revision
    return session


def accept_project(
    session: ProjectSession,
    updated: dict,
    *,
    preserve_cold: bool = True,
) -> None:
    """Rebind trusted sidecars at a validated project transition, never on reads."""
    from app.procurement import rebind_report

    before = session.project
    if (
        preserve_cold
        and before.get("cold_transfer_extension")
        and updated.get("workflow", {}).get("kind") == "liquid_preparation"
    ):
        from app.cold_transfer import apply_cold_transfer

        candidate = deepcopy(updated)
        candidate["cold_transfer_extension"] = deepcopy(before["cold_transfer_extension"])
        try:
            candidate = apply_cold_transfer(candidate, bump=False)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{exc} The project was not changed."
            ) from None
        updated.clear()
        updated.update(candidate)
    from app.bom_engine import reconcile_part_choices
    from app.budget_allowances import reconcile_budgets

    # Some design compilers rebuild the project rather than copying optional records.
    if "budget_allowances" not in updated and before.get("budget_allowances"):
        updated["budget_allowances"] = deepcopy(before["budget_allowances"])
    reconcile_part_choices(updated)
    reconcile_budgets(updated)
    report = rebind_report(before, updated, session.procurement_report)
    from app.design_workflow import refresh_workflow

    refresh_workflow(updated)
    session.project = updated
    session.procurement_report = report
    session.cost_estimate = None


def check_revision(project: dict, revision: int) -> None:
    """Raise HTTP 409 when a client revision does not match the session revision."""
    if revision != project["revision"]:
        raise HTTPException(
            status_code=409, detail="The project changed. Use its current revision."
        )


def configuration_message(project: dict) -> str:
    """Format the canonical cooling and dated-catalog fallback status message."""
    cooling = project["design"]["cooling"]
    sourcing = project["sourcing"]
    if cooling["enabled"]:
        status = (
            f"Refrigerated bay enabled. Target {cooling['target_c']:g} Â°C; manual sample transfer."
        )
    else:
        status = "Refrigerated bay is off."
    prefix = "from " if sourcing["is_lower_bound"] else ""
    return (
        f"{status} Dated catalog fallback: {prefix}${sourcing['subtotal']:.2f} USD "
        f"(checked {sourcing['checked_on']}). Refresh US prices for the current revision. "
        "Unpriced parts, shipping and taxes are excluded. Cooling performance is unverified."
    )


def cooling_feedback(patch: dict, project: dict) -> str:
    """Append cooling feedback when cooling parameters changed in a patch."""
    if not {"cooling_enabled", "cooling_target_c"}.intersection(patch):
        return ""
    return "\n\n" + configuration_message(project)

"""Project lifecycle, design, configuration, snapshot, concept, and bench routes."""

from __future__ import annotations

import inspect
import json
import math
from copy import deepcopy
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.chat_actions import execute_chat_actions
from app.chat_activity import activity_dependency, report_activity
from app.conversation_history import DISPLAY_HISTORY_LIMIT
from app.design_agent import develop_design
from app.design_sources import project_source_basis, reference_library
from app.domain import apply_update, configure_project, confirm_project, new_project
from app.inspection import clear_extension, develop_inspection
from app.labware import reapply_labware
from app.microplates import reapply_plate
from app.model_catalog import AGENT_MODELS, availability, discover_catalog, model_options
from app.model_client import ChatAction, ModelClientError
from app.project_storage import (
    MAX_SNAPSHOT_BYTES,
    ProjectNotes,
    decode_snapshot,
    make_snapshot,
    restore_snapshot,
)
from app.routers.library import resume_session_project
from app.routers.procurement import ProcurementRefreshRequest
from app.services.runtime import api_hook
from app.services.session_store import (
    ProjectSession,
    _sessions,
    accept_project,
    check_revision,
    configuration_message,
    persist_session,
    session_for,
)
from app.tip_racks import TipRackUpdate, select_tip_rack, tip_rack_options, update_tip_request
from app.workflows import (
    describe_workflow,
    has_workstation_design_context,
    workstation_context_question,
)

router = APIRouter()


class TipRackChoiceRequest(BaseModel):
    """Explicit tip-rack selection payload."""

    model_config = ConfigDict(extra="forbid")
    revision: Annotated[int, Field(strict=True, ge=0)]
    profile_id: Annotated[str, Field(min_length=1, max_length=100)]


class ConfigurationRequest(BaseModel):
    """Direct UI cooling/selection configuration update payload."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cooling_enabled: Annotated[bool | None, Field(strict=True)] = None
    cooling_target_c: Annotated[float | None, Field(ge=2, le=20)] = None
    selections: dict[str, str] | None = None

    @field_validator("cooling_target_c", mode="before")
    @classmethod
    def _strict_target(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or isinstance(value, str):
            raise ValueError("cooling_target_c must be a number.")
        return value

    @field_validator("cooling_target_c")
    @classmethod
    def _finite_target(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("cooling_target_c must be a number.")
        if not math.isfinite(float(value)):
            raise ValueError("cooling_target_c must be finite.")
        return float(value)

    @field_validator("selections")
    @classmethod
    def _valid_selections(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("selections must be an object.")
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("selections must map strings to strings.")
        return dict(value)


class ProjectNotesEdit(BaseModel):
    """Optimistic-concurrency project notes edit payload."""

    model_config = ConfigDict(extra="forbid")
    previous: ProjectNotes
    notes: ProjectNotes


class ModelSelectionRequest(BaseModel):
    """Per-project OpenAI model switch payload."""

    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=100, strict=True)


class ProgramRecipeRequest(BaseModel):
    """Post-approval liquid recipe update payload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project_revision: Annotated[int, Field(ge=0)]
    stock_concentration: Annotated[str, Field(min_length=1, max_length=1000)]
    target_concentrations: Annotated[str, Field(min_length=1, max_length=1000)]
    final_volume: Annotated[str, Field(min_length=1, max_length=1000)]


class MachineApprovalRequest(BaseModel):
    """Explicit machine design approval payload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project_revision: Annotated[int, Field(ge=0)]


class DesignRequest(BaseModel):
    """Explicit design/extension instruction payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    instruction: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("instruction")
    @classmethod
    def trim_instruction(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a design instruction before sending.")
        if len(value) > 2000:
            raise ValueError("Design instruction must contain at most 2000 characters.")
        return value


class RevisionRequest(BaseModel):
    """Revision-checked simple request payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]


@router.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/api/projects", status_code=201)
async def create_project() -> dict:
    project = api_hook("new_project", new_project)()
    _sessions[project["id"]] = ProjectSession(project=project)
    api_hook("_persist_session", persist_session)(_sessions[project["id"]])
    return {"project": deepcopy(project)}


@router.get("/api/models")
async def get_models(project_id: str | None = None) -> dict:
    return await model_options(
        api_hook("session_for", session_for)(project_id).model if project_id else None
    )


@router.get("/api/references")
async def get_reference_library() -> dict:
    return reference_library()


@router.get("/api/projects/{project_id}")
async def resume_project(project_id: str) -> dict:
    return await resume_session_project(project_id)


@router.put("/api/projects/{project_id}/notes")
async def edit_project_notes(project_id: str, edit: ProjectNotesEdit) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        existing = session.project.get("project_notes", "")
        notes = edit.notes.strip()
        if existing != edit.previous and existing != notes:
            raise HTTPException(status_code=409, detail={"notes": existing})
        if notes:
            session.project["project_notes"] = notes
        else:
            session.project.pop("project_notes", None)
    return {"project_id": project_id, "notes": notes}


@router.post("/api/projects/{project_id}/model")
async def select_project_model(project_id: str, request: ModelSelectionRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    if request.model not in AGENT_MODELS:
        raise HTTPException(status_code=422, detail="Choose a supported OpenAI model.")
    catalog_fn = api_hook("discover_catalog", discover_catalog)
    status = availability(await catalog_fn(), request.model)
    if not status["available"]:
        raise HTTPException(status_code=409, detail=status["unavailable_reason"])
    async with session.model_lock():
        session.model = request.model
        return {"project": deepcopy(session.project), "active_model": session.model}


@router.get("/api/projects/{project_id}/sources")
async def get_project_sources(project_id: str, revision: int) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        if revision != session.project["revision"]:
            raise HTTPException(
                status_code=409, detail="Download sources for the current revision."
            )
        payload = {
            "project_id": project_id,
            "revision": revision,
            "source_basis": project_source_basis(session.project),
        }
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="design-sources.json"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/projects/{project_id}/snapshot")
async def save_project(project_id: str, revision: int) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        if revision != session.project["revision"]:
            raise HTTPException(status_code=409, detail="Save the current project revision.")
        try:
            snapshot = make_snapshot(session.project, session.history)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    content = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    filename = f"autolab-{snapshot['project_id'][:8]}-r{revision}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/projects/open", status_code=201)
async def open_project(request: Request) -> dict:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise HTTPException(
                status_code=413, detail="The project file exceeds the 256 KB limit."
            )
    try:
        project, history = restore_snapshot(decode_snapshot(bytes(content)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session = ProjectSession(project=project, history=history)
    restored = project.get("restored_from", {})
    from app.price_estimation import price_context

    accept_fn = api_hook("_accept_project", accept_project)
    restored_scope = price_context(project)["input_scope_sha256"]
    for source in reversed(list(_sessions.values())):
        if (
            source.project.get("lineage_id", source.project["id"]) == restored.get("project_id")
            and source.project["revision"] == restored.get("revision")
            and price_context(source.project)["input_scope_sha256"] == restored_scope
        ):
            candidate = ProjectSession(
                project=source.project,
                procurement_report=deepcopy(source.procurement_report),
                cost_estimate=deepcopy(source.cost_estimate),
            )
            accept_fn(candidate, project)
            if candidate.procurement_report is not None or candidate.cost_estimate is not None:
                session.procurement_report = candidate.procurement_report
                session.cost_estimate = candidate.cost_estimate
                break
    _sessions[project["id"]] = session
    api_hook("_persist_session", persist_session)(session)
    return {
        "project": deepcopy(project),
        "conversation": deepcopy(history),
        "assistant_message": "Project reopened. Geometry was rebuilt from saved inputs.",
    }


@router.get("/api/projects/{project_id}/tip-rack-options")
async def get_tip_rack_options(project_id: str, revision: int) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        return tip_rack_options(session.project)


@router.post("/api/projects/{project_id}/tip-rack-options")
async def choose_tip_rack(project_id: str, request: TipRackChoiceRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        check_revision(session.project, request.revision)
        current = session.project
        literal = f"Select tip-rack profile {request.profile_id}"
        update = TipRackUpdate(profile_id=request.profile_id, source=literal)
        try:
            updated = update_tip_request(current, update, literal)
            updated, run = select_tip_rack(updated, update, literal, accept_placement=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{exc} The project was not changed."
            ) from None
        accept_fn(session, updated)
        session.history.extend(
            [{"role": "user", "content": literal}, {"role": "assistant", "content": run["message"]}]
        )
        session.history = session.history[-DISPLAY_HISTORY_LIMIT:]
        return {
            "project": deepcopy(updated),
            "assistant_message": run["message"],
            "action_runs": [run],
        }


@router.post("/api/projects/{project_id}/configuration")
async def update_configuration(project_id: str, request: ConfigurationRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        current = session.project
        try:
            updated = configure_project(current, **request.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{exc} The project was not changed."
            ) from None
        accept_fn(session, updated)
        message = configuration_message(updated)
        return {"project": deepcopy(updated), "assistant_message": message}


@router.post("/api/projects/{project_id}/functional-plan")
async def functional_plan(project_id: str, request: ProcurementRefreshRequest) -> dict:
    from app.functional_plan import propose_plan

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        project = session.project
        check_revision(project, request.project_revision)
        brief = project.get("brief") or {}
        if (
            project["workflow"]["kind"] != "general"
            or project.get("bench_station")
            or not (brief.get("objective") or brief.get("process"))
        ):
            raise HTTPException(409, "Describe a general laboratory process in chat first.")
        cached = session.functional_plan
        if (
            cached
            and cached["project_revision"] == project["revision"]
            and cached["model_request"] == session.model
        ):
            return deepcopy(cached)
        try:
            generated = await propose_plan(brief)
        except ModelClientError as exc:
            raise HTTPException(502, str(exc)) from None
        session.functional_plan = {
            **generated,
            "project_id": project_id,
            "project_revision": project["revision"],
            "model_request": session.model,
            "brief": deepcopy(brief),
        }
        return deepcopy(session.functional_plan)


@router.post(
    "/api/projects/{project_id}/concept",
    dependencies=[Depends(activity_dependency(session_for))],
)
async def generate_custom_project(project_id: str, request: ProcurementRefreshRequest) -> dict:
    from app.concept_agent import attach_concept, develop_concept

    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        project = session.project
        check_revision(project, request.project_revision)
        if project.get("concept_design"):
            return {"project": deepcopy(project), "assistant_message": "Your assembly is ready."}
        if (
            project["workflow"]["kind"] != "general"
            or project.get("bench_station")
            or not (project["brief"].get("objective") or project["brief"].get("process"))
        ):
            raise HTTPException(409, "Describe the custom equipment in a new general brief first.")
        instruction = "Design a custom laboratory assembly for my recorded brief."
        try:
            report_activity("assembling")
            compiled, reply, calls = await develop_concept(
                project, instruction, history=session.history
            )
        except ModelClientError as exc:
            raise HTTPException(502, str(exc)) from None
        if compiled is not None:
            project = attach_concept(project, deepcopy(project), compiled)
            accept_fn(session, project)
        session.history.extend(
            [{"role": "user", "content": instruction}, {"role": "assistant", "content": reply}]
        )
        session.history = session.history[-DISPLAY_HISTORY_LIMIT:]
        return {"project": deepcopy(project), "assistant_message": reply, "model_calls": calls}


@router.get("/api/projects/{project_id}/concept/{asset:path}")
async def download_custom_asset(project_id: str, asset: str, revision: int) -> Response:
    from starlette.concurrency import run_in_threadpool

    from app.concept_cad import ConceptCADUnavailable
    from app.concept_files import price_record_for_project, render_asset

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        design = session.project.get("concept_design")
        if design is None:
            raise HTTPException(404, "Generate a custom assembly first.")
        price_record = (
            price_record_for_project(
                session.project, session.procurement_report, session.cost_estimate
            )
            if asset in ("package.zip", "parts.csv")
            else None
        )
    try:
        data, mime = await run_in_threadpool(
            render_asset, design, project_id, revision, asset, price_record=price_record
        )
    except KeyError:
        raise HTTPException(404, "This file is unavailable for the current assembly.") from None
    except ConceptCADUnavailable:
        raise HTTPException(
            503,
            "STEP export requires the optional CAD installation. Install the project's "
            "CAD dependencies and restart the server.",
        ) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    name = asset.rsplit("/", 1)[-1]
    disposition = "inline" if asset == "guide.html" else "attachment"
    return Response(
        data,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/projects/{project_id}/brief")
async def get_brief(project_id: str, revision: int) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    describe_workflow_fn = api_hook("describe_workflow", describe_workflow)
    async with session.model_lock():
        current = deepcopy(session.project)
    current_revision = current.get("revision")
    if revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail=f"Revision {revision} is not current (expected {current_revision}).",
        )
    payload = {
        "project_id": current.get("id", project_id),
        "project_revision": current_revision,
        "status": current.get("status"),
        "brief": deepcopy(current.get("brief", {})),
        "workflow": describe_workflow_fn(current),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="project-brief.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/projects/{project_id}/approve-machine")
async def approve_machine_design(project_id: str, request: MachineApprovalRequest) -> dict:
    from app.design_workflow import approve_machine

    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated = approve_machine(session.project)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        accept_fn(session, updated)
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                "Machine approved. You can now define a preparation recipe and review software. "
                "This does not commission or run hardware."
            ),
        }


@router.post("/api/projects/{project_id}/program-recipe")
async def program_recipe(project_id: str, request: ProgramRecipeRequest) -> dict:
    from app.design_workflow import workflow_state

    session = api_hook("session_for", session_for)(project_id)
    apply_update_fn = api_hook("apply_update", apply_update)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        if not workflow_state(session.project)["machine_approved"]:
            raise HTTPException(409, "Approve the current machine before programming a recipe.")
        if session.project.get("workflow", {}).get("kind") != "liquid_preparation":
            raise HTTPException(
                409,
                "This recipe editor supports liquid preparation. "
                "Use this machine's own software guide for other workflows.",
            )
        try:
            updated = apply_update_fn(
                session.project, request.model_dump(exclude={"project_revision"})
            )
            if updated["plan"]["status"] != "ready":
                raise ValueError(updated["plan"]["message"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        updated["status"] = "confirmed"
        accept_fn(session, updated)
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                "Preparation recipe updated. Review the logical software preview before use."
            ),
        }


@router.post("/api/projects/{project_id}/confirm")
async def confirm(project_id: str) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        try:
            updated = confirm_project(session.project)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        accept_fn(session, updated)
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                "Preparation parameters confirmed. This does not run hardware."
            ),
        }


@router.post("/api/projects/{project_id}/design")
async def develop_layout(project_id: str, request: DesignRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    describe_workflow_fn = api_hook("describe_workflow", describe_workflow)
    execute_actions_fn = api_hook("execute_chat_actions", execute_chat_actions)
    develop_design_fn = api_hook("develop_design", develop_design)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        current = session.project
        if request.project_revision != current.get("revision"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Revision {request.project_revision} is not current "
                    f"(expected {current.get('revision')})."
                ),
            )
        workflow = describe_workflow_fn(current)
        if not isinstance(workflow, dict) or not workflow.get("design_available"):
            raise HTTPException(
                status_code=409,
                detail="Layout design is not available for this workflow.",
            )
        equipment = (
            current.get("summary", {}).get("equipment")
            or current.get("brief", {}).get("equipment")
        )
        if not has_workstation_design_context(current):
            raise HTTPException(status_code=409, detail=workstation_context_question(current))
        design = current.get("design", {}) if isinstance(current, dict) else {}
        if design.get("constraint_status") == "infeasible":
            raise HTTPException(
                status_code=409,
                detail="Layout design is not available for an infeasible width.",
            )
        try:
            if not isinstance(equipment, str) or not equipment.strip():
                updated, runs = await execute_actions_fn(
                    current,
                    [ChatAction(kind="develop_layout", source=request.instruction[:1000])],
                    request.instruction,
                )
                run = runs[0]
            else:
                updated, run = await develop_design_fn(deepcopy(current), request.instruction)
            if run.get("status") == "accepted" and updated.get("labware_rack"):
                updated = reapply_labware(updated)
            if run.get("status") == "accepted" and updated.get("labware_plate"):
                updated = reapply_plate(updated)
        except ValueError as exc:
            return {
                "project": deepcopy(current),
                "design_run": {"status": "rejected", "message": str(exc)},
                "assistant_message": str(exc),
            }
        except ModelClientError as exc:
            if hasattr(exc, "design_run"):
                return JSONResponse(
                    status_code=502,
                    content={"detail": str(exc), "design_run": exc.design_run},
                )
            raise HTTPException(status_code=502, detail=str(exc)) from None
        if run.get("status") == "accepted":
            accept_fn(session, updated)
            return {
                "project": deepcopy(updated),
                "design_run": deepcopy(run),
                "assistant_message": run.get("message", ""),
            }
        return {
            "project": deepcopy(current),
            "design_run": deepcopy(run),
            "assistant_message": run.get("message", ""),
        }


@router.post("/api/projects/{project_id}/inspection-extension")
async def add_inspection_extension(project_id: str, request: DesignRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        current = session.project
        if request.project_revision != current["revision"]:
            raise HTTPException(
                status_code=409, detail="The project changed. Use its current revision."
            )
        if not current.get("engineering_design"):
            raise HTTPException(status_code=409, detail="Develop a workstation layout first.")
        try:
            updated, run = await develop_inspection(deepcopy(current), request.instruction)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ModelClientError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": str(exc), "design_run": getattr(exc, "design_run", None)},
            )
        if run["status"] == "accepted":
            accept_fn(session, updated)
        return {
            "project": deepcopy(session.project),
            "design_run": run,
            "assistant_message": run["message"],
        }


@router.post("/api/projects/{project_id}/inspection-extension/remove")
async def remove_inspection_extension(project_id: str, request: RevisionRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        current = session.project
        if request.project_revision != current["revision"]:
            raise HTTPException(
                status_code=409, detail="The project changed. Use its current revision."
            )
        if current.get("inspection_extension"):
            updated = clear_extension(current)
            updated["revision"] += 1
            updated["status"] = "draft"
            accept_fn(session, updated)
        return {
            "project": deepcopy(session.project),
            "assistant_message": "Inspection module removed. Workstation layout preserved.",
        }


@router.get("/api/projects/{project_id}/bench-station/layout.svg")
async def bench_station_layout(project_id: str, revision: int) -> Response:
    from app.bench_station import station_svg

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        station = session.project.get("bench_station")
        if not station:
            raise HTTPException(status_code=404, detail="No bench station concept exists.")
        return Response(content=station_svg(station), media_type="image/svg+xml")


@router.get("/api/projects/{project_id}/bench-station/plan.json")
async def bench_station_plan(project_id: str, revision: int) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        station = session.project.get("bench_station")
        if not station:
            raise HTTPException(status_code=404, detail="No bench station concept exists.")
        return {"project_id": project_id, "revision": revision, **deepcopy(station)}


@router.get("/api/projects/{project_id}/bench-station/run.py")
async def bench_station_runner(project_id: str, revision: int) -> Response:
    from app import bench_station_runner as runner

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        if not session.project.get("bench_station"):
            raise HTTPException(status_code=404, detail="No bench station concept exists.")
        return Response(
            content=inspect.getsource(runner),
            media_type="text/x-python",
            headers={"Content-Disposition": 'attachment; filename="run.py"'},
        )

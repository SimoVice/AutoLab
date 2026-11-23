"""Parts, supplier catalog procurement, and cost estimation routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.model_client import using_model
from app.procurement import (
    refresh_procurement,
    report_csv,
    report_for_project,
    report_json,
)
from app.services.runtime import api_hook
from app.services.session_store import check_revision, session_for

router = APIRouter()


class ProcurementRefreshRequest(BaseModel):
    """Revision-checked procurement/build request payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: int = Field(strict=True, ge=0)


class CostEstimateRequest(ProcurementRefreshRequest):
    """Cost estimation request payload."""

    force: bool = Field(default=False, strict=True)


async def _refresh_procurement_connected(request: Request, operation) -> dict:
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            if await request.is_disconnected():
                raise HTTPException(status_code=499, detail="Catalog refresh was cancelled.")
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@router.get("/api/projects/{project_id}/parts")
async def get_parts_proposal(project_id: str, revision: int) -> dict:
    from app.parts_proposal import build_parts_proposal

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        return build_parts_proposal(
            session.project, session.procurement_report, session.cost_estimate
        )


@router.get("/api/projects/{project_id}/parts/{component_id}/model.{format}")
async def get_part_model(project_id: str, component_id: str, format: str, revision: int):
    from starlette.concurrency import run_in_threadpool

    from app.parts_proposal import render_part_model

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        project = deepcopy(session.project)
    try:
        data = await run_in_threadpool(render_part_model, project, component_id, format)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{component_id}-r{revision}.{format}"'
        },
    )


@router.get("/api/projects/{project_id}/procurement")
async def get_procurement(project_id: str, revision: int) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = report_for_project(session.project, session.procurement_report)
        state = (
            "current"
            if current
            else ("stale_revision" if session.procurement_report else "not_refreshed")
        )
        return {"report": current, "state": state}


@router.get("/api/projects/{project_id}/cost-estimate")
async def get_cost_estimate(project_id: str, revision: int) -> dict:
    from app.price_estimation import estimate_for_project, unestimated_cost

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        estimate = estimate_for_project(
            session.project, session.cost_estimate, session.procurement_report
        )
        current = bool(
            estimate
            and estimate.get("status") != "stale"
            and (not estimate.get("pending_scope_group_ids") or estimate.get("retry_required"))
        )
        return {
            "estimate": estimate
            or unestimated_cost(session.project, session.procurement_report),
            "state": (
                "current"
                if current
                else (
                    "needs_estimate"
                    if estimate and estimate.get("pending_scope_group_ids")
                    else "not_estimated"
                )
            ),
        }


@router.post("/api/projects/{project_id}/cost-estimate")
async def refresh_cost_estimate(
    project_id: str, payload: CostEstimateRequest, request: Request
) -> dict:
    from app.price_estimation import estimate_for_project, estimate_project_prices

    session = api_hook("session_for", session_for)(project_id)

    async def operation():
        async with session.estimate_lock:
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                if not any(
                    session.project.get(k)
                    for k in ("engineering_design", "bench_station", "concept_design")
                ):
                    raise HTTPException(
                        status_code=409, detail="Develop a design before estimating its cost."
                    )
                cached = estimate_for_project(
                    session.project, session.cost_estimate, session.procurement_report
                )
                if (
                    not payload.force
                    and cached
                    and cached.get("status") in ("ready", "partial")
                    and not cached.get("pending_scope_group_ids")
                ):
                    return {"estimate": cached, "state": "current"}
                project = deepcopy(session.project)
                report = deepcopy(session.procurement_report)
                model = session.model
            with using_model(model):
                if cached and (not payload.force or cached.get("retry_required")):
                    estimate = await estimate_project_prices(project, report, previous=cached)
                else:
                    estimate = await estimate_project_prices(project, report)
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                if session.procurement_report != report:
                    raise HTTPException(
                        status_code=409, detail="Supplier prices changed. Refresh the estimate."
                    )
                session.cost_estimate = estimate
                return {"estimate": deepcopy(estimate), "state": "current"}

    connected = api_hook("_refresh_procurement_connected", _refresh_procurement_connected)
    return await connected(request, operation())


@router.post("/api/projects/{project_id}/procurement/refresh")
async def refresh_project_procurement(
    project_id: str, payload: ProcurementRefreshRequest, request: Request
) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    refresh_fn = api_hook("refresh_procurement", refresh_procurement)

    async def operation():
        async with session.procurement_lock:
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                if not any(
                    session.project.get(k)
                    for k in ("engineering_design", "bench_station", "concept_design")
                ):
                    raise HTTPException(
                        status_code=409, detail="Develop a machine before refreshing its parts."
                    )
                project = deepcopy(session.project)
            result = await refresh_fn(project)
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                session.procurement_report = result
                return {"report": deepcopy(result), "state": "current"}

    connected = api_hook("_refresh_procurement_connected", _refresh_procurement_connected)
    return await connected(request, operation())


@router.get("/api/projects/{project_id}/procurement/download")
async def download_procurement(
    project_id: str, revision: int, report_id: str, format: str = "json"
) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        report = report_for_project(session.project, session.procurement_report)
        if report is None or report["report_id"] != report_id:
            raise HTTPException(status_code=409, detail="Refresh and download the current report.")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=422, detail="Choose JSON or CSV for the catalog report.")
    body = report_json(report) if format == "json" else report_csv(report)
    return Response(
        content=body,
        media_type="application/json" if format == "json" else "text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="procurement-r{revision}.{format}"',
            "Cache-Control": "no-store",
        },
    )

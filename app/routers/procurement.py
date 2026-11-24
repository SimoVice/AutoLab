"""Parts, supplier catalog procurement, and cost estimation routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.bom_engine import bom_to_csv, bom_to_json, build_complete_bom, purchasing_route
from app.hardware_compatibility import evaluate_hardware_compatibility
from app.lab_profile import has_lab_profile_data, load_lab_profile
from app.procurement import (
    refresh_procurement,
    report_csv,
    report_for_project,
    report_json,
)
from app.services.runtime import api_hook
from app.services.session_store import check_revision, session_for
from app.supplier_apis import (
    SupplierApiError,
    fetch_fx_rates,
    fetch_official_quote,
    search_official_suppliers,
    supplier_api_capabilities,
)
from app.supplier_catalogs import PRODUCTS

router = APIRouter()

_DESIGN_KEYS = (
    "engineering_design",
    "bench_station",
    "concept_design",
    "standalone_fixture",
    "upstream_project",
)


class ProcurementRefreshRequest(BaseModel):
    """Revision-checked procurement/build request payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: int = Field(strict=True, ge=0)
    currency: Literal["USD", "CHF", "EUR"] = "USD"
    use_official_apis: bool = False


class BomRefreshRequest(BaseModel):
    """Explicit complete-BOM live API & FX refresh request."""

    model_config = ConfigDict(extra="forbid")
    project_revision: int = Field(strict=True, ge=0)
    currency: Literal["USD", "CHF", "EUR"] = "USD"
    use_official_apis: bool = True


class SupplierSearchRequest(BaseModel):
    """Official supplier catalog & local reference search request."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=160)
    suppliers: list[Literal["digikey", "mouser"]] | None = None
    currency: Literal["USD", "CHF", "EUR"] = "USD"
    max_results: int = Field(default=6, strict=True, ge=1, le=12)


class CostEstimateRequest(ProcurementRefreshRequest):
    """Cost estimation request payload."""

    force: bool = Field(default=False, strict=True)


def _active_lab_profile() -> dict[str, Any] | None:
    prof = load_lab_profile()
    return prof if has_lab_profile_data(prof) else None


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
    from app.price_estimation import purchase_cost_report

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        return {
            "estimate": purchase_cost_report(
                session.project, session.procurement_report, lab_profile=_active_lab_profile()
            ),
            "state": "current",
        }


@router.post("/api/projects/{project_id}/cost-estimate")
async def refresh_cost_estimate(project_id: str, payload: CostEstimateRequest) -> dict:
    # Retain the revision-checked refresh entry point for saved clients.
    result = await get_cost_estimate(project_id, payload.project_revision)
    if result["estimate"]["status"] == "design_required":
        raise HTTPException(status_code=409, detail="Develop a design before estimating its cost.")
    return result


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
                if not any(session.project.get(k) for k in _DESIGN_KEYS):
                    raise HTTPException(
                        status_code=409, detail="Develop a machine before refreshing its parts."
                    )
                project = deepcopy(session.project)
            if payload.currency == "USD" and not payload.use_official_apis:
                result = await refresh_fn(project)
            else:
                result = await refresh_fn(
                    project,
                    currency=payload.currency,
                    use_official_apis=payload.use_official_apis,
                )
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                session.procurement_report = result
                return {"report": deepcopy(result), "state": "current"}

    connected = api_hook("_refresh_procurement_connected", _refresh_procurement_connected)
    return await connected(request, operation())


@router.get("/api/projects/{project_id}/bom")
async def get_purchasable_bom(
    project_id: str,
    revision: int,
    currency: Literal["USD", "CHF", "EUR"] = "USD",
) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        lab_profile = _active_lab_profile()
        report = report_for_project(session.project, session.procurement_report)
        try:
            bom = build_complete_bom(
                session.project,
                procurement_report=report,
                lab_profile=lab_profile,
                currency=currency,
            )
        except SupplierApiError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        compatibility = evaluate_hardware_compatibility(
            session.project, bom, lab_profile=lab_profile
        )
        return {
            "bom": bom,
            "compatibility": compatibility,
            "supplier_api_status": supplier_api_capabilities(),
        }


@router.post("/api/projects/{project_id}/bom/refresh")
async def refresh_purchasable_bom(
    project_id: str, payload: BomRefreshRequest, request: Request
) -> dict:
    session = api_hook("session_for", session_for)(project_id)

    async def operation():
        async with session.procurement_lock:
            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                project = deepcopy(session.project)
                lab_profile = _active_lab_profile()
            fx_rates = None
            warnings: list[str] = []
            if payload.currency != "USD" and purchasing_route(project) != "unselected":
                try:
                    fx_rates = await fetch_fx_rates("USD")
                except SupplierApiError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from None

            base_bom = build_complete_bom(project, lab_profile=lab_profile, currency="USD")
            live_quotes: dict[str, dict[str, Any]] = {}
            if payload.use_official_apis:
                caps = supplier_api_capabilities()
                dk_or_ms = caps["digikey_v4"]["configured"] or caps["mouser_v2"]["configured"]
                sem = asyncio.Semaphore(4)

                async def _fetch_line(line: dict[str, Any]) -> None:
                    cid = line.get("offer_id") or line.get("catalog_id")
                    if (
                        not cid
                        or line.get("kind") != "commercial_part"
                        or line.get("purchase_quantity") == 0
                    ):
                        return
                    is_shopify = cid in PRODUCTS and PRODUCTS[cid].adapter == "shopify"
                    has_api_pn = bool(line.get("digikey_pn") or line.get("mouser_pn"))
                    if not is_shopify and not (dk_or_ms and has_api_pn) and cid not in PRODUCTS:
                        return
                    async with sem:
                        try:
                            quote_obj = await fetch_official_quote(
                                {
                                    "catalog_id": cid,
                                    "option_id": cid,
                                    "mpn": line.get("mpn") if line.get("catalog_id") else None,
                                    "digikey_pn": line.get("digikey_pn"),
                                    "mouser_pn": line.get("mouser_pn"),
                                    "sku": line.get("sku"),
                                },
                                currency=payload.currency,
                                fx_rates=fx_rates,
                            )
                            live_quotes[str(cid)] = quote_obj
                        except SupplierApiError as exc:
                            warnings.append(f"{cid}: {exc}")

                await asyncio.gather(*(_fetch_line(line) for line in base_bom.get("lines") or []))

            async with session.model_lock():
                check_revision(session.project, payload.project_revision)
                report = report_for_project(session.project, session.procurement_report)
                bom = build_complete_bom(
                    session.project,
                    procurement_report=report,
                    lab_profile=lab_profile,
                    currency=payload.currency,
                    fx_rates=fx_rates,
                    live_quotes=live_quotes,
                )
                compatibility = evaluate_hardware_compatibility(
                    session.project, bom, lab_profile=lab_profile
                )
                return {
                    "bom": bom,
                    "compatibility": compatibility,
                    "supplier_api_status": supplier_api_capabilities(),
                    "live_quote_warnings": warnings[:12],
                }

    connected = api_hook("_refresh_procurement_connected", _refresh_procurement_connected)
    return await connected(request, operation())


@router.get("/api/projects/{project_id}/bom/download")
async def download_purchasable_bom(
    project_id: str,
    revision: int,
    format: Literal["csv", "json"] = "csv",
    currency: Literal["USD", "CHF", "EUR"] = "USD",
) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        lab_profile = _active_lab_profile()
        report = report_for_project(session.project, session.procurement_report)
        try:
            bom = build_complete_bom(
                session.project,
                procurement_report=report,
                lab_profile=lab_profile,
                currency=currency,
            )
        except SupplierApiError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    body = bom_to_csv(bom) if format == "csv" else bom_to_json(bom)
    return Response(
        content=body,
        media_type="text/csv" if format == "csv" else "application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="bom-r{revision}-{currency.lower()}.{format}"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/projects/{project_id}/compatibility")
async def get_hardware_compatibility(project_id: str, revision: int) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        lab_profile = _active_lab_profile()
        report = report_for_project(session.project, session.procurement_report)
        bom = build_complete_bom(
            session.project, procurement_report=report, lab_profile=lab_profile
        )
        return evaluate_hardware_compatibility(session.project, bom, lab_profile=lab_profile)


@router.post("/api/suppliers/search")
async def search_suppliers_route(payload: SupplierSearchRequest) -> dict:
    try:
        return await search_official_suppliers(
            payload.query,
            suppliers=payload.suppliers,
            currency=payload.currency,
            max_results=payload.max_results,
        )
    except SupplierApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


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

"""Standalone printable fixture, dimensioned drawing, and build-feedback routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.build_feedback import append_build_log, apply_build_log_revision, compute_reprint_diff
from app.fixture_adapter import attach_standalone_fixture, list_fixture_references
from app.fixture_cad import build_fixture_cad_files
from app.fixture_drawings import (
    build_fixture_package_zip,
    render_bench_build_sheet_html,
    render_bench_build_sheet_md,
    render_e13_checklist_md,
    render_fixture_drawing_pdf,
    render_fixture_drawing_svg,
    render_labware_drawing_pdf,
    render_labware_drawing_svg,
)
from app.lab_profile import has_lab_profile_data, load_lab_profile
from app.services.runtime import api_hook
from app.services.session_store import accept_project, check_revision, session_for

router = APIRouter()


class FixtureCreateRequest(BaseModel):
    """Revision-checked standalone fixture configuration payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    spec: dict[str, Any]


class BuildLogHttpRequest(BaseModel):
    """Revision-checked physical build log submission payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    log: dict[str, Any]


class BuildRevisionRequest(BaseModel):
    """Revision-checked tolerance update trigger from a recorded build log."""

    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    clearance_delta_mm: float | None = Field(default=None, ge=-1.5, le=1.5)
    fdm_undersize_delta_mm: float | None = Field(default=None, ge=-1.0, le=1.0)


@router.get("/api/fixture-references")
async def get_fixture_references() -> dict[str, Any]:
    return {"references": list_fixture_references()}


@router.post("/api/projects/{project_id}/fixture")
async def configure_project_fixture(project_id: str, request: FixtureCreateRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    profile = load_lab_profile()
    lab_profile = profile if has_lab_profile_data(profile) else None
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated = attach_standalone_fixture(
                session.project, request.spec, lab_profile=lab_profile
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        accept_fn(session, updated)
        title = updated["standalone_fixture"]["spec"]["title"]
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                f"Configured standalone printable fixture '{title}' "
                f"(revision {updated['revision']})."
            ),
        }


@router.get("/api/projects/{project_id}/fixture/download/{asset:path}")
async def download_fixture_asset(
    project_id: str,
    asset: str,
    revision: int,
    parts: str | None = Query(default=None),
) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = deepcopy(session.project)
    fixture = current.get("standalone_fixture")
    if not fixture:
        raise HTTPException(status_code=404, detail="No standalone fixture exists in this project.")

    valid_ids = {p["id"] for p in fixture["parts"]}
    selected: list[str] | None = None
    if parts is not None and parts.strip():
        selected = [p.strip() for p in parts.split(",") if p.strip()]
        invalid = [p for p in selected if p not in valid_ids]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown fixture part(s): {', '.join(invalid)}.",
            )

    loop = asyncio.get_running_loop()
    normalized_asset = asset.lstrip("/")
    if normalized_asset in ("tolerance-coupon.stl", "tolerance-coupon.step"):
        normalized_asset = f"parts/{normalized_asset}"

    if normalized_asset == "package.zip":
        payload = await loop.run_in_executor(
            None, build_fixture_package_zip, current, selected
        )
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="autolab-fixture-r{revision}.zip"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "drawing.svg":
        return Response(
            content=render_fixture_drawing_svg(fixture, revision),
            media_type="image/svg+xml",
            headers={
                "Content-Disposition": f'attachment; filename="fixture-r{revision}.svg"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "drawing.pdf":
        return Response(
            content=render_fixture_drawing_pdf(fixture, revision),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="fixture-r{revision}.pdf"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "build-sheet.html":
        return Response(
            content=render_bench_build_sheet_html(fixture, current, revision),
            media_type="text/html",
            headers={
                "Content-Disposition": f'attachment; filename="build-sheet-r{revision}.html"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "build-sheet.md":
        return Response(
            content=render_bench_build_sheet_md(fixture, revision),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="build-sheet-r{revision}.md"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "e13-bench-checklist.md":
        return Response(
            content=render_e13_checklist_md(fixture, revision),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="e13-checklist-r{revision}.md"',
                "Cache-Control": "no-store",
            },
        )
    if normalized_asset == "assembly.step":
        cad_files = await loop.run_in_executor(None, build_fixture_cad_files, fixture, None)
        if "cad/assembly.step" not in cad_files:
            raise HTTPException(status_code=404, detail="Assembly STEP is unavailable.")
        return Response(
            content=cad_files["cad/assembly.step"],
            media_type="model/step",
            headers={
                "Content-Disposition": f'attachment; filename="fixture-assembly-r{revision}.step"',
                "Cache-Control": "no-store",
            },
        )

    part_cad_names = {f"parts/{pid}.stl": (pid, "model/stl") for pid in valid_ids}
    part_cad_names.update({f"parts/{pid}.step": (pid, "model/step") for pid in valid_ids})
    if normalized_asset in part_cad_names:
        pid, mime = part_cad_names[normalized_asset]
        cad_files = await loop.run_in_executor(None, build_fixture_cad_files, fixture, [pid])
        filename = normalized_asset.rsplit("/", 1)[-1]
        return Response(
            content=cad_files[normalized_asset],
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    raise HTTPException(status_code=404, detail="Requested fixture asset was not found.")


@router.get("/api/projects/{project_id}/labware/drawing.{ext}")
async def download_labware_drawing(project_id: str, ext: str, revision: int) -> Response:
    if ext not in ("svg", "pdf"):
        raise HTTPException(status_code=422, detail="Choose 'svg' or 'pdf'.")
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = deepcopy(session.project)
    try:
        payload = (
            render_labware_drawing_svg(current)
            if ext == "svg"
            else render_labware_drawing_pdf(current)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    mime = "image/svg+xml" if ext == "svg" else "application/pdf"
    return Response(
        content=payload,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="labware-r{revision}.{ext}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/projects/{project_id}/build-logs")
async def get_build_logs(project_id: str) -> dict[str, Any]:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = session.project
        return {
            "project_id": project_id,
            "revision": current["revision"],
            "build_logs": deepcopy(current.get("build_logs") or []),
            "build_feedback": deepcopy(
                current.get("build_feedback") or compute_reprint_diff(current)
            ),
        }


@router.post("/api/projects/{project_id}/build-logs", status_code=201)
async def create_build_log(project_id: str, request: BuildLogHttpRequest) -> dict[str, Any]:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated, entry = append_build_log(session.project, request.log)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        accept_fn(session, updated)
        return {"project": deepcopy(updated), "log": entry}


@router.post("/api/projects/{project_id}/build-logs/{log_id}/revise")
async def revise_from_build_log(
    project_id: str,
    log_id: str,
    request: BuildRevisionRequest,
) -> dict[str, Any]:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    profile = load_lab_profile()
    lab_profile = profile if has_lab_profile_data(profile) else None
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated, summary = apply_build_log_revision(
                session.project,
                log_id,
                clearance_delta_mm=request.clearance_delta_mm,
                fdm_undersize_delta_mm=request.fdm_undersize_delta_mm,
                lab_profile=lab_profile,
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        accept_fn(session, updated)
        reprint = ", ".join(summary["parts_to_reprint"]) or "none"
        return {
            "project": deepcopy(updated),
            "revision_summary": summary,
            "assistant_message": (
                f"Applied physical build feedback from {log_id} -> revision {updated['revision']}. "
                f"Reprint required: {reprint}."
            ),
        }

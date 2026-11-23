"""Assembly instructions and asynchronous CAD/package export routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.procurement import project_with_report
from app.project_exports import (
    ExportError,
    export_archive,
    export_file,
    export_status,
    media_type_for,
    start_export,
)
from app.services.runtime import api_hook
from app.services.session_store import check_revision, session_for

router = APIRouter()


def _export_error_response(exc: ExportError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/api/projects/{project_id}/instructions")
async def get_instructions(project_id: str, revision: int) -> dict:
    from app.instructions import build_instruction_guide

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
    return build_instruction_guide(current)


@router.get("/api/projects/{project_id}/instructions/download")
async def download_instructions(
    project_id: str,
    revision: int,
    format: str = "html",
    report_id: str | None = None,
) -> Response:
    from app.instructions import build_instruction_files

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
        if (
            report_id is not None
            and current.get("procurement_report", {}).get("report_id") != report_id
        ):
            raise HTTPException(
                status_code=409, detail="Download instructions for the current report."
            )
    names = {
        "html": "assembly-guide.html",
        "md": "assembly-guide.md",
        "json": "assembly.json",
        "protocol": "protocol-template.json",
        "preview": "protocol-preview.py",
    }
    if format == "program":
        from app.program_source import build_program

        try:
            program = build_program(current)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return Response(
            program,
            media_type="text/x-python",
            headers={
                "Content-Disposition": 'attachment; filename="program.py"',
                "Cache-Control": "no-store",
            },
        )
    if format not in names:
        raise HTTPException(
            status_code=422,
            detail="Choose HTML, Markdown, JSON instructions or a protocol template/local preview.",
        )
    name = names[format]
    return Response(
        content=build_instruction_files(current)[name],
        media_type=media_type_for(name),
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/projects/{project_id}/exports", status_code=202)
async def create_export(project_id: str) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
    try:
        return await api_hook("start_export", start_export)(current)
    except ExportError as exc:
        raise _export_error_response(exc) from None


@router.get("/api/projects/{project_id}/exports/{job_id}")
async def get_export(project_id: str, job_id: str) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
    try:
        return await export_status(current, job_id)
    except ExportError as exc:
        raise _export_error_response(exc) from None


@router.get("/api/projects/{project_id}/exports/{job_id}/download")
async def download_export(project_id: str, job_id: str) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
    try:
        payload = await export_archive(current, job_id)
        meta = await export_status(current, job_id)
    except ExportError as exc:
        raise _export_error_response(exc) from None
    filename = f"autolab-{meta['project_id']}-r{meta['revision']}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/projects/{project_id}/exports/{job_id}/files/{name:path}")
async def download_export_file(project_id: str, job_id: str, name: str) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = project_with_report(
            session.project, session.procurement_report, session.cost_estimate
        )
    try:
        payload = await export_file(current, job_id, name)
    except ExportError as exc:
        raise _export_error_response(exc) from None
    safe = name.rsplit("/", 1)[-1]
    return Response(
        content=payload,
        media_type=media_type_for(name),
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )

"""Serve issued assembly files through the normal project session and revision lock."""

from io import BytesIO
from zipfile import ZipFile

from fastapi import HTTPException, Response
from starlette.concurrency import run_in_threadpool

from app.cad_worker import CadWorkerError
from app.mixed_handoff import OUTPUTS, build_package, keep_package, read_issued, snapshot_key
from app.procurement import report_for_project
from app.project_storage import make_snapshot
from app.routers.procurement import _active_lab_profile


async def issue(session, asset, revision):
    from app.routers.projects import check_revision

    if asset not in OUTPUTS | {"package.zip", "manifest.json"}:
        raise HTTPException(404, "Unknown assembly deliverable.")
    async with session.model_lock():
        check_revision(session.project, revision)
        profile = _active_lab_profile()
        report = report_for_project(session.project, session.procurement_report)
        try:
            snapshot = make_snapshot(session.project, session.history)
            key = snapshot_key(snapshot, profile, report)
            raw = await run_in_threadpool(read_issued, session.issued_handoff, key)
            if raw is None:
                raw = await build_package(session.project, snapshot, profile, report)
                session.issued_handoff = await run_in_threadpool(keep_package, raw, key)
        except (ValueError, CadWorkerError) as exc:
            raise HTTPException(422, str(exc)) from None
    if asset == "package.zip":
        name = f"autolab-assembly-r{revision}.zip"
        mime = "application/zip"
    else:
        with ZipFile(BytesIO(raw)) as archive:
            raw = archive.read(asset)
        name = asset.rsplit("/", 1)[-1]
        mime = {
            "json": "application/json",
            "step": "model/step",
            "stl": "model/stl",
            "csv": "text/csv",
            "md": "text/markdown",
        }[name.rsplit(".", 1)[-1]]
    return Response(
        raw,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

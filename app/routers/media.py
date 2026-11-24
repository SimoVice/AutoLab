"""Voice transcription, design illustration, and labware photo inspection routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.illustration import (
    MAX_REFERENCE_DATA_URL,
    MAX_REQUEST_BYTES,
    IllustrationError,
    generate_illustration,
    prepare_reference,
)
from app.model_catalog import IMAGE_MODEL, availability, discover_catalog
from app.services.runtime import api_hook
from app.services.session_store import session_for
from app.transcription import MAX_AUDIO_BYTES, TranscriptionError, transcribe_audio
from app.vision import Box, VisionError
from app.vision_jobs import (
    InspectionError,
    inspection_archive,
    inspection_image,
    inspection_status,
    review_inspection,
    start_inspection,
)

router = APIRouter()


class IllustrationRequest(BaseModel):
    """Scene illustration payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: int = Field(ge=1, strict=True)
    reference_image_data_url: str = Field(
        min_length=1, max_length=MAX_REFERENCE_DATA_URL, strict=True
    )


class StepIllustrationRequest(BaseModel):
    """Assembly instruction step illustration payload."""

    model_config = ConfigDict(extra="forbid")
    project_revision: int = Field(ge=1, strict=True)
    step_id: str = Field(min_length=1, max_length=160, strict=True)


class InspectionCreateRequest(BaseModel):
    """Labware inspection upload payload."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    project_revision: Annotated[int, Field(strict=True)]
    image_data_url: Annotated[str, Field(min_length=1, max_length=5_592_472)]


class ReviewDecisionModel(BaseModel):
    """Human review decision on a detected inspection box."""

    model_config = ConfigDict(extra="forbid")
    id: str
    action: str
    kind: str | None = None
    label: str | None = None
    box: Box | None = None
    component_id: str | None = None
    tip_rack_contents: str | None = None


class ReviewAdditionModel(BaseModel):
    """Human-added inspection annotation."""

    model_config = ConfigDict(extra="forbid")
    kind: str
    label: str
    box: Box
    component_id: str | None = None
    tip_rack_contents: str | None = None


class InspectionReviewRequest(BaseModel):
    """Complete inspection review payload."""

    model_config = ConfigDict(extra="forbid")
    observation_version: Annotated[int, Field(strict=True)]
    scene_reviewed: Annotated[bool, Field(strict=True)]
    decisions: list[ReviewDecisionModel] = Field(default_factory=list)
    additions: list[ReviewAdditionModel] = Field(default_factory=list)


def _inspection_error_response(exc: InspectionError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _transcribe_connected(request: Request, content: bytes) -> dict:
    """Cancel the upstream request when the browser abandons this recording."""
    transcribe_fn = api_hook("transcribe_audio", transcribe_audio)
    task = asyncio.create_task(transcribe_fn(content))
    try:
        while not task.done():
            if await request.is_disconnected():
                raise HTTPException(status_code=499, detail="Dictation was cancelled.")
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _check_illustration_project(project: dict, revision: int) -> None:
    if project["revision"] != revision:
        raise HTTPException(
            status_code=409, detail="The project changed. Capture its current view."
        )
    if not project.get("engineering_design"):
        raise HTTPException(
            status_code=409, detail="Develop a machine before creating an illustration."
        )


async def _illustrate_connected(
    request: Request, project: dict, reference, *, prompt=None
) -> dict:
    illustrate_fn = api_hook("generate_illustration", generate_illustration)
    options = {"prompt": prompt} if prompt is not None else {}
    task = asyncio.create_task(illustrate_fn(project, reference, **options))
    try:
        while not task.done():
            if await request.is_disconnected():
                raise HTTPException(status_code=499, detail="Illustration was cancelled.")
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@router.post("/api/transcribe")
async def transcribe_recording(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/wav":
        raise HTTPException(status_code=415, detail="Send a recorded PCM WAV clip as audio/wav.")
    content = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_AUDIO_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Record at most 60 seconds of audio."
                    )
                content.extend(chunk)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="Audio upload timed out. Record a shorter clip."
        )
    try:
        return await _transcribe_connected(request, bytes(content))
    except TranscriptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.post("/api/projects/{project_id}/illustration")
async def illustrate_project(project_id: str, request: Request) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    max_bytes = api_hook("MAX_REQUEST_BYTES", MAX_REQUEST_BYTES)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Send the project view as a JSON request.")
    content = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(content) + len(chunk) > max_bytes:
                    raise HTTPException(status_code=413, detail="The reference image is too large.")
                content.extend(chunk)
        data = IllustrationRequest.model_validate_json(content)
    except TimeoutError:
        raise HTTPException(
            status_code=408, detail="The reference image upload timed out."
        ) from None
    except ValidationError:
        raise HTTPException(
            status_code=422, detail="Send the current revision and a PNG or JPEG view."
        ) from None
    async with session.model_lock():
        _check_illustration_project(session.project, data.project_revision)
        project = deepcopy(session.project)
    try:
        reference = await asyncio.to_thread(prepare_reference, data.reference_image_data_url)
        catalog_fn = api_hook("discover_catalog", discover_catalog)
        status = availability(await catalog_fn(), IMAGE_MODEL)
        if not status["available"]:
            raise HTTPException(status_code=503, detail=status["unavailable_reason"])
        async with session.model_lock():
            _check_illustration_project(session.project, data.project_revision)
        illustrate_connected = api_hook("_illustrate_connected", _illustrate_connected)
        result = await illustrate_connected(request, project, reference)
    except IllustrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    async with session.model_lock():
        _check_illustration_project(session.project, data.project_revision)
    return result


@router.post("/api/projects/{project_id}/instructions/illustration")
async def illustrate_instruction_step(project_id: str, request: Request) -> dict:
    from app.step_illustration import (
        STEP_SCOPE,
        schematic_reference,
        step_context,
        step_prompt,
    )

    session = api_hook("session_for", session_for)(project_id)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "Send the revision and assembly step as JSON.")
    content = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(content) + len(chunk) > 2048:
                    raise HTTPException(413, "The assembly image request is too large.")
                content.extend(chunk)
        data = StepIllustrationRequest.model_validate_json(content)
    except TimeoutError:
        raise HTTPException(408, "The assembly image request timed out.") from None
    except ValidationError:
        raise HTTPException(
            422, "Send only the current revision and assembly step identity."
        ) from None
    try:
        async with session.model_lock():
            project = deepcopy(session.project)
            if project["revision"] != data.project_revision:
                raise HTTPException(409, "The project changed. Open its current assembly guide.")
            context = step_context(project, data.step_id)
        catalog_fn = api_hook("discover_catalog", discover_catalog)
        status = availability(await catalog_fn(), IMAGE_MODEL)
        if not status["available"]:
            raise HTTPException(503, status["unavailable_reason"])
        reference = await asyncio.to_thread(schematic_reference, context)
        async with session.model_lock():
            if session.project["revision"] != data.project_revision:
                raise HTTPException(409, "The project changed. Open its current assembly guide.")
        illustrate_connected = api_hook("_illustrate_connected", _illustrate_connected)
        result = await illustrate_connected(
            request, project, reference, prompt=step_prompt(context)
        )
        async with session.model_lock():
            if session.project["revision"] != data.project_revision:
                raise HTTPException(409, "The project changed. Discard the earlier step image.")
        return {
            **result,
            "step_id": data.step_id,
            "step_title": context["step_title"],
            "component_ids": context["component_ids"],
            "scope": STEP_SCOPE,
            "input_kind": context["input_kind"],
            "sources": context["sources"],
            "instruction_source": {
                "kind": "current_project_instruction",
                "project_id": project_id,
                "project_revision": data.project_revision,
                "step_id": data.step_id,
            },
            "reference_scope": (
                "Text-only graphic from this revision's authoritative title and actions; "
                "no equipment geometry supplied."
                if context["input_kind"] == "instruction_checklist"
                else "Canonical schematic primitives, including actual holes and orientation "
                "markers where present; not detailed source CAD."
                if context["input_kind"] == "canonical_primitives"
                else "Schematic canonical component envelopes; not detailed source CAD."
            ),
        }
    except IllustrationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/api/projects/{project_id}/inspections", status_code=202)
async def create_inspection(project_id: str, request: InspectionCreateRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = deepcopy(session.project)
        try:
            return await start_inspection(
                current,
                project_revision=request.project_revision,
                image_data_url=request.image_data_url,
            )
        except InspectionError as exc:
            raise _inspection_error_response(exc) from None


@router.get("/api/projects/{project_id}/inspections/{job_id}")
async def get_inspection(project_id: str, job_id: str) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = deepcopy(session.project)
    try:
        return await inspection_status(current, job_id)
    except InspectionError as exc:
        raise _inspection_error_response(exc) from None


@router.get("/api/projects/{project_id}/inspections/{job_id}/image")
async def get_inspection_image(project_id: str, job_id: str) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = deepcopy(session.project)
    try:
        payload, _, _ = await inspection_image(current, job_id)
    except InspectionError as exc:
        raise _inspection_error_response(exc) from None
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/projects/{project_id}/inspections/{job_id}/review")
async def review_inspection_job(
    project_id: str, job_id: str, request: InspectionReviewRequest
) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = deepcopy(session.project)
    try:
        return await review_inspection(current, job_id, request.model_dump(exclude_unset=True))
    except InspectionError as exc:
        raise _inspection_error_response(exc) from None


@router.get("/api/projects/{project_id}/inspections/{job_id}/archive")
async def download_inspection_archive(
    project_id: str, job_id: str, observation_version: int | None = None
) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        current = deepcopy(session.project)
    try:
        payload, filename = await inspection_archive(current, job_id, observation_version)
    except InspectionError as exc:
        raise _inspection_error_response(exc) from None
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class CameraCalibrationRequest(BaseModel):
    """Fiducial planar homography calibration request."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    image_data_url: Annotated[str, Field(min_length=1, max_length=5_592_472)]
    board_width_mm: Annotated[float, Field(ge=40.0, le=1000.0)] = 160.0
    board_height_mm: Annotated[float, Field(ge=40.0, le=1000.0)] = 110.0
    marker_size_mm: Annotated[float, Field(ge=5.0, le=150.0)] = 20.0
    manual_corners_px: list[list[float]] | None = None


@router.get("/api/cameras")
async def get_local_cameras() -> dict:
    from app.camera_calibration import list_local_cameras

    return list_local_cameras()


@router.get("/api/cameras/calibration-board.svg")
async def get_calibration_board_svg(
    width_mm: float = 160.0,
    height_mm: float = 110.0,
    marker_size_mm: float = 20.0,
) -> Response:
    from app.camera_calibration import generate_calibration_board_svg

    try:
        svg = generate_calibration_board_svg(width_mm, height_mm, marker_size_mm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Content-Disposition": 'attachment; filename="calibration-board.svg"'},
    )


@router.post("/api/projects/{project_id}/inspections/calibrate")
async def calibrate_inspection_camera(
    project_id: str, request: CameraCalibrationRequest
) -> dict:
    from app.camera_calibration import calibrate_planar_fiducials

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        if session.project.get("revision") != request.project_revision:
            raise HTTPException(409, "Project revision mismatch; refresh before calibrating.")
    try:
        return calibrate_planar_fiducials(
            request.image_data_url,
            board_width_mm=request.board_width_mm,
            board_height_mm=request.board_height_mm,
            marker_size_mm=request.marker_size_mm,
            manual_corners_px=request.manual_corners_px,
        )
    except (VisionError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None


"""Software protocol generation, simulation, and hardware-gateway routes."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.opentrons_verifier import verify_jubilee_protocol, verify_opentrons_protocol
from app.protocol_generators import (
    extract_or_build_protocol_plan,
    generate_jubilee_protocol,
    generate_opentrons_protocol,
)
from app.services.runtime import api_hook
from app.services.session_store import check_revision, session_for

router = APIRouter()


class ProtocolGenerateRequest(BaseModel):
    """Protocol compilation and verification payload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project_revision: Annotated[int, Field(ge=0)]
    target_platform: Literal["auto", "opentrons-ot2", "jubilee"] = "auto"
    stock_concentration: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    target_concentrations: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    final_volume: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    allow_serial: bool = True
    allow_multi_dispense: bool = True
    min_transfer_ul: Annotated[float, Field(gt=0.0, le=50.0)] = 1.0


class ProtocolSimulateRequest(BaseModel):
    """Protocol simulation verification payload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project_revision: Annotated[int, Field(ge=0)]
    target_platform: Literal["auto", "opentrons-ot2", "jubilee"] = "auto"
    source_code: Annotated[str | None, Field(min_length=1, max_length=200000)] = None
    try_opentrons_cli: bool = True


def _resolve_platform(project: dict, requested: str) -> str:
    if requested != "auto":
        return requested
    design = project.get("design") or {}
    if design.get("foundation"):
        return "jubilee"
    return "opentrons-ot2"


@router.post("/api/projects/{project_id}/software/protocol")
async def generate_software_protocol(project_id: str, request: ProtocolGenerateRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        current = deepcopy(session.project)

    platform = _resolve_platform(current, request.target_platform)
    try:
        plan = extract_or_build_protocol_plan(
            current,
            stock_concentration=request.stock_concentration,
            target_concentrations=request.target_concentrations,
            final_volume=request.final_volume,
            allow_serial=request.allow_serial,
            allow_multi_dispense=request.allow_multi_dispense,
            min_transfer_ul=request.min_transfer_ul,
        )
        if platform == "jubilee":
            generated = generate_jubilee_protocol(
                current,
                protocol_plan=plan,
                multi_dispense=request.allow_multi_dispense,
                allow_serial=request.allow_serial,
            )
            simulation = verify_jubilee_protocol(generated["source_code"])
        else:
            generated = generate_opentrons_protocol(
                current,
                protocol_plan=plan,
                multi_dispense=request.allow_multi_dispense,
                allow_serial=request.allow_serial,
            )
            simulation = verify_opentrons_protocol(generated["source_code"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return {
        "project_id": project_id,
        "revision": current.get("revision", 0),
        "platform": generated["platform"],
        "filename": generated["filename"],
        "plan": generated["plan"],
        "pipettes": generated.get("pipettes"),
        "deck_slots": generated.get("deck_slots"),
        "source_code": generated["source_code"],
        "simulation": simulation,
    }


@router.post("/api/projects/{project_id}/software/simulate")
async def simulate_software_protocol(project_id: str, request: ProtocolSimulateRequest) -> dict:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        current = deepcopy(session.project)

    platform = _resolve_platform(current, request.target_platform)
    source_code = request.source_code
    filename = "opentrons_protocol.py" if platform == "opentrons-ot2" else "jubilee_protocol.py"
    if source_code is None:
        try:
            generated = (
                generate_jubilee_protocol(current)
                if platform == "jubilee"
                else generate_opentrons_protocol(current)
            )
            source_code = generated["source_code"]
            filename = generated["filename"]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    simulation = (
        verify_jubilee_protocol(source_code)
        if platform == "jubilee"
        else verify_opentrons_protocol(source_code, try_cli=request.try_opentrons_cli)
    )
    return {
        "project_id": project_id,
        "revision": current.get("revision", 0),
        "platform": platform,
        "filename": filename,
        "simulation": simulation,
    }


class GatewayProbeRequest(BaseModel):
    """Read-only localhost probe payload for checking a running hardware gateway."""

    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: Annotated[str, Field(min_length=10, max_length=200)] = "http://127.0.0.1:8787"


@router.get("/api/projects/{project_id}/software/gateway/checklist")
async def get_gateway_commissioning_checklist(
    project_id: str,
    revision: int,
    driver_kind: Literal["loopback", "grbl", "klipper"] = "loopback",
    watchdog_timeout_s: float = 30.0,
) -> dict:
    from app.hardware_gateway import build_gateway_commissioning_checklist

    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = deepcopy(session.project)
    return build_gateway_commissioning_checklist(
        current,
        driver_kind=driver_kind,
        watchdog_timeout_s=watchdog_timeout_s,
    )


@router.post("/api/projects/{project_id}/software/gateway/probe")
async def probe_local_gateway(project_id: str, request: GatewayProbeRequest) -> dict:
    import httpx

    from app.hardware_gateway import is_loopback_origin

    api_hook("session_for", session_for)(project_id)
    base = request.base_url.rstrip("/")
    if not is_loopback_origin(base):
        raise HTTPException(
            422, "Only local loopback gateway URLs (127.0.0.1 / localhost) may be probed."
        )
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            health = await client.get(f"{base}/health")
            health.raise_for_status()
            status = await client.get(f"{base}/status")
            status.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Cannot reach local gateway at {base}: {exc}") from None
    return {
        "reachable": True,
        "base_url": base,
        "health": health.json(),
        "status": status.json(),
    }


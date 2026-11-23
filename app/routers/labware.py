"""HTTP routes for the data-driven labware and standard hardware registries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.labware_registry import (
    get_hardware_part,
    get_labware_definition,
    list_hardware_parts,
    list_labware,
    register_custom_labware,
)

router = APIRouter()


@router.get("/api/labware-library")
async def get_labware_library(
    category: str | None = None,
    query: str | None = None,
    include_tipracks: bool = True,
) -> dict[str, Any]:
    items = list_labware(category=category, query=query, include_tipracks=include_tipracks)
    return {"count": len(items), "items": items}


@router.get("/api/labware-library/{load_name}")
async def get_labware_item(load_name: str) -> dict[str, Any]:
    try:
        return get_labware_definition(load_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/api/labware-library/custom", status_code=201)
async def add_custom_labware(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    definition = (
        payload["definition"]
        if isinstance(payload.get("definition"), dict) and "schemaVersion" in payload["definition"]
        else payload
    )
    try:
        summary = register_custom_labware(definition)
    except ValueError as exc:
        status = 409 if "collides" in str(exc).lower() else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"status": "registered", "labware": summary}


@router.get("/api/hardware-library")
async def get_hardware_library(
    category: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    items = list_hardware_parts(category=category, query=query)
    return {"count": len(items), "parts": items}


@router.get("/api/hardware-library/{part_id}")
async def get_hardware_item(part_id: str) -> dict[str, Any]:
    try:
        return get_hardware_part(part_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

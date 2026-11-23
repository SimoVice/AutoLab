"""Durable project library save/list/open routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import project_library
from app.services.runtime import api_hook
from app.services.session_store import persist_session, session_for

router = APIRouter()


class LibrarySaveRequest(BaseModel):
    """Save-to-library payload."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120, strict=True)
    revision: int = Field(ge=0, strict=True)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a project name without control characters.")
        return value


def _require_library() -> None:
    library = api_hook("project_library", project_library)
    if not library.configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "The project library is not configured. Download a portable project file instead."
            ),
        )


async def resume_session_project(project_id: str) -> dict:
    """Return the serialized active or resumed session state."""
    from copy import deepcopy

    library = api_hook("project_library", project_library)
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        saved = library.entry(project_id)
        return {
            "project": deepcopy(session.project),
            "conversation": deepcopy(session.history),
            "title": saved["title"] if saved else None,
            "assistant_message": "Project reopened at its saved revision.",
        }


@router.get("/api/library")
async def list_saved_projects() -> dict:
    library = api_hook("project_library", project_library)
    return {"enabled": library.configured(), "projects": library.entries()}


@router.post("/api/projects/{project_id}/save")
async def save_named_project(project_id: str, request: LibrarySaveRequest) -> dict:
    _require_library()
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        if session.project["revision"] != request.revision:
            raise HTTPException(status_code=409, detail="Save the current project revision.")
        saved = api_hook("_persist_session", persist_session)(session, title=request.title)
    return {"saved_project": saved}


@router.post("/api/library/{project_id}/open")
async def open_saved_project(project_id: str) -> dict:
    _require_library()
    library = api_hook("project_library", project_library)
    if library.entry(project_id) is None:
        raise HTTPException(status_code=404, detail="Saved project not found.")
    return await resume_session_project(project_id)

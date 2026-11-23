"""AutoLab HTTP API composition root.

Routes live in app.routers and stateful business logic lives in app.services.
Legacy module-level attributes are re-exported to preserve compatibility with
direct imports and test monkeypatches.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from app import project_library
from app.agent_loop import run_agent_turn
from app.chat_actions import execute_chat_actions
from app.chat_activity import install_activity
from app.design_agent import develop_design
from app.domain import apply_update, configure_project, confirm_project, new_project
from app.illustration import MAX_REQUEST_BYTES, generate_illustration
from app.model_catalog import discover_catalog
from app.model_client import generate_turn
from app.procurement import refresh_procurement
from app.project_exports import start_export
from app.routers import auth, chat, exports, fixtures, library, media, procurement, projects
from app.routers.chat import send_message
from app.routers.exports import download_instructions, get_instructions
from app.routers.media import _illustrate_connected
from app.routers.procurement import _refresh_procurement_connected
from app.services.chat_service import MessageRequest
from app.services.session_store import (
    ProjectSession,
    _sessions,
    accept_project,
    persist_session,
    session_for,
)
from app.transcription import transcribe_audio
from app.workflows import describe_workflow

_accept_project = accept_project
_persist_session = persist_session

app = FastAPI(
    title="AutoLab",
    version="0.1.0",
    openapi_url=None if os.getenv("AUTOLAB_HOSTED") == "1" else "/openapi.json",
)
auth.install_auth_routes(app)

for router_module in (projects, library, chat, procurement, exports, fixtures, media):
    app.include_router(router_module.router)

install_activity(app)

__all__ = [
    "HTTPException",
    "MAX_REQUEST_BYTES",
    "MessageRequest",
    "ProjectSession",
    "_accept_project",
    "_illustrate_connected",
    "_persist_session",
    "_refresh_procurement_connected",
    "_sessions",
    "app",
    "apply_update",
    "configure_project",
    "confirm_project",
    "describe_workflow",
    "develop_design",
    "discover_catalog",
    "download_instructions",
    "execute_chat_actions",
    "generate_illustration",
    "generate_turn",
    "get_instructions",
    "new_project",
    "project_library",
    "refresh_procurement",
    "run_agent_turn",
    "send_message",
    "session_for",
    "start_export",
    "transcribe_audio",
]

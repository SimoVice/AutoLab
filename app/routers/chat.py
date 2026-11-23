"""Conversational chat endpoint router."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.chat_activity import activity_dependency
from app.services.chat_service import MessageRequest, handle_chat_message
from app.services.session_store import session_for

router = APIRouter()


@router.post(
    "/api/projects/{project_id}/messages",
    dependencies=[Depends(activity_dependency(session_for))],
)
async def send_message(project_id: str, request: MessageRequest) -> dict:
    return await handle_chat_message(project_id, request)

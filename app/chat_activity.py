"""Ephemeral, request-scoped activity; never inferred from elapsed time."""

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from fastapi import HTTPException, Request

_current = ContextVar("chat_activity", default=None)
_activities: dict[tuple[str, str], dict] = {}
_valid_id = re.compile(r"^[a-zA-Z0-9_-]{16,80}$")

MAX_ACTIVITIES = 512
MAX_TRACE_ITEMS = 16
RETENTION_SECONDS = 60


def report_activity(
    stage: str,
    *,
    detail: str | None = None,
    tool: str | None = None,
    step: int | None = None,
    max_steps: int | None = None,
) -> None:
    """Update the active request stage and optional step/tool telemetry."""
    key = _current.get()
    if key not in _activities:
        return
    entry = _activities[key]
    entry["stage"] = stage
    entry["updated"] = time.monotonic()

    if detail is None and tool is None and step is None and max_steps is None:
        return

    if detail is not None:
        entry["detail"] = detail[:240]
    if tool is not None:
        entry["tool"] = tool[:64]
    if step is not None:
        entry["step"] = step
    if max_steps is not None:
        entry["max_steps"] = max_steps

    trace = entry.setdefault("trace", [])
    trace_item = {"stage": stage}
    if tool is not None:
        trace_item["tool"] = tool[:64]
    if detail is not None:
        trace_item["detail"] = detail[:160]
    if step is not None:
        trace_item["step"] = step
    trace.append(trace_item)
    if len(trace) > MAX_TRACE_ITEMS:
        del trace[:-MAX_TRACE_ITEMS]


@contextmanager
def activity_scope(stage: str):
    key = _current.get()
    previous = _activities.get(key, {}).get("stage")
    report_activity(stage)
    try:
        yield
    finally:
        if previous is not None:
            report_activity(previous)


def _prune():
    now = time.monotonic()
    for key, entry in list(_activities.items()):
        if not entry["active"] and now - entry["updated"] >= RETENTION_SECONDS:
            del _activities[key]
    while len(_activities) >= MAX_ACTIVITIES:
        completed = [key for key, entry in _activities.items() if not entry["active"]]
        if not completed:
            break
        del _activities[min(completed, key=lambda key: _activities[key]["updated"])]


def activity_dependency(session_for):
    async def track(request: Request):
        request_id = request.headers.get("x-chat-request-id", "")
        if not _valid_id.fullmatch(request_id):
            yield
            return
        project_id = request.path_params["project_id"]
        session_for(project_id)
        _prune()
        key = (project_id, request_id)
        if key in _activities:
            raise HTTPException(409, "This request identifier has already been used.")
        if len(_activities) >= MAX_ACTIVITIES:
            raise HTTPException(503, "Too many requests are active. Try again shortly.")
        _activities[key] = {"stage": "waiting", "active": True, "updated": time.monotonic()}
        token = _current.set(key)
        try:
            yield
        finally:
            _activities[key].update(active=False, updated=time.monotonic())
            _current.reset(token)

    return track


def install_activity(app) -> None:
    @app.get("/api/projects/{project_id}/activity/{request_id}")
    async def activity(project_id: str, request_id: str):
        _prune()
        entry = _activities.get((project_id, request_id))
        if entry is None:
            raise HTTPException(404, "No active request with this identifier.")
        payload = {"stage": entry["stage"], "active": entry["active"]}
        for optional_key in ("detail", "tool", "step", "max_steps"):
            if entry.get(optional_key) is not None:
                payload[optional_key] = entry[optional_key]
        if entry.get("trace"):
            payload["trace"] = deepcopy(entry["trace"])
        return payload

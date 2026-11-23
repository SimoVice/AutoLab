"""Structural verification for the decomposed AutoLab API and router modules."""

from __future__ import annotations

from pathlib import Path

import app.api as api
from app.services import session_store

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_api_composition_root_is_small_and_shares_session_store():
    lines = (APP_DIR / "api.py").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 250
    assert api._sessions is session_store._sessions


def test_expected_router_and_service_modules_exist():
    for rel in (
        "routers/auth.py",
        "routers/projects.py",
        "routers/library.py",
        "routers/chat.py",
        "routers/procurement.py",
        "routers/exports.py",
        "routers/media.py",
        "services/runtime.py",
        "services/session_store.py",
        "services/chat_service.py",
    ):
        assert (APP_DIR / rel).is_file(), rel


def _collect_paths(routes) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        if hasattr(route, "path"):
            paths.add(route.path)
        elif hasattr(route, "original_router"):
            paths |= _collect_paths(route.original_router.routes)
    return paths


def test_key_routes_from_every_domain_router_are_registered():
    paths = _collect_paths(api.app.routes)
    expected = {
        "/api/health",
        "/api/models",
        "/api/references",
        "/api/projects",
        "/api/projects/{project_id}",
        "/api/projects/{project_id}/snapshot",
        "/api/projects/open",
        "/api/library",
        "/api/projects/{project_id}/save",
        "/api/library/{project_id}/open",
        "/api/projects/{project_id}/messages",
        "/api/projects/{project_id}/parts",
        "/api/projects/{project_id}/procurement",
        "/api/projects/{project_id}/cost-estimate",
        "/api/projects/{project_id}/instructions",
        "/api/projects/{project_id}/exports",
        "/api/transcribe",
        "/api/projects/{project_id}/illustration",
        "/api/projects/{project_id}/inspections",
        "/api/projects/{project_id}/activity/{request_id}",
    }
    missing = expected - paths
    assert not missing, f"Missing expected routes: {sorted(missing)}"

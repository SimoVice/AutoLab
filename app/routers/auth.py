"""Authentication router wrapper around the hosted access gate."""

from __future__ import annotations

from fastapi import FastAPI

from app.host_auth import install_host_auth


def install_auth_routes(app: FastAPI) -> None:
    """Install the opt-in hosted authentication middleware and endpoints."""
    install_host_auth(app)

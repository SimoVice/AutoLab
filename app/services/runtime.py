"""Runtime hook lookup preserving test monkeypatches applied to app.api."""

from __future__ import annotations

import sys
from typing import TypeVar

T = TypeVar("T")


def api_hook(name: str, default: T) -> T:
    """Return an override set on app.api when loaded, otherwise the local default."""
    module = sys.modules.get("app.api")
    if module is not None and hasattr(module, name):
        return getattr(module, name)
    return default

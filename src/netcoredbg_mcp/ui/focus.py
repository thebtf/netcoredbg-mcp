"""Focus assertion helpers."""

from __future__ import annotations

from typing import Any


async def assert_focus(backend: Any, selector: dict[str, Any]) -> dict[str, Any]:
    """Assert focus is on or inside the selector."""
    return await backend.assert_focus(dict(selector))

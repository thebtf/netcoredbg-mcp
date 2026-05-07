"""Scoped list item helpers."""

from __future__ import annotations

from typing import Any


async def invoke_list_item(
    backend: Any,
    selector: dict[str, Any],
    *,
    item: dict[str, Any],
    invoke: str = "default",
) -> dict[str, Any]:
    """Invoke a list item by visible name or index through the active backend."""
    return await backend.list_invoke_item(dict(selector), dict(item), invoke)


async def toggle_list_item_child(
    backend: Any,
    selector: dict[str, Any],
    *,
    item: dict[str, Any],
    child: dict[str, Any],
    target_state: str | None = None,
) -> dict[str, Any]:
    """Toggle a child control found inside a resolved list item."""
    return await backend.list_toggle_item_child(
        dict(selector),
        dict(item),
        dict(child),
        target_state,
    )

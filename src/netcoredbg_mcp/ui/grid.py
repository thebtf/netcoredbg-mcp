"""DataGrid evidence helpers."""

from __future__ import annotations

from typing import Any


async def read_grid_visible_rows(
    backend: Any,
    selector: dict[str, Any],
) -> dict[str, Any]:
    """Read visible DataGrid row evidence through the active backend."""
    return await backend.grid_visible_rows(dict(selector))


async def read_grid_selected_rows(
    backend: Any,
    selector: dict[str, Any],
) -> dict[str, Any]:
    """Read selected DataGrid row evidence through the active backend."""
    return await backend.grid_selected_rows(dict(selector))


async def select_grid_range(
    backend: Any,
    selector: dict[str, Any],
    start_index: int,
    end_index: int,
) -> dict[str, Any]:
    """Select a DataGrid row range through the active backend."""
    return await backend.grid_select_range(dict(selector), start_index, end_index)


async def assert_grid_range(
    backend: Any,
    selector: dict[str, Any],
    start_index: int,
    end_index: int,
) -> dict[str, Any]:
    """Assert a DataGrid row range through the active backend."""
    return await backend.grid_assert_range(dict(selector), start_index, end_index)

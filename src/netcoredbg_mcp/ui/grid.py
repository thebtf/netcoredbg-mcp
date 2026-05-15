"""DataGrid evidence helpers."""

from __future__ import annotations

from typing import Any


async def read_grid_visible_rows(
    backend: Any,
    selector: dict[str, Any],
) -> dict[str, Any]:
    """Read visible DataGrid row evidence through the active backend."""
    return await backend.grid_visible_rows(dict(selector))


async def snapshot_grid(
    backend: Any,
    selector: dict[str, Any],
    *,
    rows: dict[str, Any] | None = None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Read visible DataGrid rows with cell-level evidence."""
    if hasattr(backend, "grid_snapshot"):
        return await backend.grid_snapshot(
            dict(selector),
            rows=dict(rows or {}),
            columns=list(columns or []),
        )
    return await backend.grid_visible_rows(dict(selector))


async def read_grid_selected_rows(
    backend: Any,
    selector: dict[str, Any],
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Read selected DataGrid row evidence through the active backend."""
    return await backend.grid_selected_rows(dict(selector), columns=columns or [])


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


async def assert_grid_rows(
    backend: Any,
    selector: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Assert visible DataGrid row cell values from snapshot evidence."""
    if hasattr(backend, "grid_assert_rows"):
        return await backend.grid_assert_rows(
            dict(selector),
            list(rows),
            columns=list(columns or []),
        )

    snapshot = await snapshot_grid(backend, selector, columns=columns)
    if snapshot.get("status") in {"UNSUPPORTED", "BLOCKED"}:
        return dict(snapshot)

    visible_rows = snapshot.get("visible_rows")
    if not isinstance(visible_rows, list):
        return {
            "status": "FAIL",
            "asserted": False,
            "reason": "row evidence unavailable",
            "failed_rows": list(rows),
        }

    failures: list[dict[str, Any]] = []
    matched_rows: list[int] = []
    for expected in rows:
        index = expected.get("index")
        row = _row_by_index(visible_rows, index)
        expected_cells = expected.get("contains") or {}
        if not isinstance(row, dict):
            failures.append({"index": index, "reason": "row not found"})
            continue
        cells = row.get("cells")
        if not isinstance(cells, dict) or not cells:
            failures.append(
                {
                    "index": index,
                    "reason": "row cell evidence unavailable",
                }
            )
            continue
        missing = {
            str(key): value
            for key, value in expected_cells.items()
            if str(cells.get(str(key), "")) != str(value)
        }
        if missing:
            failures.append(
                {
                    "index": index,
                    "reason": "row cell mismatch",
                    "missing": missing,
                    "actual_cells": dict(cells),
                }
            )
            continue
        if index is not None:
            matched_rows.append(int(index))

    if failures:
        reason = (
            "row cell evidence unavailable"
            if any(item["reason"] == "row cell evidence unavailable" for item in failures)
            else "row cell assertion failed"
        )
        return {
            "status": "FAIL",
            "asserted": False,
            "reason": reason,
            "failed_rows": failures,
            "snapshot": snapshot,
        }

    return {
        "status": "PASS",
        "asserted": True,
        "matched_rows": matched_rows,
        "snapshot": snapshot,
    }


def _row_by_index(rows: list[Any], index: Any) -> dict[str, Any] | None:
    try:
        expected_index = int(index)
    except (TypeError, ValueError):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("index") == expected_index:
            return row
    return None

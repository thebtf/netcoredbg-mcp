"""DataGrid evidence helpers."""

from __future__ import annotations

from collections.abc import Mapping
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


async def read_grid_state(
    backend: Any,
    selector: dict[str, Any],
    *,
    rows: dict[str, Any] | None = None,
    columns: list[str] | None = None,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read bounded DataGrid visible and selected-row state."""
    snapshot = await snapshot_grid(backend, selector, rows=rows, columns=columns)
    if not _passes(snapshot):
        return dict(snapshot) if isinstance(snapshot, dict) else {
            "status": "BLOCKED",
            "reason": "grid state snapshot returned non-object result",
            "snapshot_result": snapshot,
        }
    if not isinstance(snapshot, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid state snapshot returned non-object result",
            "snapshot_result": snapshot,
        }

    selected = await read_grid_selected_rows(backend, selector, columns=columns)
    if not _passes(selected):
        result = dict(selected) if isinstance(selected, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid selected row state unavailable")
        result["snapshot"] = snapshot
        return result
    if not isinstance(selected, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid selected row state returned non-object result",
            "snapshot": snapshot,
            "selection_result": selected,
        }

    result = dict(snapshot)
    result["status"] = "PASS"
    result["selected_rows"] = list(selected.get("selected_rows") or [])
    result["identity_strategy"] = _identity_strategy(identity or {})
    return result


async def select_grid_range(
    backend: Any,
    selector: dict[str, Any],
    start_index: int,
    end_index: int,
) -> dict[str, Any]:
    """Select a DataGrid row range through the active backend."""
    return await backend.grid_select_range(dict(selector), start_index, end_index)


async def select_grid_row(
    backend: Any,
    selector: dict[str, Any],
    row_index: int | None = None,
    *,
    row_key: str | None = None,
    columns: list[str] | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select one currently visible DataGrid row and confirm the selection."""
    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity,
        rows=rows,
        columns=columns,
    )
    if blocked is not None:
        return blocked
    visible_index = _visible_index(resolved)
    if visible_index is None:
        return {
            "status": "BLOCKED",
            "reason": "resolved row has no visible index",
            "resolved_row": _compact_row_ref(resolved, identity),
        }

    selection = await select_grid_range(backend, selector, visible_index, visible_index)
    if not _passes(selection):
        result = dict(selection) if isinstance(selection, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid row selection failed")
        result["resolved_row"] = _compact_row_ref(resolved, identity)
        return result

    confirmation = await read_grid_selected_rows(backend, selector, columns=columns)
    if not _passes(confirmation):
        result = dict(confirmation) if isinstance(confirmation, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "selected row confirmation failed")
        result["confirmed_selection"] = False
        result["resolved_row"] = _compact_row_ref(resolved, identity)
        result["selection_result"] = selection
        return result
    if not isinstance(confirmation, dict):
        return {
            "status": "BLOCKED",
            "reason": "selected row confirmation returned non-object result",
            "confirmed_selection": False,
            "resolved_row": _compact_row_ref(resolved, identity),
            "selection_result": selection,
            "confirmation_result": confirmation,
        }

    selected_rows = confirmation.get("selected_rows")
    if not isinstance(selected_rows, list):
        return {
            "status": "BLOCKED",
            "reason": "selected row confirmation did not include selected_rows",
            "confirmed_selection": False,
            "resolved_row": _compact_row_ref(resolved, identity),
            "selection_result": selection,
            "confirmation_result": confirmation,
        }
    observed = [
        _visible_index(row)
        for row in selected_rows
        if isinstance(row, Mapping) and _visible_index(row) is not None
    ]
    if observed != [visible_index]:
        return {
            "status": "FAIL",
            "reason": "selected row confirmation failed",
            "confirmed_selection": False,
            "resolved_row": _compact_row_ref(resolved, identity),
            "observed_selected_indices": observed,
            "selected_rows": selected_rows,
            "selection_result": selection,
            "confirmation_result": confirmation,
        }

    result = dict(selection) if isinstance(selection, dict) else {}
    result["status"] = "PASS"
    result["confirmed_selection"] = True
    result["resolved_row"] = _compact_row_ref(resolved, identity)
    result["selected_rows"] = selected_rows
    result["observed_selected_indices"] = observed
    return result


async def click_grid_row(
    backend: Any,
    selector: dict[str, Any],
    row_index: int | None = None,
    *,
    row_key: str | None = None,
    column: str | None = None,
    columns: list[str] | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Click one currently visible DataGrid row through a backend primitive."""
    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity,
        rows=rows,
        columns=columns,
    )
    if blocked is not None:
        return blocked
    visible_index = _visible_index(resolved)
    if visible_index is None:
        return {
            "status": "BLOCKED",
            "reason": "resolved row has no visible index",
            "resolved_row": _compact_row_ref(resolved, identity),
        }

    click_row = getattr(backend, "grid_click_row", None)
    if not callable(click_row):
        return {
            "status": "BLOCKED",
            "reason": "grid row click unavailable",
            "requested": {"adapter": "ui.grid.click_row"},
            "accepted": {"backend": "DataGrid backend with grid_click_row"},
            "next_step": "Use a FlaUI bridge backend that can click resolved grid rows.",
            "resolved_row": _compact_row_ref(resolved, identity),
        }
    result = await click_row(dict(selector), visible_index, column=column)
    if not isinstance(result, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid row click returned non-object result",
            "resolved_row": _compact_row_ref(resolved, identity),
            "click_result": result,
        }
    output = dict(result)
    output.setdefault("status", "PASS")
    output["resolved_row"] = _compact_row_ref(resolved, identity)
    return output


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


async def resolve_visible_grid_row(
    backend: Any,
    selector: dict[str, Any],
    *,
    row_index: int | None = None,
    row_key: str | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
    columns: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve a requested row against current visible row evidence."""
    if row_index is None and not row_key:
        return {}, {
            "status": "BLOCKED",
            "reason": "grid row request missing",
            "requested": {"row_index": row_index, "row_key": row_key},
            "accepted": {"row": "visible row_index or row_key"},
            "next_step": (
                "Provide row_index for a visible logical row or row_key for a "
                "unique visible row identity."
            ),
        }

    snapshot = await snapshot_grid(
        backend,
        selector,
        rows=rows or {"visible_only": True},
        columns=_identity_columns(identity, columns),
    )
    if not _passes(snapshot):
        result = dict(snapshot) if isinstance(snapshot, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid visible row lookup failed")
        result["requested"] = {"row_index": row_index, "row_key": row_key}
        return {}, result
    if not isinstance(snapshot, dict):
        return {}, {
            "status": "BLOCKED",
            "reason": "grid visible row lookup returned non-object result",
            "requested": {"row_index": row_index, "row_key": row_key},
            "snapshot_result": snapshot,
        }
    visible_rows = snapshot.get("visible_rows")
    if not isinstance(visible_rows, list):
        return {}, {
            "status": "BLOCKED",
            "reason": "grid visible row evidence unavailable",
            "requested": {"row_index": row_index, "row_key": row_key},
            "accepted": {"visible_rows": "list of visible row objects"},
            "next_step": "Use a DataGrid backend that returns visible row evidence.",
        }

    matches = _matching_visible_rows(
        visible_rows,
        row_index=row_index,
        row_key=row_key,
        identity=identity,
    )
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return {}, {
            "status": "BLOCKED",
            "reason": "grid row is not visible",
            "requested": {"row_index": row_index, "row_key": row_key},
            "accepted": {"row": "currently visible row index or unique row key"},
            "next_step": "Scroll the grid or choose a currently visible row before acting.",
        }
    return {}, {
        "status": "AMBIGUOUS",
        "reason": "grid row identity is ambiguous",
        "requested": {"row_index": row_index, "row_key": row_key},
        "matches": [_compact_row_ref(row, identity) for row in matches],
        "next_step": "Disambiguate the row with row_index or a unique row_key.",
    }


def _matching_visible_rows(
    visible_rows: list[Any],
    *,
    row_index: int | None,
    row_key: str | None,
    identity: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in visible_rows:
        if not isinstance(row, Mapping):
            continue
        candidate = dict(row)
        if row_index is not None:
            logical_index = _logical_index(candidate)
            if logical_index == row_index:
                matches.append(candidate)
            continue
        if row_key and _row_matches_key(candidate, row_key, identity):
            matches.append(candidate)
    return matches


def _row_matches_key(
    row: Mapping[str, Any],
    row_key: str,
    identity: Mapping[str, Any] | None,
) -> bool:
    if not row_key:
        return False
    candidates = {
        _row_identity(row, identity),
        str(row.get("automation_id") or ""),
        str(row.get("automationId") or ""),
        str(row.get("name") or ""),
    }
    cells = row.get("cells")
    if isinstance(cells, Mapping):
        candidates.update(str(value) for value in cells.values())
    cell_values = row.get("cell_values")
    if isinstance(cell_values, list):
        for cell in cell_values:
            if isinstance(cell, Mapping):
                candidates.add(str(cell.get("text") or ""))
                candidates.add(str(cell.get("value") or ""))
    return row_key in candidates


def _row_identity(
    row: Mapping[str, Any],
    identity: Mapping[str, Any] | None = None,
) -> str:
    cells = row.get("cells")
    if isinstance(cells, Mapping):
        for column in _identity_columns(identity, None):
            if cells.get(column):
                return str(cells[column])
        for value in cells.values():
            if value:
                return str(value)
    cell_values = row.get("cell_values")
    if isinstance(cell_values, list):
        for cell in cell_values:
            if isinstance(cell, Mapping):
                value = cell.get("text") or cell.get("value")
                if value:
                    return str(value)
    for key in ("stable_id", "id", "automation_id", "automationId", "name"):
        if row.get(key):
            return str(row[key])
    return f"row:{row.get('row_index', row.get('index'))}"


def _compact_row_ref(
    row: Mapping[str, Any],
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": row.get("index"),
        "identity": _row_identity(row, identity),
    }
    if row.get("row_index") is not None:
        result["row_index"] = row.get("row_index")
    return result


def _identity_columns(
    identity: Mapping[str, Any] | None,
    columns: list[str] | None,
) -> list[str]:
    requested: list[str] = []
    if columns:
        requested.extend(str(item) for item in columns if item)
    if identity:
        column = identity.get("column")
        if column:
            requested.append(str(column))
        raw_columns = identity.get("columns")
        if isinstance(raw_columns, list):
            requested.extend(str(item) for item in raw_columns if item)
        elif raw_columns:
            requested.append(str(raw_columns))
    return list(dict.fromkeys(requested))


def _identity_strategy(identity: Mapping[str, Any]) -> dict[str, Any]:
    columns = _identity_columns(identity, None)
    if columns:
        result: dict[str, Any] = {"kind": "configured_column", "derived": True}
        if len(columns) == 1:
            result["column"] = columns[0]
        else:
            result["columns"] = columns
        return result
    return {"kind": "row_fallback", "derived": True}


def _logical_index(row: Mapping[str, Any]) -> int | None:
    if row.get("row_index") is not None:
        return _int_or_none(row.get("row_index"))
    return _int_or_none(row.get("index"))


def _visible_index(row: Mapping[str, Any]) -> int | None:
    return _int_or_none(row.get("index", row.get("row_index")))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _passes(result: Any) -> bool:
    return isinstance(result, dict) and str(result.get("status", "PASS")).upper() in {
        "PASS",
        "OK",
        "SUCCESS",
    }


def _blocked_status(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "BLOCKED").upper()
    return "BLOCKED" if status in {"PASS", "OK", "SUCCESS"} else status

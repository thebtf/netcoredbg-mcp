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
    evidence_columns = _identity_columns(identity, columns)
    snapshot = await snapshot_grid(
        backend,
        selector,
        rows=rows,
        columns=evidence_columns,
    )
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

    selected = await read_grid_selected_rows(
        backend,
        selector,
        columns=evidence_columns,
    )
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


async def ensure_grid_row_visible(
    backend: Any,
    selector: dict[str, Any],
    row_index: int | None = None,
    *,
    row_key: str | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
    columns: list[str] | None = None,
    max_scrolls: int | None = None,
    scroll_settle_ms: int | None = None,
) -> dict[str, Any]:
    """Make a DataGrid row visible by explicit row request and confirm it."""
    identity_payload = _identity_payload(identity, columns)
    evidence_columns = _identity_columns(identity_payload, columns)
    requested = {"row_index": row_index, "row_key": row_key}

    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity_payload,
        rows=rows,
        columns=evidence_columns,
    )
    if blocked is None:
        return {
            "status": "PASS",
            "already_visible": True,
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }
    if blocked.get("reason") != "grid row is not visible":
        return blocked

    ensure_visible = getattr(backend, "grid_ensure_visible", None)
    if not callable(ensure_visible):
        return {
            "status": "BLOCKED",
            "reason": "grid ensure-visible unavailable",
            "requested": {"adapter": "ui.grid.ensure_visible", **requested},
            "accepted": {"backend": "DataGrid backend with grid_ensure_visible"},
            "next_step": "Use a FlaUI bridge backend that can realize or scroll grid rows.",
            "lookup_result": blocked,
        }

    ensure_kwargs: dict[str, Any] = {
        "identity": identity_payload,
        "rows": dict(rows or {"visible_only": True}),
        "columns": evidence_columns,
    }
    if row_key is not None:
        ensure_kwargs["row_key"] = row_key
    if row_index is not None:
        ensure_kwargs["row_index"] = row_index
    if max_scrolls is not None:
        ensure_kwargs["max_scrolls"] = max_scrolls
    if scroll_settle_ms is not None:
        ensure_kwargs["scroll_settle_ms"] = scroll_settle_ms

    ensure_result = await ensure_visible(dict(selector), **ensure_kwargs)
    if not isinstance(ensure_result, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid ensure-visible returned non-object result",
            "requested": requested,
            "ensure_result": ensure_result,
        }
    if not _passes(ensure_result):
        result = dict(ensure_result)
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid ensure-visible backend did not pass")
        result.setdefault("requested", requested)
        return result

    confirmed, confirm_blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity_payload,
        rows=rows,
        columns=evidence_columns,
    )
    if confirm_blocked is not None:
        result = dict(confirm_blocked)
        result["status"] = _blocked_status(result)
        if result.get("reason") == "grid row is not visible":
            result["reason"] = "grid row is not visible after ensure_visible"
        result["requested"] = requested
        result["ensure_result"] = ensure_result
        return result

    output = dict(ensure_result)
    output["status"] = "PASS"
    output["already_visible"] = False
    output["resolved_row"] = _compact_row_ref(confirmed, identity_payload)
    return output


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
    ensure_visible: bool = False,
    max_scrolls: int | None = None,
    scroll_settle_ms: int | None = None,
) -> dict[str, Any]:
    """Select one currently visible DataGrid row and confirm the selection."""
    identity_payload = _identity_payload(identity, columns)
    evidence_columns = _identity_columns(identity_payload, columns)
    ensure_visible_result: dict[str, Any] | None = None
    if ensure_visible:
        ensure_visible_result = await ensure_grid_row_visible(
            backend,
            selector,
            row_index=row_index,
            row_key=row_key,
            identity=identity_payload,
            rows=rows,
            columns=evidence_columns,
            max_scrolls=max_scrolls,
            scroll_settle_ms=scroll_settle_ms,
        )
        if not _passes(ensure_visible_result):
            result = dict(ensure_visible_result) if isinstance(ensure_visible_result, dict) else {}
            result["status"] = _blocked_status(result)
            result.setdefault("reason", "grid ensure-visible failed before row selection")
            result["ensure_visible_result"] = ensure_visible_result
            result["action_skipped"] = True
            return result

    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity_payload,
        rows=rows,
        columns=evidence_columns,
    )
    if blocked is not None:
        return blocked
    visible_index = _visible_index(resolved)
    if visible_index is None:
        return {
            "status": "BLOCKED",
            "reason": "resolved row has no visible index",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }

    selection = await select_grid_range(backend, selector, visible_index, visible_index)
    if not _passes(selection):
        result = dict(selection) if isinstance(selection, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid row selection failed")
        result["resolved_row"] = _compact_row_ref(resolved, identity_payload)
        return result

    confirmation = await read_grid_selected_rows(backend, selector, columns=evidence_columns)
    if not _passes(confirmation):
        result = dict(confirmation) if isinstance(confirmation, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "selected row confirmation failed")
        result["confirmed_selection"] = False
        result["resolved_row"] = _compact_row_ref(resolved, identity_payload)
        result["selection_result"] = selection
        return result
    if not isinstance(confirmation, dict):
        return {
            "status": "BLOCKED",
            "reason": "selected row confirmation returned non-object result",
            "confirmed_selection": False,
            "resolved_row": _compact_row_ref(resolved, identity_payload),
            "selection_result": selection,
            "confirmation_result": confirmation,
        }

    selected_rows = confirmation.get("selected_rows")
    if not isinstance(selected_rows, list):
        return {
            "status": "BLOCKED",
            "reason": "selected row confirmation did not include selected_rows",
            "confirmed_selection": False,
            "resolved_row": _compact_row_ref(resolved, identity_payload),
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
            "resolved_row": _compact_row_ref(resolved, identity_payload),
            "observed_selected_indices": observed,
            "selected_rows": selected_rows,
            "selection_result": selection,
            "confirmation_result": confirmation,
        }

    result = dict(selection) if isinstance(selection, dict) else {}
    result["status"] = "PASS"
    result["confirmed_selection"] = True
    result["resolved_row"] = _compact_row_ref(resolved, identity_payload)
    result["selected_rows"] = selected_rows
    result["observed_selected_indices"] = observed
    if ensure_visible_result is not None:
        result["ensure_visible_result"] = ensure_visible_result
    return result


async def select_grid_rows_by_identities(
    backend: Any,
    selector: dict[str, Any],
    row_identities: list[str],
    *,
    columns: list[str] | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select visible DataGrid rows by stable identity and confirm the selection."""
    requested_identities = [str(item).strip() for item in row_identities]
    identity_payload = _identity_payload(identity, columns)
    evidence_columns = _identity_columns(identity_payload, columns)
    if not requested_identities:
        return {
            "status": "BLOCKED",
            "reason": "grid identity selection requires row identities",
            "requested": {"row_identities": row_identities},
            "accepted": {"row_identities": "non-empty list of unique row identities"},
        }
    if any(not item for item in requested_identities):
        return {
            "status": "BLOCKED",
            "reason": "grid identity selection contains empty identities",
            "requested": {"row_identities": row_identities},
            "accepted": {"row_identities": "non-empty list of unique row identities"},
        }
    if len(set(requested_identities)) != len(requested_identities):
        return {
            "status": "BLOCKED",
            "reason": "grid identity selection contains duplicate identities",
            "requested": {"row_identities": requested_identities},
            "accepted": {"row_identities": "unique row identities"},
        }

    snapshot = await snapshot_grid(
        backend,
        selector,
        rows=rows or {"visible_only": True},
        columns=evidence_columns,
    )
    if not _passes(snapshot):
        result = dict(snapshot) if isinstance(snapshot, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid identity selection visible row lookup failed")
        result["requested"] = {"row_identities": requested_identities}
        return result
    if not isinstance(snapshot, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid identity selection snapshot returned non-object result",
            "requested": {"row_identities": requested_identities},
            "snapshot_result": snapshot,
        }
    visible_rows = snapshot.get("visible_rows")
    if not isinstance(visible_rows, list):
        return {
            "status": "BLOCKED",
            "reason": "grid visible row evidence unavailable",
            "requested": {"row_identities": requested_identities},
            "accepted": {"visible_rows": "list of visible row objects"},
            "next_step": "Use a DataGrid backend that returns visible row evidence.",
        }

    resolved_rows: list[dict[str, Any]] = []
    for row_identity in requested_identities:
        matches = [
            dict(row)
            for row in visible_rows
            if isinstance(row, Mapping)
            and _row_identity(row, identity_payload) == row_identity
        ]
        if not matches:
            return {
                "status": "BLOCKED",
                "reason": "grid row identity is not visible",
                "requested": {
                    "row_identity": row_identity,
                    "row_identities": requested_identities,
                },
                "accepted": {"row": "currently visible unique row identity"},
                "next_step": "Scroll the grid or choose visible row identities before selecting.",
            }
        if len(matches) > 1:
            return {
                "status": "AMBIGUOUS",
                "reason": "grid row identity is ambiguous",
                "requested": {
                    "row_identity": row_identity,
                    "row_identities": requested_identities,
                },
                "matches": [_compact_row_ref(row, identity_payload) for row in matches],
                "next_step": "Disambiguate the row with a unique identity column.",
            }
        resolved_rows.append(matches[0])

    selected_indices = [_visible_index(row) for row in resolved_rows]
    if any(index is None for index in selected_indices):
        return {
            "status": "BLOCKED",
            "reason": "resolved identity row has no visible index",
            "requested": {"row_identities": requested_identities},
            "resolved_rows": [_compact_row_ref(row, identity_payload) for row in resolved_rows],
        }
    indices = [int(index) for index in selected_indices if index is not None]
    if len(set(indices)) != len(indices):
        return {
            "status": "BLOCKED",
            "reason": "grid identity selection resolved duplicate visible indices",
            "requested": {"row_identities": requested_identities},
            "selected_indices": indices,
            "resolved_rows": [_compact_row_ref(row, identity_payload) for row in resolved_rows],
        }

    selection = await _select_visible_indices(backend, selector, indices)
    if not _passes(selection):
        result = dict(selection) if isinstance(selection, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "grid identity selection failed")
        result["requested"] = {"row_identities": requested_identities}
        result["selected_indices"] = indices
        result["resolved_rows"] = [_compact_row_ref(row, identity_payload) for row in resolved_rows]
        return result

    confirmation = await read_grid_selected_rows(backend, selector, columns=evidence_columns)
    if not _passes(confirmation):
        result = dict(confirmation) if isinstance(confirmation, dict) else {}
        result["status"] = _blocked_status(result)
        result.setdefault("reason", "selected identity confirmation failed")
        result["confirmed_selection"] = False
        result["selection_result"] = selection
        return result
    if not isinstance(confirmation, dict):
        return {
            "status": "BLOCKED",
            "reason": "selected identity confirmation returned non-object result",
            "confirmed_selection": False,
            "selection_result": selection,
            "confirmation_result": confirmation,
        }
    selected_rows = confirmation.get("selected_rows")
    if not isinstance(selected_rows, list):
        return {
            "status": "BLOCKED",
            "reason": "selected identity confirmation did not include selected_rows",
            "confirmed_selection": False,
            "selection_result": selection,
            "confirmation_result": confirmation,
        }
    observed_identities = [
        _row_identity(row, identity_payload)
        for row in selected_rows
        if isinstance(row, Mapping)
    ]
    if (
        len(observed_identities) != len(requested_identities)
        or set(observed_identities) != set(requested_identities)
    ):
        return {
            "status": "FAIL",
            "reason": "selected identity confirmation failed",
            "confirmed_selection": False,
            "selected_identities": requested_identities,
            "observed_selected_identities": observed_identities,
            "selected_rows": selected_rows,
            "selection_result": selection,
            "confirmation_result": confirmation,
        }

    result = dict(selection) if isinstance(selection, dict) else {}
    result["status"] = "PASS"
    result["confirmed_selection"] = True
    result["selected_indices"] = indices
    result["selected_identities"] = requested_identities
    result["observed_selected_identities"] = observed_identities
    result["resolved_rows"] = [_compact_row_ref(row, identity_payload) for row in resolved_rows]
    result["selected_rows"] = selected_rows
    return result


async def _select_visible_indices(
    backend: Any,
    selector: dict[str, Any],
    indices: list[int],
) -> dict[str, Any]:
    contiguous = _contiguous_index_range(indices)
    if contiguous is not None:
        start_index, end_index = contiguous
        return await select_grid_range(backend, selector, start_index, end_index)

    automation_id = selector.get("automation_id") or selector.get("automationId")
    multi_select = getattr(backend, "multi_select", None)
    if not automation_id or not callable(multi_select):
        return {
            "status": "BLOCKED",
            "reason": "non-contiguous grid identity selection requires multi-select backend",
            "requested": {"selected_indices": indices},
            "accepted": {
                "backend": (
                    "grid_select_range for contiguous rows or multi_select "
                    "for non-contiguous rows"
                )
            },
        }
    selected_count = await multi_select(str(automation_id), indices)
    if selected_count < len(indices):
        return {
            "status": "BLOCKED",
            "reason": "multi-select backend did not select all requested identity rows",
            "requested_indices": indices,
            "selected_count": selected_count,
        }
    return {
        "status": "PASS",
        "selected_indices": indices,
        "selected_count": selected_count,
    }


def _contiguous_index_range(indices: list[int]) -> tuple[int, int] | None:
    if not indices:
        return None
    start = min(indices)
    end = max(indices)
    if sorted(indices) != list(range(start, end + 1)):
        return None
    return start, end


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
    ensure_visible: bool = False,
    max_scrolls: int | None = None,
    scroll_settle_ms: int | None = None,
) -> dict[str, Any]:
    """Click one currently visible DataGrid row through a backend primitive."""
    identity_payload = _identity_payload(identity, columns)
    evidence_columns = _identity_columns(identity_payload, columns)
    ensure_visible_result: dict[str, Any] | None = None
    if ensure_visible:
        ensure_visible_result = await ensure_grid_row_visible(
            backend,
            selector,
            row_index=row_index,
            row_key=row_key,
            identity=identity_payload,
            rows=rows,
            columns=evidence_columns,
            max_scrolls=max_scrolls,
            scroll_settle_ms=scroll_settle_ms,
        )
        if not _passes(ensure_visible_result):
            result = dict(ensure_visible_result) if isinstance(ensure_visible_result, dict) else {}
            result["status"] = _blocked_status(result)
            result.setdefault("reason", "grid ensure-visible failed before row click")
            result["ensure_visible_result"] = ensure_visible_result
            result["action_skipped"] = True
            return result

    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity_payload,
        rows=rows,
        columns=evidence_columns,
    )
    if blocked is not None:
        return blocked
    visible_index = _visible_index(resolved)
    if visible_index is None:
        return {
            "status": "BLOCKED",
            "reason": "resolved row has no visible index",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }

    click_row = getattr(backend, "grid_click_row", None)
    if not callable(click_row):
        return {
            "status": "BLOCKED",
            "reason": "grid row click unavailable",
            "requested": {"adapter": "ui.grid.click_row"},
            "accepted": {"backend": "DataGrid backend with grid_click_row"},
            "next_step": "Use a FlaUI bridge backend that can click resolved grid rows.",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }
    result = await click_row(
        dict(selector),
        visible_index,
        column=column,
        columns=evidence_columns,
    )
    if not isinstance(result, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid row click returned non-object result",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
            "click_result": result,
        }
    output = dict(result)
    output.setdefault("status", "PASS")
    output["resolved_row"] = _compact_row_ref(resolved, identity_payload)
    if ensure_visible_result is not None:
        output["ensure_visible_result"] = ensure_visible_result
    return output


async def right_click_grid_row(
    backend: Any,
    selector: dict[str, Any],
    row_index: int | None = None,
    *,
    row_key: str | None = None,
    column: str | None = None,
    columns: list[str] | None = None,
    identity: Mapping[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
    ensure_visible: bool = False,
    max_scrolls: int | None = None,
    scroll_settle_ms: int | None = None,
) -> dict[str, Any]:
    """Right-click one currently visible DataGrid row through a backend primitive."""
    identity_payload = _identity_payload(identity, columns)
    evidence_columns = _identity_columns(identity_payload, columns)
    ensure_visible_result: dict[str, Any] | None = None
    if ensure_visible:
        ensure_visible_result = await ensure_grid_row_visible(
            backend,
            selector,
            row_index=row_index,
            row_key=row_key,
            identity=identity_payload,
            rows=rows,
            columns=evidence_columns,
            max_scrolls=max_scrolls,
            scroll_settle_ms=scroll_settle_ms,
        )
        if not _passes(ensure_visible_result):
            result = dict(ensure_visible_result) if isinstance(ensure_visible_result, dict) else {}
            result["status"] = _blocked_status(result)
            result.setdefault("reason", "grid ensure-visible failed before row right click")
            result["ensure_visible_result"] = ensure_visible_result
            result["action_skipped"] = True
            return result

    resolved, blocked = await resolve_visible_grid_row(
        backend,
        selector,
        row_index=row_index,
        row_key=row_key,
        identity=identity_payload,
        rows=rows,
        columns=evidence_columns,
    )
    if blocked is not None:
        return blocked
    visible_index = _visible_index(resolved)
    if visible_index is None:
        return {
            "status": "BLOCKED",
            "reason": "resolved row has no visible index",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }

    right_click_row = getattr(backend, "grid_right_click_row", None)
    if not callable(right_click_row):
        return {
            "status": "BLOCKED",
            "reason": "grid row right click unavailable",
            "requested": {"adapter": "ui.grid.right_click_row"},
            "accepted": {"backend": "DataGrid backend with grid_right_click_row"},
            "next_step": "Use a FlaUI bridge backend that can right-click resolved grid rows.",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
        }
    result = await right_click_row(
        dict(selector),
        visible_index,
        column=column,
        columns=evidence_columns,
    )
    if not isinstance(result, dict):
        return {
            "status": "BLOCKED",
            "reason": "grid row right click returned non-object result",
            "resolved_row": _compact_row_ref(resolved, identity_payload),
            "click_result": result,
        }
    output = dict(result)
    output.setdefault("status", "PASS")
    output["resolved_row"] = _compact_row_ref(resolved, identity_payload)
    if ensure_visible_result is not None:
        output["ensure_visible_result"] = ensure_visible_result
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


def _identity_payload(
    identity: Mapping[str, Any] | None,
    columns: list[str] | None,
) -> dict[str, Any]:
    if identity:
        return dict(identity)
    identity_columns = _identity_columns(None, columns)
    if len(identity_columns) == 1:
        return {"column": identity_columns[0]}
    if identity_columns:
        return {"columns": identity_columns}
    return {}


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

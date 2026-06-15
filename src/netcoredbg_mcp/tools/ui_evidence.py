"""High-signal UI evidence tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.state import DebugState
from ..ui.events import UIEventBufferStore
from ..ui.grid import (
    assert_grid_range,
    read_grid_selected_rows,
    read_grid_visible_rows,
    select_grid_range,
)
from ..ui.key_sequence import run_scoped_key_sequence
from ..ui.snapshots import (
    ALLOWED_UI_FIELDS,
    UISnapshotStore,
    capture_ui_snapshot,
    diff_ui_snapshots,
    invalid_ui_fields,
    query_ui_fields,
)

_GRID_ACTION_ALIASES = {"rows": "visible_rows"}
_GRID_CANONICAL_ACTIONS = ("visible_rows", "selected_rows", "select_range", "assert_range")
_GRID_ACCEPTED_ACTIONS = ("visible_rows", "rows", "selected_rows", "select_range", "assert_range")


def register_ui_evidence_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register high-signal UI evidence tools."""
    from mcp.types import ToolAnnotations

    backend_holder: dict[str, Any] = {"instance": None}

    def _get_backend() -> Any:
        if backend_holder["instance"] is None:
            from ..ui.backend import create_backend

            backend_holder["instance"] = create_backend(
                process_registry=session.process_registry,
            )
        return backend_holder["instance"]

    async def _ensure_ui_connected() -> Any:
        from ..ui import NoActiveSessionError, NoProcessIdError

        if session.state.state == DebugState.IDLE:
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        backend = _get_backend()
        if backend.process_id != process_id:
            from ..ui.backend import connect_backend

            await connect_backend(
                backend,
                process_id,
                stealth_mode=getattr(session, "stealth_mode", False),
            )
        return backend

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_key_sequence(
        ctx: Context,
        modifiers: list[str],
        keys: list[str],
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Send keys while holding modifiers and report cleanup evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            backend = await _ensure_ui_connected()
            result = await run_scoped_key_sequence(
                backend,
                _selector(automation_id, name, control_type, root_id, xpath),
                modifiers=modifiers,
                keys=keys,
            )
            return build_response(data=result, state=session.state.state)
        except ValueError as exc:
            return build_response(
                data={"status": "FAIL", "reason": "invalid key sequence", "error": str(exc)},
                state=session.state.state,
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_grid(
        ctx: Context,
        action: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        start_index: int | None = None,
        end_index: int | None = None,
    ) -> dict:
        """Read, select, or assert WPF DataGrid row evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if not selector:
                return build_response(
                    data={"status": "FAIL", "reason": "invalid selector"},
                    state=session.state.state,
                )

            canonical_action = _GRID_ACTION_ALIASES.get(action, action)
            if canonical_action not in _GRID_CANONICAL_ACTIONS:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown grid action",
                        "action": action,
                        "accepted_actions": list(_GRID_ACCEPTED_ACTIONS),
                        "aliases": dict(_GRID_ACTION_ALIASES),
                        "next_step": "Use one of the accepted ui_grid actions.",
                    },
                    state=session.state.state,
                )

            backend = await _ensure_ui_connected()
            if canonical_action == "visible_rows":
                result = await read_grid_visible_rows(backend, selector)
            elif canonical_action == "selected_rows":
                result = await read_grid_selected_rows(backend, selector)
            elif canonical_action == "select_range":
                start, end = _require_range(start_index, end_index)
                result = await select_grid_range(backend, selector, start, end)
            elif canonical_action == "assert_range":
                start, end = _require_range(start_index, end_index)
                result = await assert_grid_range(backend, selector, start, end)
            if canonical_action != action and isinstance(result, dict):
                result = dict(result)
                result["requested_action"] = action
                result["canonical_action"] = canonical_action
            return build_response(data=result, state=session.state.state)
        except ValueError as exc:
            return build_response(
                data={"status": "FAIL", "reason": "invalid grid request", "error": str(exc)},
                state=session.state.state,
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_query(
        ctx: Context,
        fields: list[str],
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        max_results: int = 20,
    ) -> dict:
        """Read selected UI fields without dumping the full tree."""
        try:
            invalid = invalid_ui_fields(fields)
            if invalid:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown UI fields",
                        "invalid_fields": invalid,
                        "allowed_fields": list(ALLOWED_UI_FIELDS),
                    },
                    state=session.state.state,
                )
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            backend = await _ensure_ui_connected()
            result = await query_ui_fields(
                backend,
                _selector(automation_id, name, control_type, root_id, xpath),
                fields=fields,
                max_results=max_results,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_snapshot(
        ctx: Context,
        snapshot: str,
        fields: list[str],
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        max_results: int = 20,
    ) -> dict:
        """Capture a named field-limited UI snapshot."""
        try:
            invalid = invalid_ui_fields(fields)
            if invalid:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown UI fields",
                        "invalid_fields": invalid,
                        "allowed_fields": list(ALLOWED_UI_FIELDS),
                    },
                    state=session.state.state,
                )
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            backend = await _ensure_ui_connected()
            result = await capture_ui_snapshot(
                backend,
                _snapshot_store(),
                name=snapshot,
                selector=_selector(automation_id, name, control_type, root_id, xpath),
                fields=fields,
                max_results=max_results,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_diff(
        ctx: Context,
        before: str,
        after: str,
        fields: list[str],
    ) -> dict:
        """Diff two named UI snapshots."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = diff_ui_snapshots(
                _snapshot_store(),
                before,
                after,
                fields=fields,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_events(
        ctx: Context,
        action: str,
        buffer_id: str,
        fields: list[str] | None = None,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        max_events: int = 20,
    ) -> dict:
        """Start, read, or stop a bounded selector-scoped UI event buffer."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            store = _event_store()
            if action == "start":
                requested_fields = list(fields or ["focus", "selection", "text"])
                invalid = invalid_ui_fields(requested_fields)
                if invalid:
                    return build_response(
                        data={
                            "status": "FAIL",
                            "reason": "unknown UI fields",
                            "invalid_fields": invalid,
                            "allowed_fields": list(ALLOWED_UI_FIELDS),
                        },
                        state=session.state.state,
                    )
                backend = await _ensure_ui_connected()
                result = await store.start(
                    backend,
                    buffer_id=buffer_id,
                    selector=_selector(automation_id, name, control_type, root_id, xpath),
                    fields=requested_fields,
                    max_events=max_events,
                )
            elif action == "read":
                result = await store.read(buffer_id)
            elif action == "stop":
                result = store.stop(buffer_id)
            else:
                result = {
                    "status": "FAIL",
                    "reason": "unknown UI events action",
                    "action": action,
                }
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    def _snapshot_store() -> UISnapshotStore:
        return UISnapshotStore(session.runtime_smoke.ui_snapshots)

    def _event_store() -> UIEventBufferStore:
        return UIEventBufferStore(session.runtime_smoke.ui_event_buffers)


def _selector(
    automation_id: str | None,
    name: str | None,
    control_type: str | None,
    root_id: str | None,
    xpath: str | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if automation_id:
        result["automation_id"] = automation_id
    if name:
        result["name"] = name
    if control_type:
        result["control_type"] = control_type
    if root_id:
        result["root_id"] = root_id
    if xpath:
        result["xpath"] = xpath
    return result


def _require_range(start_index: int | None, end_index: int | None) -> tuple[int, int]:
    if start_index is None or end_index is None:
        raise ValueError("start_index and end_index are required for range actions")
    return start_index, end_index

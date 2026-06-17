"""High-signal UI evidence tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.runtime_smoke_operations import ui_operation_adapters
from ..session.state import DebugState
from ..ui.events import UIEventBufferStore
from ..ui.focus import assert_focus
from ..ui.grid import (
    assert_grid_range,
    click_grid_row,
    read_grid_selected_rows,
    read_grid_state,
    read_grid_visible_rows,
    select_grid_range,
    select_grid_row,
    snapshot_grid,
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
from ..ui.text import assert_text_selection, read_textbox_state

_GRID_ACTION_ALIASES = {
    "rows": "visible_rows",
    "cells": "snapshot",
    "cell_values": "snapshot",
    "selected": "selected_rows",
    "selection": "selected_rows",
    "state": "get_state",
}
_GRID_CANONICAL_ACTIONS = (
    "visible_rows",
    "snapshot",
    "selected_rows",
    "select_range",
    "select_row",
    "click_row",
    "assert_range",
    "get_state",
)
_GRID_ACCEPTED_ACTIONS = (
    "visible_rows",
    "rows",
    "snapshot",
    "cells",
    "cell_values",
    "selected_rows",
    "selected",
    "selection",
    "select_range",
    "select_row",
    "click_row",
    "assert_range",
    "get_state",
    "state",
)
_FOCUS_READ_ACTIONS = ("assert",)
_TEXT_ACTION_ALIASES = {"state": "get_state"}
_TEXT_ACTIONS = ("read", "get_state", "state", "assert_selection", "set_text")
_PROPERTY_READ_ACTIONS = ("read",)
_TEXT_PROPERTY_NAMES = {"text", "value", "valuetext"}
_PROPERTY_KEY_ALIASES = {
    "automationid": "automationId",
    "automation_id": "automationId",
    "name": "name",
    "controltype": "controlType",
    "control_type": "controlType",
    "classname": "className",
    "class_name": "className",
}


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
    async def ui_text(
        ctx: Context,
        action: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        selection_start: int | None = None,
        selection_end: int | None = None,
        text: str | None = None,
    ) -> dict:
        """Read or safely replace bounded TextBox/text evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            canonical_action = _TEXT_ACTION_ALIASES.get(action, action)
            if action not in _TEXT_ACTIONS:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown text action",
                        "action": action,
                        "accepted_actions": list(_TEXT_ACTIONS),
                        "next_step": (
                            'Use ui_text(action="read"|"get_state"|"assert_selection"|"set_text") '
                            "for bounded TextBox evidence."
                        ),
                    },
                    state=session.state.state,
                )

            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if not selector:
                return build_response(
                    data={"status": "FAIL", "reason": "invalid selector"},
                    state=session.state.state,
                )

            backend = await _ensure_ui_connected()
            if canonical_action == "set_text":
                if text is None:
                    return build_response(
                        data={
                            "status": "FAIL",
                            "reason": "text is required",
                            "action": action,
                        },
                        state=session.state.state,
                    )
                backend_provider = _static_backend_provider(backend)
                adapters = ui_operation_adapters(backend_provider)
                result = await adapters["ui.text.set_text"](selector=selector, text=text)
                return build_response(
                    data=_strip_unbounded_evidence_value(result),
                    state=session.state.state,
                )
            if canonical_action == "get_state":
                return build_response(
                    data=await read_textbox_state(backend, selector),
                    state=session.state.state,
                )
            if canonical_action == "assert_selection":
                if selection_start is None or selection_end is None:
                    return build_response(
                        data={
                            "status": "FAIL",
                            "reason": "selection_start and selection_end are required",
                            "action": action,
                        },
                        state=session.state.state,
                    )
                return build_response(
                    data=await assert_text_selection(
                        backend,
                        selector,
                        selection_start=selection_start,
                        selection_end=selection_end,
                    ),
                    state=session.state.state,
                )

            result = await backend.extract_text(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(selector, result=_bounded_text_result(result)),
                    state=session.state.state,
                )
            return build_response(
                data=_bounded_text_read_result(selector, result),
                state=session.state.state,
            )
        except Exception as exc:
            result = {"status": "BLOCKED", "reason": str(exc)}
            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(selector, result=result),
                    state=session.state.state,
                )
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_property(
        ctx: Context,
        action: str,
        property: str | None = None,
        property_name: str | None = None,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Read bounded UI property evidence without mutation side effects."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if action not in _PROPERTY_READ_ACTIONS:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown property action",
                        "action": action,
                        "accepted_actions": list(_PROPERTY_READ_ACTIONS),
                        "next_step": (
                            "Use ui_property(action=\"read\") for read-only "
                            "property evidence."
                        ),
                    },
                    state=session.state.state,
                )

            requested_property = str(property_name or property or "")
            if not requested_property:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "invalid property",
                        "accepted_properties": _accepted_property_names(),
                        "next_step": "Provide property or property_name to read.",
                    },
                    state=session.state.state,
                )

            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if not selector:
                return build_response(
                    data={"status": "FAIL", "reason": "invalid selector"},
                    state=session.state.state,
                )

            backend = await _ensure_ui_connected()
            selector_kwargs = {
                "automation_id": automation_id,
                "name": name,
                "control_type": control_type,
                "root_id": root_id,
                "xpath": xpath,
            }
            if requested_property.lower() in _TEXT_PROPERTY_NAMES:
                result = await backend.extract_text(**selector_kwargs)
                if _is_selector_miss(result):
                    return build_response(
                        data=_selector_blocked(
                            selector,
                            result=_bounded_property_backend_result(result),
                        ),
                        state=session.state.state,
                    )
                return build_response(
                    data=_bounded_property_result(
                        selector,
                        requested_property,
                        result,
                        value=_text_property_value(result),
                        source=result.get("source") if isinstance(result, dict) else None,
                    ),
                    state=session.state.state,
                )

            result = await backend.find_element(**selector_kwargs)
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(
                        selector,
                        result=_bounded_property_backend_result(result),
                    ),
                    state=session.state.state,
                )
            key = _PROPERTY_KEY_ALIASES.get(requested_property.lower(), requested_property)
            return build_response(
                data=_bounded_property_result(
                    selector,
                    requested_property,
                    result,
                    value=_property_value(result, key),
                ),
                state=session.state.state,
            )
        except Exception as exc:
            result = {"status": "BLOCKED", "reason": str(exc)}
            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(selector, result=result),
                    state=session.state.state,
                )
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_focus(
        ctx: Context,
        action: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Read bounded focus evidence for a selector without moving focus."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if action not in _FOCUS_READ_ACTIONS:
                return build_response(
                    data={
                        "status": "FAIL",
                        "reason": "unknown focus action",
                        "action": action,
                        "accepted_actions": list(_FOCUS_READ_ACTIONS),
                        "next_step": "Use ui_focus(action=\"assert\") for read-only focus proof.",
                    },
                    state=session.state.state,
                )

            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if not selector:
                return build_response(
                    data={"status": "FAIL", "reason": "invalid selector"},
                    state=session.state.state,
                )

            backend = await _ensure_ui_connected()
            result = await assert_focus(backend, selector)
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(selector, result=_bounded_focus_result(result)),
                    state=session.state.state,
                )
            return build_response(
                data=_bounded_focus_result(result, selector=selector),
                state=session.state.state,
            )
        except Exception as exc:
            result = {"status": "BLOCKED", "reason": str(exc)}
            selector = _selector(automation_id, name, control_type, root_id, xpath)
            if _is_selector_miss(result):
                return build_response(
                    data=_selector_blocked(selector, result=result),
                    state=session.state.state,
                )
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
        row_index: int | None = None,
        row_key: str | None = None,
        column: str | None = None,
        rows: dict[str, Any] | None = None,
        columns: list[str] | None = None,
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
            elif canonical_action == "snapshot":
                result = await snapshot_grid(backend, selector, rows=rows, columns=columns)
            elif canonical_action == "selected_rows":
                result = await read_grid_selected_rows(backend, selector, columns=columns)
            elif canonical_action == "get_state":
                result = await read_grid_state(
                    backend,
                    selector,
                    rows=rows,
                    columns=columns,
                )
            elif canonical_action == "select_range":
                start, end = _require_range(start_index, end_index)
                result = await select_grid_range(backend, selector, start, end)
                if _passes(result):
                    result = await _confirm_grid_selection(
                        backend,
                        selector,
                        start=start,
                        end=end,
                        columns=columns,
                        selection_result=result,
                    )
            elif canonical_action == "select_row":
                result = await select_grid_row(
                    backend,
                    selector,
                    row_index=row_index,
                    row_key=row_key,
                    columns=columns,
                    rows=rows,
                )
            elif canonical_action == "click_row":
                result = await click_grid_row(
                    backend,
                    selector,
                    row_index=row_index,
                    row_key=row_key,
                    column=column,
                    columns=columns,
                    rows=rows,
                )
            elif canonical_action == "assert_range":
                start, end = _require_range(start_index, end_index)
                result = await assert_grid_range(backend, selector, start, end)
            if isinstance(result, dict):
                result = _strip_unbounded_evidence_value(dict(result))
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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
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
                backend = await _ensure_ui_connected()
                result = await store.read(buffer_id, backend=backend)
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

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_monitor_start(
        ctx: Context,
        monitor_id: str,
        fields: list[str] | None = None,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
        max_events: int = 20,
    ) -> dict:
        """Start a selector-scoped semantic UI monitor."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
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
            result = await _event_store().monitor_start(
                backend,
                monitor_id=monitor_id,
                selector=_selector(automation_id, name, control_type, root_id, xpath),
                fields=requested_fields,
                max_events=max_events,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_monitor_poll(
        ctx: Context,
        monitor_id: str,
        after_cursor: int = 0,
    ) -> dict:
        """Poll a semantic UI monitor once and return cursor-filtered events."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            backend = await _ensure_ui_connected()
            result = await _event_store().monitor_poll(
                monitor_id,
                after_cursor=after_cursor,
                backend=backend,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_monitor_wait(
        ctx: Context,
        monitor_id: str,
        after_cursor: int = 0,
        timeout_ms: int = 1000,
        poll_interval_ms: int = 100,
    ) -> dict:
        """Wait for a semantic UI monitor event or return a bounded timeout."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = await _event_store().monitor_wait(
                monitor_id,
                after_cursor=after_cursor,
                timeout_ms=timeout_ms,
                poll_interval_ms=poll_interval_ms,
                backend_provider=_ensure_ui_connected,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def ui_monitor_events(
        ctx: Context,
        monitor_id: str,
        after_cursor: int = 0,
    ) -> dict:
        """Return retained semantic UI monitor history without polling."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = _event_store().monitor_events(
                monitor_id,
                after_cursor=after_cursor,
            )
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


def _is_selector_miss(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status", "PASS")).upper()
    if status not in {"FAIL", "BLOCKED", "NOT_FOUND"}:
        return False
    if result.get("found") is False:
        return True
    reason = str(result.get("reason") or result.get("error") or "").lower()
    return any(
        marker in reason
        for marker in (
            "not found",
            "not_found",
            "no element",
            "no such element",
            "no matching element",
            "selector not found",
            "unable to find",
        )
    )


def _selector_blocked(
    selector: dict[str, Any],
    *,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "selector not found",
        "requested": {"selector": selector},
        "accepted": {
            "selector_keys": [
                "automation_id",
                "name",
                "control_type",
                "root_id",
                "xpath",
            ]
        },
        "next_step": "Inspect the fixture UI tree and update the selector.",
        "result": result,
    }


def _bounded_text_read_result(selector: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "text backend returned non-object result",
            "selector": selector,
            "result": result,
        }
    bounded = _bounded_text_result(result)
    bounded.setdefault("status", "PASS")
    bounded.setdefault("text", str(result.get("text", "")))
    bounded["selector"] = selector
    return bounded


def _bounded_text_result(result: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "status",
        "text",
        "source",
        "found",
        "reason",
        "error",
        "matched",
        "automation_id",
        "name",
        "control_type",
    )
    return {key: result[key] for key in allowed_keys if key in result}


def _accepted_property_names() -> list[str]:
    return [
        "AutomationId",
        "Name",
        "ControlType",
        "ClassName",
        "Text",
        "Value",
        "ValueText",
    ]


def _text_property_value(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    value = result.get("text")
    return str(value) if value is not None else None


def _property_value(result: Any, key: str) -> Any:
    if not isinstance(result, dict):
        return None
    if key in result:
        return result[key]
    key_lower = key.lower()
    for candidate_key, candidate_value in result.items():
        if candidate_key.lower() == key_lower:
            return candidate_value
    return None


def _bounded_property_backend_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "property backend returned non-object result",
            "result": _strip_unbounded_evidence_value(result),
        }
    allowed_keys = (
        "status",
        "found",
        "reason",
        "error",
        "matched",
        "unsupported",
        "backend",
        "requested",
        "accepted",
        "next_step",
        "source",
    )
    return {
        key: _strip_unbounded_evidence_value(result[key])
        for key in allowed_keys
        if key in result
    }


def _bounded_property_result(
    selector: dict[str, Any],
    property_name: str,
    result: Any,
    *,
    value: Any,
    source: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "property backend returned non-object result",
            "property": property_name,
            "value": value,
            "selector": selector,
            "result": _strip_unbounded_evidence_value(result),
        }
    bounded = _bounded_property_backend_result(result)
    bounded.setdefault("status", "PASS")
    bounded["property"] = property_name
    bounded["value"] = _strip_unbounded_evidence_value(value)
    if source is not None:
        bounded["source"] = _strip_unbounded_evidence_value(source)
    bounded["selector"] = selector
    return bounded


def _bounded_focus_result(
    result: Any,
    *,
    selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "focus backend returned non-object result",
            "selector": selector or {},
            "result": result,
        }
    allowed_keys = (
        "status",
        "focused",
        "reason",
        "unsupported",
        "backend",
        "expected",
        "actual",
        "requested",
        "accepted",
        "next_step",
        "error",
    )
    bounded = {
        key: _strip_unbounded_evidence_value(result[key])
        for key in allowed_keys
        if key in result
    }
    bounded.setdefault("status", "PASS")
    if selector is not None:
        bounded["selector"] = selector
    return bounded


def _strip_unbounded_evidence_value(value: Any) -> Any:
    unbounded_keys = {"full_tree", "raw_tree", "ui_tree", "window_tree"}
    if isinstance(value, dict):
        return {
            key: _strip_unbounded_evidence_value(item)
            for key, item in value.items()
            if key not in unbounded_keys
        }
    if isinstance(value, list):
        return [_strip_unbounded_evidence_value(item) for item in value]
    return value


def _static_backend_provider(backend: Any) -> Callable[[], Any]:
    async def _provider() -> Any:
        return backend

    return _provider


async def _confirm_grid_selection(
    backend: Any,
    selector: dict[str, Any],
    *,
    start: int,
    end: int,
    columns: list[str] | None,
    selection_result: Any,
) -> dict[str, Any]:
    confirmation = await read_grid_selected_rows(backend, selector, columns=columns)
    requested_range = {"start": start, "end": end}
    if not isinstance(confirmation, dict):
        return {
            "status": "BLOCKED",
            "confirmed_selection": False,
            "reason": "selected row confirmation returned non-object result",
            "requested_range": requested_range,
            "selection_result": _strip_unbounded_evidence_value(selection_result),
            "confirmation_result": _strip_unbounded_evidence_value(confirmation),
        }
    if not _passes(confirmation):
        result = _strip_unbounded_evidence_value(confirmation)
        if not isinstance(result, dict):
            result = {}
        result["status"] = str(result.get("status") or "BLOCKED")
        if result["status"].upper() == "PASS":
            result["status"] = "BLOCKED"
        result.setdefault("reason", "selected row confirmation failed")
        result["confirmed_selection"] = False
        result["requested_range"] = requested_range
        result["selection_result"] = _strip_unbounded_evidence_value(selection_result)
        return result

    selected_rows = confirmation.get("selected_rows")
    if not isinstance(selected_rows, list):
        return {
            "status": "BLOCKED",
            "confirmed_selection": False,
            "reason": "selected row confirmation did not include selected_rows",
            "requested_range": requested_range,
            "selection_result": _strip_unbounded_evidence_value(selection_result),
            "confirmation_result": _strip_unbounded_evidence_value(confirmation),
        }

    observed_indices = sorted(
        index
        for row in selected_rows
        if isinstance(row, dict)
        for index in [_row_index(row)]
        if index is not None
    )
    expected_indices = _inclusive_range_indices(start, end)
    if observed_indices != expected_indices:
        return {
            "status": "FAIL",
            "confirmed_selection": False,
            "reason": "selected row confirmation failed",
            "requested_range": requested_range,
            "observed_selected_indices": observed_indices,
            "selected_rows": _strip_unbounded_evidence_value(selected_rows),
            "selection_result": _strip_unbounded_evidence_value(selection_result),
            "confirmation_result": _strip_unbounded_evidence_value(confirmation),
        }

    stripped_selection_result = _strip_unbounded_evidence_value(selection_result)
    result = (
        stripped_selection_result
        if isinstance(stripped_selection_result, dict)
        else {}
    )
    result["status"] = "PASS"
    result["confirmed_selection"] = True
    result["selected_range"] = requested_range
    result["observed_selected_indices"] = observed_indices
    result["selected_rows"] = _strip_unbounded_evidence_value(selected_rows)
    return result


def _passes(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("status", "PASS")).upper() in {"PASS", "OK", "SUCCESS"}


def _inclusive_range_indices(start: int, end: int) -> list[int]:
    lower, upper = sorted((start, end))
    return list(range(lower, upper + 1))


def _row_index(row: dict[str, Any]) -> int | None:
    raw_index = row.get("index", row.get("row_index"))
    if isinstance(raw_index, bool):
        return None
    if isinstance(raw_index, int):
        return raw_index
    if isinstance(raw_index, str):
        try:
            return int(raw_index.strip())
        except ValueError:
            return None
    return None


def _require_range(start_index: int | None, end_index: int | None) -> tuple[int, int]:
    if start_index is None or end_index is None:
        raise ValueError("start_index and end_index are required for range actions")
    return start_index, end_index

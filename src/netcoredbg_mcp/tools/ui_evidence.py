"""High-signal UI evidence tools."""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.state import DebugState
from ..ui.grid import (
    assert_grid_range,
    read_grid_selected_rows,
    read_grid_visible_rows,
    select_grid_range,
)
from ..ui.key_sequence import run_scoped_key_sequence


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
            await backend.connect(process_id)
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

            backend = await _ensure_ui_connected()
            if action == "visible_rows":
                result = await read_grid_visible_rows(backend, selector)
            elif action == "selected_rows":
                result = await read_grid_selected_rows(backend, selector)
            elif action == "select_range":
                start, end = _require_range(start_index, end_index)
                result = await select_grid_range(backend, selector, start, end)
            elif action == "assert_range":
                start, end = _require_range(start_index, end_index)
                result = await assert_grid_range(backend, selector, start, end)
            else:
                result = {
                    "status": "FAIL",
                    "reason": "unknown grid action",
                    "action": action,
                }
            return build_response(data=result, state=session.state.state)
        except ValueError as exc:
            return build_response(
                data={"status": "FAIL", "reason": "invalid grid request", "error": str(exc)},
                state=session.state.state,
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)


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

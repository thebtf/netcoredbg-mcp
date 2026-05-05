"""Breakpoint management tools."""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager

logger = logging.getLogger(__name__)


def register_breakpoint_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
    notify_breakpoints_changed: Callable[[Any], Coroutine],
    resolve_project_root: Callable[..., Coroutine],
) -> None:
    """Register breakpoint management tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def add_breakpoint(
        ctx: Context,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> dict:
        """
        Add a breakpoint at a specific line.

        IMPORTANT TIMING:
        - Breakpoints set BEFORE start_debug only work for debugging app startup.
        - For UI apps (WPF/WinForms): remove breakpoints before launch, then add them
          AFTER the UI is fully loaded. Otherwise the app may hang during initialization.
        - When debugging UI issues: wait for app to be fully interactive before setting
          breakpoints in event handlers.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.

        Args:
            file: Absolute path to source file
            line: Line number (1-based)
            condition: Optional condition expression
            hit_condition: Optional hit count condition
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file, must_exist=True)
            bp = await session.add_breakpoint(validated_file, line, condition, hit_condition)
            await notify_breakpoints_changed(ctx)
            return build_response(
                data={
                    "file": bp.file,
                    "line": bp.line,
                    "condition": bp.condition,
                    "verified": bp.verified,
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def remove_breakpoint(ctx: Context, file: str, line: int) -> dict:
        """Remove a breakpoint from a specific line.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file)
            removed = await session.remove_breakpoint(validated_file, line)
            await notify_breakpoints_changed(ctx)
            if not removed:
                # Look for a breakpoint at this location via its DAP-adjusted line
                for bp in session.breakpoints.get_for_file(validated_file):
                    if bp.dap_line == line:
                        return build_response(
                            data={
                                "removed": False,
                                "hint": (
                                    f"No breakpoint at requested line {line}. "
                                    f"A breakpoint requested at line {bp.line} was "
                                    f"adjusted by DAP to line {bp.dap_line} (typical "
                                    f"for async state machines). "
                                    f"Remove it with remove_breakpoint(line={bp.line})."
                                ),
                            },
                            state=session.state.state,
                        )
            return build_response(
                data={"removed": removed},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def list_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """List all breakpoints or breakpoints in a specific file.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.
        """
        try:
            def _bp_dict(file_path: str, bp) -> dict:
                norm = session.breakpoints._normalize_path(file_path)
                hit_count = session.state.hit_counts.get((norm, bp.line), 0)
                return {
                    "line": bp.line,
                    "dap_line": bp.dap_line,
                    "condition": bp.condition,
                    "verified": bp.verified,
                    "hit_count": hit_count,
                }

            if file:
                # Resolve project root from MCP context
                await resolve_project_root(ctx, session)
                # Validate file path if provided
                validated_file = session.validate_path(file)
                bps = session.breakpoints.get_for_file(validated_file)
                result = {
                    validated_file: [_bp_dict(validated_file, bp) for bp in bps]
                }
            else:
                all_bps = session.breakpoints.get_all()
                result = {
                    f: [_bp_dict(f, bp) for bp in bps]
                    for f, bps in all_bps.items()
                }
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def clear_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """Clear breakpoints from a file or all files.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            validated_file = None
            if file:
                # Resolve project root from MCP context
                await resolve_project_root(ctx, session)
                # Validate file path if provided
                validated_file = session.validate_path(file)
            count = await session.clear_breakpoints(validated_file)
            await notify_breakpoints_changed(ctx)
            return build_response(
                data={"removed": count},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def add_function_breakpoint(
        ctx: Context,
        function_name: str,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> dict:
        """Set a breakpoint on a function by name.

        Breaks when the named function is entered. This is useful when you know
        the method name but not the exact line number.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.

        Args:
            function_name: Full or partial function name to break on
            condition: Optional condition expression
            hit_condition: Optional hit count condition
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            bp = await session.add_function_breakpoint(function_name, condition, hit_condition)
            await notify_breakpoints_changed(ctx)
            return build_response(
                data={"function": bp.name, "condition": bp.condition, "verified": bp.verified},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def configure_exceptions(
        ctx: Context,
        filters: list[str] | None = None,
    ) -> dict:
        """Configure which exceptions should pause the debugger.

        Controls exception breakpoints — when the debugger should stop on exceptions.
        By default, no exception filters are set (exceptions don't pause unless uncaught).

        Common filters supported by netcoredbg:
        - "all": Break on all exceptions (caught and uncaught)
        - "user-unhandled": Break on exceptions not handled in user code

        Pass an empty list to disable all exception breakpoints.

        Escape hatch: see the dap-escape-hatch prompt for unwrapped DAP requests.

        Args:
            filters: List of exception filter names. Pass [] to disable.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            success = await session.configure_exception_breakpoints(filters or [])
            if not success:
                return build_error_response(
                    "Failed to set exception breakpoints",
                    state=session.state.state,
                )
            return build_response(
                data={"filters": filters or [], "configured": True},
                state=session.state.state,
                message=f"Exception breakpoints configured: {filters or []}",
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

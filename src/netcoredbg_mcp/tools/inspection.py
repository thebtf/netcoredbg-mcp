"""Variable inspection and evaluation tools."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context, FastMCP
    from ..session import SessionManager
    from ..mux import SessionOwnership

from ..response import build_error_response, build_response
from ..utils.source import read_source_context

logger = logging.getLogger(__name__)


def register_inspection_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register variable inspection and evaluation tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_threads() -> dict:
        """Get all threads in the debugged process."""
        try:
            threads = await session.get_threads()
            return build_response(
                data=[{"id": t.id, "name": t.name} for t in threads],
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_call_stack(thread_id: int | None = None, levels: int = 20) -> dict:
        """Get the call stack for a thread.

        Diagnostic: Set NETCOREDBG_STACKTRACE_DELAY_MS env var to add delay before
        stackTrace request. This helps diagnose timing issues with ICorDebugThread3.
        Example: NETCOREDBG_STACKTRACE_DELAY_MS=300
        """
        try:
            # Diagnostic test: configurable delay before stackTrace
            # If delay helps, root cause is timing (CLR not ready)
            # If delay doesn't help, root cause is binary mismatch
            delay_ms = int(os.environ.get("NETCOREDBG_STACKTRACE_DELAY_MS", "0"))
            if delay_ms > 0:
                logger.info(f"[DIAGNOSTIC] Applying {delay_ms}ms delay before stackTrace request")
                await asyncio.sleep(delay_ms / 1000.0)

            frames = await session.get_stack_trace(thread_id, 0, levels)
            frames_data = [
                {
                    "id": f.id, "name": f.name, "source": f.source,
                    "line": f.line, "column": f.column,
                }
                for f in frames
            ]

            # Read source context for the top frame
            source_context = None
            if frames:
                source_context = read_source_context(frames[0].source, frames[0].line)

            data = {"frames": frames_data}
            if source_context is not None:
                data["source_context"] = source_context

            return build_response(data=data, state=session.state.state)
        except Exception as e:
            error_msg = str(e)
            # Enhanced error message for E_NOINTERFACE
            if "0x80004002" in error_msg or "E_NOINTERFACE" in error_msg.upper():
                logger.warning(
                    "[DIAGNOSTIC] E_NOINTERFACE on ICorDebugThread3. "
                    "Try setting NETCOREDBG_STACKTRACE_DELAY_MS=300 to test timing hypothesis."
                )
            return build_error_response(error_msg, state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_scopes(frame_id: int | None = None) -> dict:
        """Get variable scopes for a stack frame."""
        try:
            scopes = await session.get_scopes(frame_id)
            return build_response(
                data=[
                    {
                        "name": s.get("name", ""),
                        "variablesReference": s.get("variablesReference", 0),
                    }
                    for s in scopes
                ],
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_variables(variables_reference: int) -> dict:
        """Get variables for a scope or structured variable."""
        try:
            variables = await session.get_variables(variables_reference)
            return build_response(
                data=[
                    {
                        "name": v.name,
                        "value": v.value,
                        "type": v.type,
                        "variablesReference": v.variables_reference,
                    }
                    for v in variables
                ],
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def evaluate_expression(expression: str, frame_id: int | None = None) -> dict:
        """Evaluate an expression in the current debug context."""
        try:
            result = await session.evaluate(expression, frame_id)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def set_variable(
        ctx: Context,
        variables_reference: int,
        name: str,
        value: str,
    ) -> dict:
        """Set a variable's value during debugging.

        Modifies a variable in the current scope. The program must be stopped.
        Use get_variables first to find the variables_reference for the scope.

        Args:
            variables_reference: Reference from get_scopes or get_variables
            name: Variable name to modify
            value: New value as a string expression
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            result = await session.set_variable(variables_reference, name, value)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_exception_info(thread_id: int | None = None) -> dict:
        """Get information about the current exception."""
        try:
            info = await session.get_exception_info(thread_id)
            return build_response(data=info, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

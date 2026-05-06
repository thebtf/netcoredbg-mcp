"""Runtime smoke composite tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response, extend_next_actions
from ..session import SessionManager
from ..session.hygiene import HygienePreflightResult, RuntimeHygieneService


def register_runtime_smoke_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
    resolve_project_root: Callable[..., Awaitable[Any]],
) -> None:
    """Register runtime smoke composite tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def debug_hygiene_preflight(
        ctx: Context,
        file: str | None = None,
        clear_breakpoints: bool = True,
        clear_trace_log: bool = True,
        clear_exception_filters: bool = False,
    ) -> dict:
        """Clear stale debugger state and report a compact hygiene result."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            validated_file = None
            if file:
                await resolve_project_root(ctx, session)
                try:
                    validated_file = session.validate_path(file)
                except Exception as exc:
                    return _build_hygiene_response(
                        session,
                        HygienePreflightResult.validation_failed(str(exc)),
                    )

            service = getattr(session, "hygiene", None) or RuntimeHygieneService(session)
            result = await service.preflight(
                file=validated_file,
                clear_breakpoints=clear_breakpoints,
                clear_trace_log=clear_trace_log,
                clear_exception_filters=clear_exception_filters,
            )
            return _build_hygiene_response(session, result)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)


def _build_hygiene_response(
    session: SessionManager,
    result: HygienePreflightResult,
) -> dict:
    message = (
        "Hygiene preflight passed."
        if result.status.value == "PASS"
        else "Hygiene preflight failed."
    )
    return build_response(
        data=result.to_dict(),
        state=session.state.state,
        next_actions=extend_next_actions(
            session.state.state,
            ["debug_hygiene_preflight"],
        ),
        message=message,
    )

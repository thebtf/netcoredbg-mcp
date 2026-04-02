"""Process management tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context, FastMCP
    from ..session import SessionManager

from ..response import build_error_response, build_response

logger = logging.getLogger(__name__)


def register_process_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register process management tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def cleanup_processes(ctx: Context, force: bool = False) -> dict:
        """View or terminate tracked debug processes.

        Without force: shows all tracked processes and their status (alive/dead).
        With force=True: terminates all tracked processes (netcoredbg + debuggees).

        Use this instead of manual taskkill. The server tracks which processes
        it spawned — no risk of killing unrelated processes.

        Args:
            force: If True, terminate all tracked processes. If False, just show status.
        """
        try:
            if force:
                access_error = check_session_access(ctx)
                if access_error:
                    return build_error_response(access_error, state=session.state.state)

            registry = session.process_registry
            status_list = registry.status()

            if force:
                terminated = registry.cleanup_all()
                return build_response(
                    data={
                        "action": "cleanup",
                        "terminated": terminated,
                        "processes": status_list,
                    },
                    state=session.state.state,
                    message=f"Terminated {terminated} processes.",
                )

            return build_response(
                data={
                    "action": "status",
                    "processes": status_list,
                    "total": len(status_list),
                    "alive": sum(1 for p in status_list if p.get("alive")),
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

"""Process management tools."""

import logging
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response
from ..session import SessionManager

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
        """View process observations or join live trusted-producer cleanup.

        Without force, return observation status. With force=True, stop owned
        adapters and bridges through their captured finalizers. PID observations
        and old PID files never authorize termination; attached targets detach.
        `terminated` counts native-confirmed forced roots, not every exit or tree size.

        Args:
            force: Join owner cleanup instead of only showing status.
        """
        try:
            if force:
                access_error = check_session_access(ctx)
                if access_error:
                    return build_error_response(access_error, state=session.state.state)

            registry = session.process_registry
            status_list = registry.status()

            if force:
                result = await registry.cleanup_all()
                data = {
                    "action": "cleanup",
                    "terminated": result.terminated,
                    "processes": status_list,
                    "complete": result.complete,
                    "remaining_owners": result.remaining_owners,
                    "errors": result.errors,
                    "tree_terminated": None,
                }
                if not result.complete:
                    response = build_error_response(
                        "Owner cleanup incomplete; unfinished owners remain registered.",
                        state=session.state.state,
                    )
                    response["data"] = data
                    return response
                return build_response(
                    data=data,
                    state=session.state.state,
                    message=(
                        f"Cleanup complete; confirmed {result.terminated} owned root terminations. "
                        "Natural or unconfirmed root exits are not counted. Tree count unknown."
                    ),
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

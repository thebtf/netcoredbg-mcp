"""MCP Resources for debug state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import Server

    from .session import SessionManager


def register_resources(server: Server, session: SessionManager) -> None:
    """Register MCP resources."""

    @server.resource("debug://state")
    async def get_debug_state() -> str:
        """
        Current debug session state.
        Includes: state, currentThreadId, stopReason, threads, exitCode
        """
        return json.dumps(session.state.to_dict(), indent=2)

    @server.resource("debug://breakpoints")
    async def get_breakpoints() -> str:
        """
        All active breakpoints grouped by file.
        Each breakpoint includes: line, condition, hitCondition, verified, id
        """
        all_bps = session.breakpoints.get_all()
        result = {
            file: [
                {
                    "line": bp.line,
                    "condition": bp.condition,
                    "hitCondition": bp.hit_condition,
                    "verified": bp.verified,
                    "id": bp.id,
                }
                for bp in bps
            ]
            for file, bps in all_bps.items()
        }
        return json.dumps(result, indent=2)

    @server.resource("debug://output")
    async def get_output() -> str:
        """
        Debug console output from the debugged program.
        Contains stdout and stderr output.
        """
        return "".join(session.state.output_buffer)

    @server.resource("debug://threads")
    async def get_threads() -> str:
        """
        Current threads in the debugged process.
        """
        # Fetch fresh thread data from the debugger
        await session.get_threads()
        threads = [{"id": t.id, "name": t.name} for t in session.state.threads]
        return json.dumps(threads, indent=2)

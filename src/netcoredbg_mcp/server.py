"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .response import build_error_response
from .session import SessionManager
from .session.state import DebugState, StoppedSnapshot
from .utils.project import get_project_root
from .utils.source import read_source_context

logger = logging.getLogger(__name__)

# Global session manager (single client mode)
_session: SessionManager | None = None
_initial_project_path: str | None = None


def get_session() -> SessionManager:
    """Get or create session manager.

    Note: Single client mode - only one debug session supported at a time.
    """
    global _session
    if _session is None:
        netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
        _session = SessionManager(netcoredbg_path, _initial_project_path)
    return _session


async def resolve_project_root(ctx: Context, session: SessionManager) -> Path | None:
    """Resolve the current project root, potentially updating session.

    Uses MCP roots from client if available, otherwise falls back to
    configured project path.

    Args:
        ctx: MCP Context for accessing client roots
        session: Session manager to update if project root changes

    Returns:
        Current project root path
    """
    # Try to get project root from MCP context (includes client roots)
    project_root = await get_project_root(ctx)

    if project_root:
        # Update session's project path if it differs
        current = session.project_path
        new_path = str(project_root)
        if current != new_path:
            logger.info(f"Updating project root: {current} -> {new_path}")
            session.set_project_path(new_path)

    return project_root


def create_server(project_path: str | None = None) -> FastMCP:
    """Create and configure the MCP server.

    Args:
        project_path: Initial root path for the project being debugged.
            All file operations will be constrained to this path.
            Can be dynamically updated from MCP client roots.
    """
    global _initial_project_path
    _initial_project_path = project_path
    mcp = FastMCP("netcoredbg-mcp")
    session = get_session()

    # Helper to notify resource updates (MCP spec compliance)
    from pydantic import AnyUrl

    async def notify_state_changed(ctx: Context) -> None:
        """Notify client that debug://state resource has changed."""
        try:
            if ctx.session:
                await ctx.session.send_resource_updated(AnyUrl("debug://state"))
        except Exception as e:
            logger.warning(f"Failed to send resource update notification for debug://state: {e}")

    async def notify_breakpoints_changed(ctx: Context) -> None:
        """Notify client that debug://breakpoints resource has changed."""
        try:
            if ctx.session:
                await ctx.session.send_resource_updated(AnyUrl("debug://breakpoints"))
        except Exception as e:
            logger.warning(f"Failed to send resource update notification for debug://breakpoints: {e}")

    # ============== Mux Session Ownership ==============

    from .mux import SessionOwnership, get_mux_session_id

    _ownership = SessionOwnership()

    def _check_session_access(ctx: Context) -> str | None:
        """Check if the calling session has access to mutating operations.
        Returns error message if denied, None if allowed."""
        session_id = get_mux_session_id(ctx)
        return _ownership.check_access(session_id)

    # ============== Shared Helpers ==============

    def _build_stopped_response(
        snapshot: StoppedSnapshot,
        action_name: str,
    ) -> dict:
        """Build a rich response from a StoppedSnapshot for execution tools."""
        state_value = snapshot.state.value

        if snapshot.timed_out:
            next_actions = ["get_output", "pause_execution", "get_debug_state", "stop_debug"]
            message = (
                "Program is still running after timeout. "
                "Breakpoint may not have been reached."
            )
        elif state_value == "stopped":
            next_actions = [
                "get_call_stack", "get_variables", "evaluate_expression",
                "step_over", "step_into", "step_out", "continue_execution",
            ]
            reason = snapshot.stop_reason or "unknown"
            message = f"Program is PAUSED (reason: {reason}). Inspect state, then resume."
        elif state_value == "terminated":
            next_actions = ["get_output", "stop_debug"]
            exit_code = snapshot.exit_code
            message = f"Program terminated (exit code: {exit_code})."
        else:
            next_actions = ["get_debug_state", "stop_debug"]
            message = f"Unexpected state: {state_value}."

        result: dict = {
            "state": state_value,
            "reason": snapshot.stop_reason,
            "thread_id": snapshot.thread_id,
            "timed_out": snapshot.timed_out,
            "message": message,
            "next_actions": next_actions,
        }

        if snapshot.exit_code is not None:
            result["exit_code"] = snapshot.exit_code
        if snapshot.exception_info:
            result["exception_info"] = snapshot.exception_info

        return result

    async def _execute_and_wait(
        ctx: Context,
        action_coro,
        action_name: str,
        timeout: float = 30.0,
    ) -> dict:
        """Execute an action (continue/step), wait for stopped, return rich response."""
        try:
            session.prepare_for_execution()
            await action_coro
            snapshot = await session.wait_for_stopped(timeout=timeout)
            response = _build_stopped_response(snapshot, action_name)

            # Add source context if stopped at a known location
            if snapshot.state == DebugState.STOPPED and snapshot.thread_id:
                try:
                    frames = await session.get_stack_trace(snapshot.thread_id, 0, 1)
                    if frames:
                        response["location"] = {
                            "file": frames[0].source,
                            "line": frames[0].line,
                            "function": frames[0].name,
                            "source_context": read_source_context(
                                frames[0].source, frames[0].line,
                            ),
                        }
                except Exception:
                    logger.debug("Failed to get source context", exc_info=True)

            await notify_state_changed(ctx)
            return response
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Register Tool Modules ==============

    from .tools.debug import register_debug_tools
    from .tools.breakpoints import register_breakpoint_tools
    from .tools.inspection import register_inspection_tools
    from .tools.output import register_output_tools
    from .tools.ui import register_ui_tools
    from .tools.process import register_process_tools
    from .prompts import register_prompts

    register_debug_tools(
        mcp=mcp,
        session=session,
        ownership=_ownership,
        notify_state_changed=notify_state_changed,
        check_session_access=_check_session_access,
        execute_and_wait=_execute_and_wait,
        resolve_project_root=resolve_project_root,
    )

    register_breakpoint_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
        notify_breakpoints_changed=notify_breakpoints_changed,
        resolve_project_root=resolve_project_root,
    )

    register_inspection_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_output_tools(
        mcp=mcp,
        session=session,
    )

    register_ui_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_process_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_prompts(mcp)

    # ============== Resources ==============

    @mcp.resource("debug://state", mime_type="application/json")
    async def debug_state_resource() -> str:
        """Current debug session state (JSON).

        Contains: status, stop_reason, threads, process info.
        Updates when: session starts/stops, breakpoint hit, step completes.
        """
        return json.dumps(session.state.to_dict(), indent=2)

    @mcp.resource("debug://breakpoints", mime_type="application/json")
    async def debug_breakpoints_resource() -> str:
        """All active breakpoints (JSON).

        Contains: file paths with line numbers, conditions, verified status.
        Updates when: breakpoints added/removed/verified.
        """
        all_bps = session.breakpoints.get_all()
        result = {
            f: [{"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps]
            for f, bps in all_bps.items()
        }
        return json.dumps(result, indent=2)

    @mcp.resource("debug://output", mime_type="text/plain")
    async def debug_output_resource() -> str:
        """Debug console output (plain text).

        Contains: stdout/stderr from debugged process.
        Updates when: new output arrives.
        """
        return "".join(session.state.output_buffer)

    @mcp.resource("debug://threads", mime_type="application/json")
    async def debug_threads_resource() -> str:
        """Current threads in the debugged process (JSON).

        Contains: thread id and name for each active thread.
        Updates when: process stops (breakpoint, step, pause).
        """
        threads = await session.get_threads()
        return json.dumps([{"id": t.id, "name": t.name} for t in threads], indent=2)

    logger.info("NetCoreDbg MCP Server initialized")
    return mcp

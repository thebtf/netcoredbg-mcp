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
        import atexit

        netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
        _session = SessionManager(netcoredbg_path, _initial_project_path)
        # Register temp dir cleanup on server exit
        atexit.register(_session.temp_manager.cleanup_all)
        # GC stale temp dirs from previous crashed sessions
        _session.temp_manager.gc_stale()
    return _session


async def resolve_project_root(ctx: Context, session: SessionManager) -> Path | None:
    """Resolve the current project root, potentially updating session.

    Operator-pinned project scope (--project / env / config) is authoritative.
    Client MCP roots participate only when no operator pin is configured and
    never authorize UNC/network roots (see ``utils.project.get_project_root``).

    Args:
        ctx: MCP Context for accessing client roots
        session: Session manager to update if project root changes

    Returns:
        Current project root path
    """
    project_root = await get_project_root(ctx)

    if project_root:
        # Update session's project path if it differs
        current = session.project_path
        new_path = str(project_root)
        if current != new_path:
            logger.info(f"Updating project root: {current} -> {new_path}")
            session.set_project_path(new_path)

    return project_root


async def resolve_project_root_readonly(ctx: Context, _session: SessionManager) -> Path | None:
    """Resolve the current project root without mutating shared session scope."""
    return await get_project_root(ctx)


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

    # Resource subscription tracking + update notifications (FD-006, Engram #393). See
    # resource_updates.py for why subscribe/unsubscribe requires the low-level Server escape
    # hatch and why the negotiated capability needs a post-hoc correction in __main__.py.
    from .resource_updates import (
        BREAKPOINTS_URI,
        OUTPUT_URI,
        STATE_URI,
        THREADS_URI,
        ResourceSubscriptions,
        notify_resource_updated,
        notify_resources_updated,
        register_resource_subscription_handlers,
    )

    def _resource_update_token(uri: str) -> object:
        revision = session.resource_update_revision(uri)
        if uri == STATE_URI:
            return (revision, json.dumps(session.state.to_dict(), sort_keys=True))
        if uri == BREAKPOINTS_URI:
            return (
                revision,
                tuple(
                    (
                        file_path,
                        tuple(
                            (bp.line, bp.dap_line, bp.condition, bp.verified)
                            for bp in breakpoints
                        ),
                    )
                    for file_path, breakpoints in sorted(session.breakpoints.get_all().items())
                ),
            )
        if uri == OUTPUT_URI:
            return (
                revision,
                session.state.output_sequence,
                session.state.output_trimmed_before,
                len(session.state.output_buffer),
            )
        if uri == THREADS_URI:
            return (
                revision,
                session.state.state.value,
                session.state.current_thread_id,
                tuple((thread.id, thread.name) for thread in session.state.threads),
            )
        return revision

    _resource_subscriptions = ResourceSubscriptions(_resource_update_token)
    register_resource_subscription_handlers(mcp, _resource_subscriptions)

    async def _publish_dap_resource_updates(uris: tuple[str, ...]) -> None:
        await notify_resources_updated(uris, _resource_subscriptions)

    session.set_resource_update_callback(_publish_dap_resource_updates)

    async def notify_state_changed(_ctx: Context) -> None:
        """Notify subscribed clients that debug://state has changed."""
        await notify_resource_updated(STATE_URI, _resource_subscriptions)

    async def notify_breakpoints_changed(_ctx: Context) -> None:
        """Notify subscribed clients that debug://breakpoints has changed."""
        await notify_resource_updated(BREAKPOINTS_URI, _resource_subscriptions)

    async def notify_threads_changed(_ctx: Context) -> None:
        """Notify subscribed clients that debug://threads has changed."""
        await notify_resource_updated(THREADS_URI, _resource_subscriptions)

    async def notify_output_changed(_ctx: Context) -> None:
        """Notify subscribed clients that debug://output has changed.

        DAP output events publish immediately through SessionManager's asynchronous
        mutation callback. Tool-settle calls remain as a deterministic fallback and are
        de-duplicated by the resource token captured after the mutation.
        """
        await notify_resource_updated(OUTPUT_URI, _resource_subscriptions)

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
                "Program is still running after timeout. Breakpoint may not have been reached."
            )
        elif state_value == "stopped":
            next_actions = [
                "get_call_stack",
                "get_variables",
                "evaluate_expression",
                "step_over",
                "step_into",
                "step_out",
                "continue_execution",
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

        # Surface stopped event description/text (FR-6)
        result["description"] = snapshot.description or ""
        result["text"] = snapshot.text or ""

        return result

    async def _execute_and_wait(
        ctx: Context,
        action_coro,
        action_name: str,
        timeout: float = 30.0,
    ) -> dict:
        """Execute an action (continue/step), wait for stopped, return rich response."""
        try:
            # Phase 1: Report resuming
            try:
                await ctx.report_progress(progress=0, total=100, message=f"{action_name}...")
            except Exception:
                pass

            session.prepare_for_execution()
            await action_coro

            # Phase 2: Report waiting
            try:
                await ctx.report_progress(
                    progress=30,
                    total=100,
                    message="Waiting for stop event...",
                )
            except Exception:
                pass

            # Heartbeat callback — fires every ~5s while waiting
            async def heartbeat(elapsed: float) -> None:
                try:
                    await ctx.report_progress(
                        progress=30,
                        total=100,
                        message=f"Still waiting... ({elapsed:.0f}s)",
                    )
                except Exception:
                    pass

            snapshot = await session.wait_for_stopped(timeout=timeout, heartbeat_callback=heartbeat)

            # Phase 3: Report result
            try:
                if snapshot.timed_out:
                    msg = f"Timed out waiting ({timeout:.0f}s) — program still running"
                elif snapshot.state == DebugState.TERMINATED:
                    msg = f"Program terminated (exit code: {snapshot.exit_code})"
                else:
                    reason = snapshot.stop_reason or "unknown"
                    msg = f"Program stopped: {reason}"
                await ctx.report_progress(progress=100, total=100, message=msg)
            except Exception:
                pass

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
                                frames[0].source,
                                frames[0].line,
                            ),
                        }
                except Exception:
                    logger.debug("Failed to get source context", exc_info=True)

            await notify_state_changed(ctx)
            await notify_threads_changed(ctx)
            await notify_output_changed(ctx)
            return response
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Register Tool Modules ==============

    from .prompts import register_prompts
    from .tools.breakpoints import register_breakpoint_tools
    from .tools.code_search import register_code_search_tools
    from .tools.debug import register_debug_tools
    from .tools.enc import register_enc_tools
    from .tools.inspection import register_inspection_tools
    from .tools.memory import register_memory_tools
    from .tools.output import register_output_tools
    from .tools.process import register_process_tools
    from .tools.runtime_smoke import register_runtime_smoke_tools
    from .tools.ui import register_ui_tools
    from .tools.ui_evidence import register_ui_evidence_tools

    register_debug_tools(
        mcp=mcp,
        session=session,
        ownership=_ownership,
        notify_state_changed=notify_state_changed,
        notify_threads_changed=notify_threads_changed,
        notify_output_changed=notify_output_changed,
        check_session_access=_check_session_access,
        execute_and_wait=_execute_and_wait,
        resolve_project_root=resolve_project_root,
        resolve_project_root_readonly=resolve_project_root_readonly,
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

    register_memory_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_output_tools(
        mcp=mcp,
        session=session,
    )

    register_runtime_smoke_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
        resolve_project_root=resolve_project_root,
        resolve_project_root_readonly=resolve_project_root_readonly,
    )

    register_ui_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_ui_evidence_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_process_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
    )

    register_code_search_tools(
        mcp=mcp,
        session=session,
        resolve_project_root=resolve_project_root,
    )

    register_enc_tools(
        mcp=mcp,
        session=session,
        check_session_access=_check_session_access,
        notify_state_changed=notify_state_changed,
        resolve_project_root=resolve_project_root,
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
            f: [
                {
                    "line": bp.line,
                    "dap_line": bp.dap_line,
                    "condition": bp.condition,
                    "verified": bp.verified,
                }
                for bp in bps
            ]
            for f, bps in all_bps.items()
        }
        return json.dumps(result, indent=2)

    @mcp.resource("debug://output", mime_type="text/plain")
    async def debug_output_resource() -> str:
        """Debug console output (plain text).

        Contains: stdout/stderr from debugged process.
        Updates when: new output arrives.
        """
        return "".join(e.text for e in session.state.output_buffer)

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

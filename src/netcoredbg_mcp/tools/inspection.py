"""Variable inspection, evaluation, tracepoints, snapshots, and analysis tools."""

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine

from mcp.server.fastmcp import Context, FastMCP

from ..session import SessionManager

from ..response import build_error_response, build_response
from ..utils.source import read_source_context

logger = logging.getLogger(__name__)

_NULL_SENTINELS = frozenset(("null", "Nothing", "None", ""))


def compute_collection_stats(items: list, sample_size: int) -> dict[str, Any]:
    """Compute collection statistics from a list of Variable objects.

    Pure function — no session access. Extracted for testability.

    Args:
        items: List of Variable objects with .name, .value, .type attributes.
        sample_size: Number of first/last items to include. Must be > 0.

    Returns:
        Statistics dict with count, element_type, null_count, first_items,
        last_items, duplicate_count, and optional numeric stats.
    """
    count = len(items)
    element_type = items[0].type or "unknown" if items else "unknown"
    null_count = sum(1 for v in items if v.value in _NULL_SENTINELS)

    first_items = [
        {"name": v.name, "value": v.value, "type": v.type or ""}
        for v in items[:sample_size]
    ]
    last_items = [
        {"name": v.name, "value": v.value, "type": v.type or ""}
        for v in items[-sample_size:]
    ] if count > sample_size else []

    result: dict[str, Any] = {
        "count": count,
        "element_type": element_type,
        "null_count": null_count,
        "first_items": first_items,
        "last_items": last_items,
    }

    numeric_values: list[float] = []
    for v in items:
        try:
            numeric_values.append(float(v.value))
        except (ValueError, TypeError):
            pass

    if numeric_values:
        result["min"] = min(numeric_values)
        result["max"] = max(numeric_values)
        result["sum"] = sum(numeric_values)
        result["average"] = sum(numeric_values) / len(numeric_values)

    seen: set[str] = set()
    duplicates = 0
    for v in items:
        if v.value in seen:
            duplicates += 1
        seen.add(v.value)
    result["duplicate_count"] = duplicates

    return result


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

        State: STOPPED required. Returns frame_id values needed for get_scopes().

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
        """Get variable scopes for a stack frame.

        State: STOPPED required. Call get_call_stack() first to get frame_id. Returns variables_reference for get_variables().
        """
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
    async def get_variables(
        variables_reference: int,
        filter: str | None = None,
        start: int | None = None,
        count: int | None = None,
    ) -> dict:
        """Get variables for a scope or structured variable.

        State: STOPPED required. Call get_scopes() first to get variables_reference.

        Supports paging for large collections (e.g. arrays, lists).

        Args:
            variables_reference: Reference from get_scopes or a nested variable
            filter: Filter to "indexed" (array elements) or "named" (properties only)
            start: Index of first variable to fetch (for paging)
            count: Maximum number of variables to return (for paging)
        """
        try:
            variables = await session.get_variables(
                variables_reference, filter=filter, start=start, count=count
            )
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
        """Evaluate an expression in the current debug context.

        State: STOPPED required.
        """
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

        State: STOPPED required.

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
        """Get information about the current exception.

        State: STOPPED required (stopped on exception).
        """
        try:
            info = await session.get_exception_info(thread_id)
            return build_response(data=info, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_modules() -> dict:
        """List loaded assemblies/modules in the debug session.

        Returns module name, path, version, optimization status, and symbol loading state.
        Useful for diagnosing assembly loading failures and version conflicts.

        Note: Data comes from module load/unload events tracked during the session.
        """
        try:
            modules = [m.to_dict() for m in session.state.modules]
            return build_response(
                data={"modules": modules, "count": len(modules)},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False))
    async def quick_evaluate(expression: str, frame_id: int | None = None) -> dict:
        """Evaluate an expression while the program is running (atomic pause-eval-resume).

        Pauses execution for ~5ms, evaluates the expression, then resumes.
        Use this instead of manually pausing, evaluating, and continuing.

        IMPORTANT: Only works when program is RUNNING. If stopped, use evaluate_expression instead.

        Args:
            expression: Expression to evaluate (e.g., "myVariable", "list.Count")
            frame_id: Optional stack frame ID for evaluation context
        """
        try:
            result = await session.quick_evaluate(expression, frame_id)
            if "error" in result:
                return build_error_response(result["error"], state=session.state.state)
            return build_response(data=result, state=session.state.state)
        except RuntimeError as e:
            return build_error_response(str(e), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_exception_context(
        max_frames: int = 10,
        include_variables_for_frames: int = 1,
        max_inner_exceptions: int = 5,
    ) -> dict:
        """Get full exception context in one call (exception autopsy).

        State: STOPPED required (stopped on exception).

        Returns exception type/message, inner exception chain, stack frames with
        source locations, and local variables for the top N frames — all in a
        single response. Use this FIRST when the debugger stops on an exception.

        This replaces the manual sequence of:
        get_exception_info → get_call_stack → get_scopes → get_variables

        Args:
            max_frames: Maximum stack frames to return (default 10)
            include_variables_for_frames: Include locals for top N frames (default 1)
            max_inner_exceptions: Max inner exception chain depth (default 5)
        """
        try:
            result = await session.get_exception_context(
                max_frames=max_frames,
                include_variables_for_frames=include_variables_for_frames,
                max_inner_exceptions=max_inner_exceptions,
            )
            return build_response(data=result, state=session.state.state)
        except RuntimeError as e:
            return build_error_response(str(e), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_stop_context(
        include_variables: bool = True,
        include_output_tail: int = 10,
    ) -> dict:
        """Get rich context when stopped at any breakpoint — one call replaces many.

        Returns stop reason, stack trace with source, locals in the top frame,
        hit count for the current breakpoint, and recent output lines.

        Call this FIRST when execution stops. It gives you everything you need
        to understand the stop without multiple sequential tool calls.

        Args:
            include_variables: Include local variables for top frame (default True)
            include_output_tail: Include last N output lines (default 10, 0 to skip)
        """
        try:
            result = await session.get_stop_context(
                include_variables=include_variables,
                include_output_tail=include_output_tail,
            )
            return build_response(data=result, state=session.state.state)
        except RuntimeError as e:
            return build_error_response(str(e), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ── Tracepoint tools (FR-3) ──────────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def add_tracepoint(ctx: Context, file: str, line: int, expression: str) -> dict:
        """Set a non-stopping tracepoint that logs expression values.

        State: Works in any state. The tracepoint fires automatically on each hit.

        The tracepoint evaluates the expression each time the line is hit,
        without visibly pausing the program. Results are stored in a trace
        buffer accessible via get_trace_log.

        Args:
            file: Source file path
            line: Line number (1-based)
            expression: Expression to evaluate on each hit
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            from ..session.tracepoints import TracepointManager
            if not hasattr(session, '_tracepoint_manager'):
                session._tracepoint_manager = TracepointManager()

            mgr: TracepointManager = session._tracepoint_manager

            # Validate and normalize file path to prevent path traversal
            real_file = os.path.realpath(os.path.abspath(file))
            if not os.path.isabs(real_file):
                return build_error_response("File path must be absolute.", state=session.state.state)
            if not os.path.isfile(real_file):
                return build_error_response(
                    f"File not found: {real_file}", state=session.state.state,
                )
            norm_file = real_file

            # Register tracepoint
            tp = mgr.add(norm_file, line, expression)

            # Set a real DAP breakpoint on this line
            try:
                bp_result = await session.add_breakpoint(norm_file, line)
                if bp_result and isinstance(bp_result, dict):
                    tp.breakpoint_id = bp_result.get("id")
            except Exception as e:
                logger.warning("Failed to set DAP breakpoint for tracepoint %s: %s", tp.id, e)

            return build_response(
                data={
                    "id": tp.id,
                    "file": tp.file,
                    "line": tp.line,
                    "expression": tp.expression,
                    "breakpoint_verified": tp.breakpoint_id is not None,
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def remove_tracepoint(ctx: Context, tracepoint_id: str) -> dict:
        """Remove a tracepoint by ID.

        Args:
            tracepoint_id: Tracepoint ID (e.g., "tp-1")
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            mgr = getattr(session, '_tracepoint_manager', None)
            if mgr is None:
                return build_error_response("No tracepoints configured.", state=session.state.state)

            tp = mgr.remove(tracepoint_id)
            if tp is None:
                return build_error_response(
                    f"Tracepoint '{tracepoint_id}' not found.", state=session.state.state,
                )

            # Remove the underlying DAP breakpoint so it no longer stops execution
            try:
                await session.remove_breakpoint(tp.file, tp.line)
            except Exception as e:
                logger.warning("Failed to remove DAP breakpoint for tracepoint %s: %s", tp.id, e)

            return build_response(
                data={"removed": tracepoint_id, "was_active": tp.active, "hit_count": tp.hit_count},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_trace_log(
        since: float | None = None,
        tracepoint_id: str | None = None,
    ) -> dict:
        """Get tracepoint evaluation log.

        State: Works in any state.

        Args:
            since: Only return entries after this timestamp (monotonic)
            tracepoint_id: Filter to specific tracepoint
        """
        try:
            mgr = getattr(session, '_tracepoint_manager', None)
            if mgr is None:
                return build_response(
                    data={"entries": [], "total": 0, "truncated": False},
                    state=session.state.state,
                )

            entries = mgr.get_log(since=since, tracepoint_id=tracepoint_id)
            return build_response(
                data={
                    "entries": [
                        {
                            "timestamp": e.timestamp,
                            "file": e.file,
                            "line": e.line,
                            "expression": e.expression,
                            "value": e.value,
                            "thread_id": e.thread_id,
                            "tracepoint_id": e.tracepoint_id,
                        }
                        for e in entries
                    ],
                    "total": len(entries),
                    "truncated": mgr.is_log_full,
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def clear_trace_log(ctx: Context) -> dict:
        """Clear the tracepoint evaluation log."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            mgr = getattr(session, '_tracepoint_manager', None)
            count = mgr.clear_log() if mgr else 0
            return build_response(
                data={"cleared": count}, state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ── Snapshot tools (FR-4) ────────────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def create_snapshot(ctx: Context, name: str) -> dict:
        """Capture all local variables at the current frame as a named snapshot.

        State: STOPPED required.

        Must be called when the program is stopped at a breakpoint.
        Max 20 snapshots per session (oldest evicted when full).

        Args:
            name: Unique name for this snapshot
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            from ..session.snapshots import SnapshotManager
            if not hasattr(session, '_snapshot_manager'):
                session._snapshot_manager = SnapshotManager()

            snap = await session._snapshot_manager.create(name, session)
            return build_response(
                data={
                    "name": snap.name,
                    "frame": snap.frame_name,
                    "variable_count": len(snap.variables),
                    "timestamp": snap.timestamp,
                },
                state=session.state.state,
            )
        except (ValueError, RuntimeError) as e:
            return build_error_response(str(e), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def diff_snapshots(name1: str, name2: str) -> dict:
        """Compare two snapshots and show variable differences.

        Args:
            name1: First snapshot name (before state)
            name2: Second snapshot name (after state)
        """
        try:
            mgr = getattr(session, '_snapshot_manager', None)
            if mgr is None:
                return build_error_response("No snapshots created.", state=session.state.state)

            result = mgr.diff(name1, name2)
            return build_response(data=result, state=session.state.state)
        except KeyError as e:
            return build_error_response(str(e), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def list_snapshots() -> dict:
        """List all captured snapshots with metadata."""
        try:
            mgr = getattr(session, '_snapshot_manager', None)
            if mgr is None:
                return build_response(
                    data={"snapshots": []}, state=session.state.state,
                )
            snapshots = mgr.list_snapshots()
            return build_response(
                data={"snapshots": snapshots}, state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ── Collection analyzer (FR-5) ───────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def analyze_collection(
        ctx: Context,
        variables_reference: int,
        sample_size: int = 5,
    ) -> dict:
        """Analyze a collection variable in one call.

        State: STOPPED required. Get variables_reference from get_variables() response.

        Returns count, element type, null count, first/last N items,
        and numeric stats (min/max/sum/average) for numeric collections.

        Args:
            variables_reference: Variable reference from get_variables response
            sample_size: Number of first/last items to include (default 5)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if sample_size <= 0:
                return build_error_response(
                    "sample_size must be greater than 0.", state=session.state.state,
                )

            from ..session.state import DebugState
            if session.state.state != DebugState.STOPPED:
                return build_error_response(
                    "Program must be stopped to analyze collections.",
                    state=session.state.state,
                )

            items = await session.get_variables(variables_reference)
            if not items:
                return build_error_response(
                    "No items found. Variable may not be a collection or reference is expired.",
                    state=session.state.state,
                )

            result = compute_collection_stats(items, sample_size)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ── Object summarizer (FR-6) ─────────────────────────────────────

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def summarize_object(
        ctx: Context,
        variables_reference: int,
        max_depth: int = 2,
        max_properties: int = 50,
    ) -> dict:
        """Produce a flattened summary of a complex object.

        State: STOPPED required. Get variables_reference from get_variables() response.

        Returns property paths (dot notation), values, and types up to
        the configured depth. Detects circular references.

        Args:
            variables_reference: Variable reference from get_variables response
            max_depth: Maximum nesting depth (default 2, max 5)
            max_properties: Maximum total properties to return (default 50)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            from ..session.state import DebugState
            if session.state.state != DebugState.STOPPED:
                return build_error_response(
                    "Program must be stopped to summarize objects.",
                    state=session.state.state,
                )

            clamped_depth = max(1, min(max_depth, 5))
            properties: list[dict[str, str]] = []

            async def _walk(var_ref: int, prefix: str, depth: int, ancestors: frozenset[int]) -> None:
                if depth > clamped_depth or len(properties) >= max_properties:
                    return
                if var_ref in ancestors:
                    # True cycle: this ref is on the current call stack
                    properties.append({
                        "path": prefix or "<root>",
                        "value": "<circular ref>",
                        "type": "",
                    })
                    return

                current_ancestors = ancestors | {var_ref}
                vars_list = await session.get_variables(var_ref)

                for v in vars_list:
                    if len(properties) >= max_properties:
                        break

                    path = f"{prefix}.{v.name}" if prefix else v.name
                    properties.append({
                        "path": path,
                        "value": v.value,
                        "type": v.type or "",
                    })

                    # Recurse into nested objects
                    if v.variables_reference > 0 and depth < clamped_depth:
                        await _walk(v.variables_reference, path, depth + 1, current_ancestors)

            await _walk(variables_reference, "", 0, frozenset())

            total = len(properties)
            return build_response(
                data={
                    "properties": properties,
                    "total_properties": total,
                    "truncated": total >= max_properties,
                    "depth_reached": clamped_depth,
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .response import build_error_response, build_response
from .session import SessionManager
from .session.state import DebugState, StoppedSnapshot
from .utils.project import get_project_root
from .utils.app_type import detect_app_type
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

    # ============== Helpers ==============

    def _build_stopped_response(
        snapshot: StoppedSnapshot,
        action_name: str,
    ) -> dict:
        """Build a rich response from a StoppedSnapshot for execution tools.

        Includes state, reason, location with source context, and next_actions
        so the agent knows exactly what happened and what to do next.
        """
        state_value = snapshot.state.value

        # Determine next_actions based on resulting state
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
        """Execute an action (continue/step), wait for stopped, return rich response.

        This is the long-poll pattern: the tool blocks until the debugger fires
        a stopped/terminated/exited event (or timeout expires).
        """
        try:
            session._execution_event.clear()
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

    # ============== Debug Control Tools ==============

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def start_debug(
        ctx: Context,
        program: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stop_at_entry: bool = False,
        pre_build: bool = True,
        build_project: str | None = None,
        build_configuration: str = "Debug",
    ) -> dict:
        """
        Start debugging a .NET program. RECOMMENDED for most debugging scenarios.

        This is the preferred method for debugging .NET applications. It launches
        a new process under the debugger with full feature support including:
        - Complete call stack visibility
        - Full variable inspection
        - All breakpoint features

        SMART RESOLUTION: For .NET 6+ apps (WPF/WinForms), automatically resolves
        .exe to .dll to avoid "deps.json conflict" errors. You can pass either
        App.exe or App.dll - the correct target will be selected automatically.

        PRE-BUILD: By default, builds the project before launching to ensure you're
        debugging the latest code. Provide build_project path to .csproj file.
        Set pre_build=False to skip building (e.g., for pre-built binaries).

        BUILD WARNINGS: Hidden by default to reduce noise. If the build succeeds
        but the app behaves unexpectedly, call get_build_diagnostics() to see
        all warnings — they may reveal the issue.

        Use attach_debug only for already-running processes (e.g., ASP.NET services).

        Args:
            program: Path to the .NET executable or DLL to debug (auto-resolved)
            cwd: Working directory for the program
            args: Command line arguments
            env: Environment variables
            stop_at_entry: Stop at entry point
            pre_build: Build project before launching (default: True). Requires build_project.
            build_project: Path to .csproj file (required when pre_build=True)
            build_configuration: Build configuration (Debug/Release)
        """
        try:
            # Resolve project root from MCP context (may update session)
            await resolve_project_root(ctx, session)

            # Validate pre_build requires build_project
            if pre_build and not build_project:
                return build_error_response(
                    "pre_build=True requires build_project path to .csproj file. "
                    "Either provide build_project or set pre_build=False.",
                    state=session.state.state,
                )

            # Validate program path (security: prevent arbitrary execution)
            # If pre_build=True, don't require file to exist yet (build will create it)
            validated_program = session.validate_program(program, must_exist=not pre_build)

            # Validate cwd if provided (for pre_build, cwd may not exist yet either)
            validated_cwd = cwd
            if cwd:
                validated_cwd = session.validate_path(cwd, must_exist=not pre_build)

            # Validate build_project if provided (must exist for build to work)
            validated_build_project = None
            if build_project:
                validated_build_project = session.validate_path(build_project, must_exist=True)

            # Progress callback to report to MCP client
            async def report_progress(progress: float, total: float, message: str) -> None:
                await ctx.report_progress(progress=progress, total=total, message=message)

            result = await session.launch(
                program=validated_program,
                cwd=validated_cwd,
                args=args,
                env=env,
                stop_at_entry=stop_at_entry,
                pre_build=pre_build,
                build_project=validated_build_project,
                build_configuration=build_configuration,
                progress_callback=report_progress,
            )
            await notify_state_changed(ctx)

            # Detect application type for agent hints
            app_type = detect_app_type(validated_program)
            data = {**result, "app_type": app_type}

            message = None
            if app_type == "gui":
                message = (
                    "GUI application detected. Let the window fully load before "
                    "setting breakpoints."
                )

            return build_response(
                data=data,
                state=session.state.state,
                message=message,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def attach_debug(process_id: int) -> dict:
        """
        AVOID - Use start_debug instead. Attach to already-running process (LIMITED).

        LIMITATION: netcoredbg does NOT support justMyCode in attach mode (only in launch).
        This is an UPSTREAM limitation that CANNOT be fixed by this MCP server.
        Result: stack traces will be incomplete/empty, debugging will be unreliable.

        ONLY use this if you MUST debug an already-running process that you
        cannot restart (e.g., production service, container you cannot control).

        For normal debugging, ALWAYS use start_debug which has full functionality.
        If start_debug fails with build errors, fix the build - don't switch to attach.

        Args:
            process_id: PID of an already-running .NET process (NOT for normal debugging)
        """
        try:
            result = await session.attach(process_id)
            return build_response(
                data=result,
                state=session.state.state,
                warning=(
                    "ATTACH MODE HAS LIMITED FUNCTIONALITY. "
                    "Stack traces may be incomplete due to netcoredbg limitation. "
                    "For reliable debugging, use start_debug instead."
                ),
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def stop_debug(ctx: Context) -> dict:
        """Stop the current debug session."""
        try:
            result = await session.stop()
            await notify_state_changed(ctx)
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def restart_debug(ctx: Context, rebuild: bool = True) -> dict:
        """Restart the current debug session with the same configuration.

        Stops the current session, optionally rebuilds, and relaunches.
        Use this after code changes to debug the updated version.

        Args:
            rebuild: Whether to rebuild before restarting (default: True)
        """
        try:
            result = await session.restart(rebuild=rebuild)
            await notify_state_changed(ctx)

            # Detect app type for hints
            program = result.get("program", "")
            app_type = detect_app_type(program) if program else None

            message = "Debug session restarted."
            if app_type == "gui":
                message += " GUI app detected — wait for window before setting breakpoints."

            return build_response(
                data={**result, "app_type": app_type},
                state=session.state.state,
                message=message,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def continue_execution(ctx: Context, thread_id: int | None = None) -> dict:
        """Continue program execution. Blocks until the program stops again or timeout.

        This tool uses the long-poll pattern: it waits for the debugger to report
        a stopped event (breakpoint hit, exception, step complete) before returning.

        The response includes the new state, stop reason, and next_actions so you
        know exactly what happened and what to do next.

        IMPORTANT: While waiting, the program is RUNNING — do not call
        get_variables or get_call_stack until this tool returns with state=stopped.
        """
        return await _execute_and_wait(
            ctx, session.continue_execution(thread_id), "continue_execution"
        )

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def pause_execution(ctx: Context, thread_id: int | None = None) -> dict:
        """Pause program execution.

        Unlike continue/step tools, this returns immediately after sending
        the pause command — it does not wait for a stopped event.
        """
        try:
            result = await session.pause(thread_id)
            await notify_state_changed(ctx)
            return build_response(
                data=result,
                state=session.state.state,
                next_actions=[
                    "get_call_stack", "get_variables", "evaluate_expression",
                    "step_over", "step_into", "step_out", "continue_execution",
                ],
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def step_over(ctx: Context, thread_id: int | None = None) -> dict:
        """Step over to the next line. Blocks until the step completes.

        Executes the current line without entering function calls.
        Returns the new stopped location with source context.

        IMPORTANT: After this returns with state=stopped, inspect variables
        at the new location before deciding the next action.
        """
        return await _execute_and_wait(
            ctx, session.step_over(thread_id), "step_over"
        )

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def step_into(ctx: Context, thread_id: int | None = None) -> dict:
        """Step into the next function call. Blocks until the step completes.

        Enters the function being called on the current line.
        Use this when you need to investigate what happens inside a called function.

        IMPORTANT: After this returns with state=stopped, you are inside the
        called function. Use step_out to return to the caller.
        """
        return await _execute_and_wait(
            ctx, session.step_in(thread_id), "step_into"
        )

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def step_out(ctx: Context, thread_id: int | None = None) -> dict:
        """Step out of the current function. Blocks until the step completes.

        Continues execution until the current function returns, then stops
        at the caller. Use this to exit a function you stepped into.
        """
        return await _execute_and_wait(
            ctx, session.step_out(thread_id), "step_out"
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_debug_state() -> dict:
        """
        Get the current debug session state.

        Returns state, threads, current position, and exception info.
        The user cannot see this directly - summarize important info for them.

        IMPORTANT: Always check state before asking user to interact with the app GUI!
        If the app is paused at a breakpoint, the user cannot interact with UI.
        Call continue_execution first if state shows stopped/paused.
        """
        try:
            return build_response(
                data=session.state.to_dict(),
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Breakpoint Tools ==============

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

        Args:
            file: Absolute path to source file
            line: Line number (1-based)
            condition: Optional condition expression
            hit_condition: Optional hit count condition
        """
        try:
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
        """Remove a breakpoint from a specific line."""
        try:
            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file)
            removed = await session.remove_breakpoint(validated_file, line)
            await notify_breakpoints_changed(ctx)
            return build_response(
                data={"removed": removed},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def list_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """List all breakpoints or breakpoints in a specific file."""
        try:
            if file:
                # Resolve project root from MCP context
                await resolve_project_root(ctx, session)
                # Validate file path if provided
                validated_file = session.validate_path(file)
                bps = session.breakpoints.get_for_file(validated_file)
                result = {
                    validated_file: [
                        {"line": bp.line, "condition": bp.condition, "verified": bp.verified}
                        for bp in bps
                    ]
                }
            else:
                all_bps = session.breakpoints.get_all()
                result = {
                    f: [
                        {"line": bp.line, "condition": bp.condition, "verified": bp.verified}
                        for bp in bps
                    ]
                    for f, bps in all_bps.items()
                }
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def clear_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """Clear breakpoints from a file or all files."""
        try:
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

        Args:
            function_name: Full or partial function name to break on
            condition: Optional condition expression
            hit_condition: Optional hit count condition
        """
        try:
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
        filters: list[str] | None = None,
    ) -> dict:
        """Configure which exceptions should pause the debugger.

        Controls exception breakpoints — when the debugger should stop on exceptions.
        By default, no exception filters are set (exceptions don't pause unless uncaught).

        Common filters supported by netcoredbg:
        - "all": Break on all exceptions (caught and uncaught)
        - "user-unhandled": Break on exceptions not handled in user code

        Pass an empty list to disable all exception breakpoints.

        Args:
            filters: List of exception filter names. Pass [] to disable.
        """
        try:
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

    # ============== Inspection Tools ==============

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

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def get_output(clear: bool = False) -> dict:
        """Get stdout/stderr output from the debugged program.

        IMPORTANT: The user cannot see this output directly.
        YOU must read it and summarize relevant information for the user.
        Never tell the user to "check the console" or "look at output".

        Call periodically during debugging to catch log messages and errors.
        """
        try:
            output = "".join(session.state.output_buffer)
            if clear:
                session.state.output_buffer.clear()
            return build_response(
                data={"output": output},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def search_output(pattern: str, context_lines: int = 2) -> dict:
        """Search program output for a pattern (regex supported).

        Use this instead of get_output when looking for specific messages,
        errors, or log entries in large output. Returns matching lines with context.

        Args:
            pattern: Regex pattern to search for (case-insensitive)
            context_lines: Number of lines before/after each match (default 2)

        Returns:
            List of matches with line numbers and context
        """
        import re

        try:
            output = "".join(session.state.output_buffer)
            lines = output.splitlines()
            matches = []

            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return {"success": False, "error": f"Invalid regex: {e}"}

            for i, line in enumerate(lines):
                if regex.search(line):
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    context = lines[start:end]
                    matches.append({
                        "line_number": i + 1,
                        "match": line,
                        "context": context,
                    })

            return build_response(
                data={
                    "pattern": pattern,
                    "match_count": len(matches),
                    "matches": matches[:50],
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_output_tail(lines: int = 50) -> dict:
        """Get the last N lines of program output.

        Useful for checking recent output without loading everything.
        The user cannot see this - summarize relevant info for them.

        Args:
            lines: Number of lines to return (default 50)
        """
        try:
            output = "".join(session.state.output_buffer)
            all_lines = output.splitlines()
            tail = all_lines[-lines:]
            return build_response(
                data={
                    "total_lines": len(all_lines),
                    "returned_lines": len(tail),
                    "output": "\n".join(tail),
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Build Diagnostics Tools ==============

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def get_build_diagnostics(include_warnings: bool = True) -> dict:
        """Get full build diagnostics including all warnings.

        Build warnings are hidden by default in start_debug/restart_debug responses
        to reduce context noise. Call this tool when:
        - Build succeeds but the app crashes or behaves unexpectedly
        - Investigating assembly loading or compatibility issues
        - Checking nullable reference, deprecation, or platform warnings
        - Debugging "it compiles but doesn't work" situations

        Args:
            include_warnings: Include warning details (default True, the point of this tool)
        """
        try:
            build_result = session.last_build_result
            if build_result is None:
                return build_response(
                    data={"message": "No build has been performed yet."},
                    state=session.state.state,
                )

            return build_response(
                data=build_result.to_dict(include_warnings=include_warnings),
                state=session.state.state,
                message=(
                    f"Build: {build_result.error_count} errors, "
                    f"{build_result.warning_count} warnings"
                ),
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Process Management Tools ==============

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def cleanup_processes(force: bool = False) -> dict:
        """View or terminate tracked debug processes.

        Without force: shows all tracked processes and their status (alive/dead).
        With force=True: terminates all tracked processes (netcoredbg + debuggees).

        Use this instead of manual taskkill. The server tracks which processes
        it spawned — no risk of killing unrelated processes.

        Args:
            force: If True, terminate all tracked processes. If False, just show status.
        """
        try:
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

    # ============== UI Automation Tools ==============

    # Global UI automation instance
    from typing import Any

    _ui: Any = None

    def _get_ui() -> Any:
        """Get or create UI automation instance."""
        nonlocal _ui
        if _ui is None:
            from .ui import UIAutomation

            _ui = UIAutomation()
        return _ui

    async def _ensure_ui_connected(session: SessionManager) -> Any:
        """Ensure UI automation is connected to the debug process.

        Raises:
            NoActiveSessionError: If no debug session is active
            NoProcessIdError: If process ID not available
        """
        from .ui import NoActiveSessionError, NoProcessIdError

        if session.state.state == DebugState.IDLE:
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        ui = _get_ui()
        if ui.process_id != process_id:
            await ui.connect(process_id)
        return ui

    async def _find_ui_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ):
        """Helper to connect to UI and find an element."""
        ui = await _ensure_ui_connected(session)
        element = await ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
        )
        return ui, element

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_get_window_tree(max_depth: int = 3, max_children: int = 50) -> dict:
        """
        Get the visual tree of the debugged application's main window.

        Use this to understand the UI structure before interacting with elements.
        Call after start_debug and wait for the application window to appear.

        Args:
            max_depth: Maximum depth to traverse (default 3)
            max_children: Maximum children per element (default 50)

        Returns:
            Visual tree with automationId, controlType, name, isEnabled, etc.
        """
        try:
            ui = await _ensure_ui_connected(session)
            tree = await ui.get_window_tree(max_depth, max_children)
            return build_response(data=tree.to_dict(), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_find_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Find a UI element by AutomationId, name, or control type.

        At least one search criterion must be provided.
        Use ui_get_window_tree first to discover available elements.

        Args:
            automation_id: AutomationId property (most reliable for WPF)
            name: Element's Name/Title property
            control_type: Type like "Button", "TextBox", "MenuItem"

        Returns:
            Element info if found
        """
        try:
            ui = await _ensure_ui_connected(session)
            element = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
            )
            info = await ui.get_element_info(element)
            return build_response(data=info.to_dict(), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def ui_set_focus(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Set keyboard focus to a UI element.

        Call this before ui_send_keys to ensure keys go to the right element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.set_focus(element)
            return build_response(data={"focused": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys(
        keys: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Send keyboard input to a UI element.

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}"
        - Tab: "{TAB}"
        - Escape: "{ESC}"
        - Ctrl+C: "^c"
        - Alt+F4: "%{F4}"
        - Shift+Tab: "+{TAB}"
        - Ctrl+Shift+S: "^+s"

        Args:
            keys: Keys to send (pywinauto syntax)
            automation_id: Target element's AutomationId
            name: Target element's Name
            control_type: Target element's control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.send_keys(element, keys)
            return build_response(data={"sent": keys}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys_focused(keys: str) -> dict:
        """
        Send keyboard input to the currently focused element.

        Use this AFTER ui_set_focus to avoid re-searching for complex elements
        like DataGrid that may timeout on repeated searches.

        Workflow:
        1. ui_set_focus(automation_id="MyElement")  # Focus the element
        2. ui_send_keys_focused(keys="^{END}")      # Send keys without re-search

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}", Tab: "{TAB}", Escape: "{ESC}"
        - Ctrl+C: "^c", Alt+F4: "%{F4}", Shift+Tab: "+{TAB}"
        - Arrow keys: "{LEFT}", "{RIGHT}", "{UP}", "{DOWN}"
        - Ctrl+End: "^{END}", Ctrl+Home: "^{HOME}"

        Args:
            keys: Keys to send (pywinauto syntax)
        """
        try:
            ui = await _ensure_ui_connected(session)
            await ui.send_keys_focused(keys)
            return build_response(
                data={"sent": keys, "target": "focused"}, state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Click on a UI element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.click(element)
            return build_response(data={"clicked": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # ============== Prompts (slash commands) ==============

    @mcp.prompt(
        name="debug",
        description="Debug session workflow guide for .NET applications",
    )
    def debug_prompt() -> list[dict]:
        """Start here when debugging .NET applications."""
        return [
            {
                "role": "user",
                "content": """# .NET Debug Session Guide

## CRITICAL RULES FOR AI DEBUGGER

### State Awareness
1. **PAUSED = FROZEN:** When state=stopped, the target program is COMPLETELY FROZEN.
   Its UI won't paint, it won't respond to input, it won't produce output.
   Do NOT wait for the program to "respond" or "finish loading" - it can't.
   Inspect state (get_call_stack, get_variables), then RESUME execution.

2. **RUNNING = REFS INVALID:** When state=running, variable references from the
   previous stop are INVALID. Do NOT call get_variables with old references.
   Wait for the program to stop again (execution tools block automatically).

3. **TERMINATED = DONE:** When state=terminated, the session is over.
   Read output for errors, then call stop_debug.

### Breakpoint Timing
4. **GUI APPS (WPF/WinForms/Avalonia):** NEVER set breakpoints before the window
   is visible. The app will freeze during initialization and the window will
   never appear.

   Correct workflow:
   ```
   start_debug(program="App.dll", pre_build=True, build_project="App.csproj")
   # If state is stopped/entry: continue_execution()
   # Wait for window: ui_get_window_tree()
   # NOW set breakpoints: add_breakpoint(file="ViewModel.cs", line=42)
   # Trigger the action in the UI: ui_click(automation_id="btnSave")
   # Execution tools block until breakpoint hit - inspect state
   ```

5. **CONSOLE APPS:** Breakpoints before launch are fine - they fire on startup code.

### Inspect-Resume Cycle
6. After hitting a breakpoint, ALWAYS follow this sequence:
   a. get_call_stack() - understand where execution stopped
   b. get_scopes(frame_id) - get variable scope references
   c. get_variables(variables_reference) - read local variable values
   d. Decide: step deeper, continue to next breakpoint, or stop?
   e. RESUME - do not leave the app paused indefinitely.

### Output Monitoring
7. Call get_output_tail() after every significant execution phase to catch
   runtime errors, assertion failures, and log messages.
   The user CANNOT see program output - YOU must read and summarize it.
   Never tell the user to "check the console" or "look at output".

### Exception Handling
8. When stopped with reason=exception:
   - Call get_exception_info() BEFORE resuming
   - Read the exception type, message, and stack trace
   - Decide: is this expected (resume) or a bug (investigate deeper)?

### Step Strategy
9. Use step_over for general flow (most common).
   Use step_into when you need to enter a called function.
   Use step_out to exit the current function and return to the caller.
   Prefer step_over unless the bug is inside a function at the current line.

### Function Breakpoints
10. When you know the method name but not the line number:
    Use add_function_breakpoint(function_name="OnButtonClick").
    This breaks when the named function is entered.

### Valid Actions by State

| State | Valid Actions |
|-------|-------------|
| IDLE | start_debug, attach_debug |
| RUNNING | pause_execution, get_output*, get_debug_state, stop_debug, add_breakpoint |
| STOPPED | get_call_stack, get_variables, get_scopes, evaluate_expression, step_*, continue_execution, add/remove_breakpoint, set_variable, stop_debug, ui_* |
| TERMINATED | get_output, stop_debug, start_debug (new session) |

*get_output, get_output_tail, search_output are valid in all non-IDLE states.

## Quick Start Workflows

### Debug a Console App
```
start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj")
add_breakpoint(file="Program.cs", line=15)
continue_execution()  # blocks until breakpoint hit
get_call_stack()
get_variables(scope_reference)
continue_execution()  # resume
stop_debug()
```

### Debug a WPF/Avalonia App
```
start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj")
# App is running - wait for window
ui_get_window_tree()  # verify window is visible
add_breakpoint(file="MainViewModel.cs", line=42)
ui_click(automation_id="btnAction")  # trigger the code path
# Execution stops at breakpoint - inspect
get_call_stack()
get_variables(scope_reference)
continue_execution()  # resume the app
stop_debug()
```

### Investigate an Exception
```
configure_exceptions(filters=["all"])  # break on ALL exceptions
continue_execution()  # run until exception
# Stopped with reason=exception
get_exception_info()
get_call_stack()
get_variables(scope_reference)  # inspect state at exception point
continue_execution()  # or stop_debug() if done
```

### Sending Keys to Complex Controls (DataGrid, TreeView, etc.)
```
ui_set_focus(automation_id="MyDataGrid")  # 1. Focus the element
ui_send_keys_focused(keys="^{END}")       # 2. Send keys without re-search
ui_send_keys_focused(keys="{DOWN}")       # 3. Continue sending keys
```

## Build Warnings
11. Build warnings are HIDDEN by default in start_debug/restart_debug responses.
    If debugging leads nowhere and the app behaves unexpectedly, call
    get_build_diagnostics() - a warning about nullable references, missing assemblies,
    or compatibility issues may explain the behavior.

## Process Management
12. Use cleanup_processes() to view or terminate tracked debug processes.
    Never use manual taskkill - the server tracks what it spawned.

## Common Issues
- Empty stack trace? Use start_debug, not attach_debug
- deps.json conflict? Build uses .dll, not .exe
- E_NOINTERFACE? dbgshim.dll version mismatch with .NET runtime
- App frozen at startup? Breakpoints set too early for GUI apps (see rule 4)
- Build OK but app crashes? Check get_build_diagnostics() for warnings
""",
            }
        ]

    @mcp.prompt(
        name="exception",
        description="Guide for investigating exceptions during debugging",
    )
    def exception_prompt() -> list[dict]:
        """Steps to investigate an exception."""
        return [
            {
                "role": "user",
                "content": "The debugger stopped on an exception.",
            },
            {
                "role": "assistant",
                "content": "I'll investigate the exception. Let me gather the details.",
            },
            {
                "role": "user",
                "content": """## Exception Investigation Steps

Execute these in order:

### 1. Get Exception Details
```
get_debug_state()  # Check exceptionInfo field
```

### 2. Get Stack Trace
```
get_call_stack()   # See where exception occurred
```

### 3. Inspect Local State
```
get_scopes(frame_id)           # Get scopes for the frame
get_variables(scope_reference)  # See local variables
```

### 4. Read Recent Output
```
get_output()  # Check for error messages before exception
```

### 5. Report to User
Summarize:
- Exception type and message
- Where it occurred (file, line, method)
- Likely cause based on local state
- Suggested fix

### 6. Decision
- `continue_execution()` to ignore and continue
- `stop_debug()` to end session and fix code
""",
            },
        ]

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

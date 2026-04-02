"""Debug session control tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context, FastMCP
    from ..session import SessionManager
    from ..mux import SessionOwnership

from ..response import build_error_response, build_response
from ..utils.app_type import detect_app_type

logger = logging.getLogger(__name__)


def register_debug_tools(
    mcp: FastMCP,
    session: SessionManager,
    ownership: SessionOwnership,
    notify_state_changed: Callable[[Any], Coroutine],
    check_session_access: Callable[[Any], str | None],
    execute_and_wait: Callable[..., Coroutine],
    resolve_project_root: Callable[..., Coroutine],
) -> None:
    """Register debug session control tools on the MCP server."""
    from mcp.types import ToolAnnotations

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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

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
    async def attach_debug(ctx: Context, process_id: int) -> dict:
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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            result = await session.stop()
            ownership.release()
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
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

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
        access_error = check_session_access(ctx)
        if access_error:
            return build_error_response(access_error, state=session.state.state)

        return await execute_and_wait(
            ctx, session.continue_execution(thread_id), "continue_execution"
        )

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def pause_execution(ctx: Context, thread_id: int | None = None) -> dict:
        """Pause program execution.

        Unlike continue/step tools, this returns immediately after sending
        the pause command — it does not wait for a stopped event.
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

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
        access_error = check_session_access(ctx)
        if access_error:
            return build_error_response(access_error, state=session.state.state)

        return await execute_and_wait(
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
        access_error = check_session_access(ctx)
        if access_error:
            return build_error_response(access_error, state=session.state.state)

        return await execute_and_wait(
            ctx, session.step_in(thread_id), "step_into"
        )

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def step_out(ctx: Context, thread_id: int | None = None) -> dict:
        """Step out of the current function. Blocks until the step completes.

        Continues execution until the current function returns, then stops
        at the caller. Use this to exit a function you stepped into.
        """
        access_error = check_session_access(ctx)
        if access_error:
            return build_error_response(access_error, state=session.state.state)

        return await execute_and_wait(
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

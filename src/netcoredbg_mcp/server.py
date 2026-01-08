"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .session import SessionManager
from .utils.project import get_project_root

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

    # ============== Debug Control Tools ==============

    @mcp.tool()
    async def start_debug(
        ctx: Context,
        program: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stop_at_entry: bool = False,
        pre_build: bool = False,
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

        Use attach_debug only for already-running processes (e.g., ASP.NET services).

        Args:
            program: Path to the .NET executable or DLL to debug (auto-resolved)
            cwd: Working directory for the program
            args: Command line arguments
            env: Environment variables
            stop_at_entry: Stop at entry point
            pre_build: Build the project before launching (fixes stale binary issues)
            build_project: Path to .csproj file (required if pre_build=True)
            build_configuration: Build configuration (Debug/Release)
        """
        try:
            # Resolve project root from MCP context (may update session)
            await resolve_project_root(ctx, session)

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

            result = await session.launch(
                program=validated_program,
                cwd=validated_cwd,
                args=args,
                env=env,
                stop_at_entry=stop_at_entry,
                pre_build=pre_build,
                build_project=validated_build_project,
                build_configuration=build_configuration,
            )
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
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
            return {
                "success": True,
                "data": result,
                "warning": (
                    "ATTACH MODE HAS LIMITED FUNCTIONALITY. "
                    "Stack traces may be incomplete due to netcoredbg limitation. "
                    "For reliable debugging, use start_debug instead."
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def stop_debug() -> dict:
        """Stop the current debug session."""
        try:
            result = await session.stop()
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def continue_execution(thread_id: int | None = None) -> dict:
        """Continue program execution."""
        try:
            result = await session.continue_execution(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def pause_execution(thread_id: int | None = None) -> dict:
        """Pause program execution."""
        try:
            result = await session.pause(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_over(thread_id: int | None = None) -> dict:
        """Step over to the next line."""
        try:
            result = await session.step_over(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_into(thread_id: int | None = None) -> dict:
        """Step into the next function call."""
        try:
            result = await session.step_in(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_out(thread_id: int | None = None) -> dict:
        """Step out of the current function."""
        try:
            result = await session.step_out(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_debug_state() -> dict:
        """Get the current debug session state."""
        try:
            return {"success": True, "data": session.state.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Breakpoint Tools ==============

    @mcp.tool()
    async def add_breakpoint(
        ctx: Context,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> dict:
        """
        Add a breakpoint at a specific line.

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
            return {
                "success": True,
                "data": {
                    "file": bp.file,
                    "line": bp.line,
                    "condition": bp.condition,
                    "verified": bp.verified,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def remove_breakpoint(ctx: Context, file: str, line: int) -> dict:
        """Remove a breakpoint from a specific line."""
        try:
            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file)
            removed = await session.remove_breakpoint(validated_file, line)
            return {"success": True, "data": {"removed": removed}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
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
                        {"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps
                    ]
                }
            else:
                all_bps = session.breakpoints.get_all()
                result = {
                    f: [{"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps]
                    for f, bps in all_bps.items()
                }
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
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
            return {"success": True, "data": {"removed": count}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Inspection Tools ==============

    @mcp.tool()
    async def get_threads() -> dict:
        """Get all threads in the debugged process."""
        try:
            threads = await session.get_threads()
            return {"success": True, "data": [{"id": t.id, "name": t.name} for t in threads]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
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
            return {
                "success": True,
                "data": [
                    {"id": f.id, "name": f.name, "source": f.source, "line": f.line, "column": f.column}
                    for f in frames
                ],
            }
        except Exception as e:
            error_msg = str(e)
            # Enhanced error message for E_NOINTERFACE
            if "0x80004002" in error_msg or "E_NOINTERFACE" in error_msg.upper():
                logger.warning(
                    "[DIAGNOSTIC] E_NOINTERFACE on ICorDebugThread3. "
                    "Try setting NETCOREDBG_STACKTRACE_DELAY_MS=300 to test timing hypothesis."
                )
            return {"success": False, "error": error_msg}

    @mcp.tool()
    async def get_scopes(frame_id: int | None = None) -> dict:
        """Get variable scopes for a stack frame."""
        try:
            scopes = await session.get_scopes(frame_id)
            return {
                "success": True,
                "data": [
                    {"name": s.get("name", ""), "variablesReference": s.get("variablesReference", 0)}
                    for s in scopes
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_variables(variables_reference: int) -> dict:
        """Get variables for a scope or structured variable."""
        try:
            variables = await session.get_variables(variables_reference)
            return {
                "success": True,
                "data": [
                    {
                        "name": v.name,
                        "value": v.value,
                        "type": v.type,
                        "variablesReference": v.variables_reference,
                    }
                    for v in variables
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def evaluate_expression(expression: str, frame_id: int | None = None) -> dict:
        """Evaluate an expression in the current debug context."""
        try:
            result = await session.evaluate(expression, frame_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_exception_info(thread_id: int | None = None) -> dict:
        """Get information about the current exception."""
        try:
            info = await session.get_exception_info(thread_id)
            return {"success": True, "data": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_output(clear: bool = False) -> dict:
        """Get debug output from the debugged program."""
        try:
            output = "".join(session.state.output_buffer)
            if clear:
                session.state.output_buffer.clear()
            return {"success": True, "data": {"output": output}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Resources ==============

    @mcp.resource("debug://state")
    async def debug_state_resource() -> str:
        """Current debug session state."""
        return json.dumps(session.state.to_dict(), indent=2)

    @mcp.resource("debug://breakpoints")
    async def debug_breakpoints_resource() -> str:
        """All active breakpoints."""
        all_bps = session.breakpoints.get_all()
        result = {
            f: [{"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps]
            for f, bps in all_bps.items()
        }
        return json.dumps(result, indent=2)

    @mcp.resource("debug://output")
    async def debug_output_resource() -> str:
        """Debug console output."""
        return "".join(session.state.output_buffer)

    logger.info("NetCoreDbg MCP Server initialized")
    return mcp

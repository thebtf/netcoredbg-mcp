"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

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
    ) -> dict:
        """
        Start debugging a .NET program.

        Args:
            program: Path to the .NET executable or DLL to debug
            cwd: Working directory for the program
            args: Command line arguments
            env: Environment variables
            stop_at_entry: Stop at entry point
        """
        try:
            # Resolve project root from MCP context (may update session)
            await resolve_project_root(ctx, session)

            # Validate program path (security: prevent arbitrary execution)
            validated_program = session.validate_program(program)

            # Validate cwd if provided
            validated_cwd = cwd
            if cwd:
                validated_cwd = session.validate_path(cwd, must_exist=True)

            result = await session.launch(
                program=validated_program,
                cwd=validated_cwd,
                args=args,
                env=env,
                stop_at_entry=stop_at_entry,
            )
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def attach_debug(process_id: int) -> dict:
        """Attach to a running .NET process by PID."""
        try:
            result = await session.attach(process_id)
            return {"success": True, "data": result}
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
        """Get the call stack for a thread."""
        try:
            frames = await session.get_stack_trace(thread_id, 0, levels)
            return {
                "success": True,
                "data": [
                    {"id": f.id, "name": f.name, "source": f.source, "line": f.line, "column": f.column}
                    for f in frames
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

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

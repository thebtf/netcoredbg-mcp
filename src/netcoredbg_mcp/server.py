"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP

from .session import SessionManager

logger = logging.getLogger(__name__)

# Global session manager
_session: SessionManager | None = None


def get_session() -> SessionManager:
    """Get or create session manager."""
    global _session
    if _session is None:
        netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
        _session = SessionManager(netcoredbg_path)
    return _session


def create_server() -> FastMCP:
    """Create and configure the MCP server."""
    mcp = FastMCP("netcoredbg-mcp")
    session = get_session()

    # ============== Debug Control Tools ==============

    @mcp.tool()
    async def start_debug(
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
            result = await session.launch(
                program=program, cwd=cwd, args=args, env=env, stop_at_entry=stop_at_entry
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
        return {"success": True, "data": session.state.to_dict()}

    # ============== Breakpoint Tools ==============

    @mcp.tool()
    async def add_breakpoint(
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
            bp = await session.add_breakpoint(file, line, condition, hit_condition)
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
    async def remove_breakpoint(file: str, line: int) -> dict:
        """Remove a breakpoint from a specific line."""
        try:
            removed = await session.remove_breakpoint(file, line)
            return {"success": True, "data": {"removed": removed}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def list_breakpoints(file: str | None = None) -> dict:
        """List all breakpoints or breakpoints in a specific file."""
        try:
            if file:
                bps = session.breakpoints.get_for_file(file)
                result = {
                    file: [{"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps]
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
    async def clear_breakpoints(file: str | None = None) -> dict:
        """Clear breakpoints from a file or all files."""
        try:
            count = await session.clear_breakpoints(file)
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
        import json
        return json.dumps(session.state.to_dict(), indent=2)

    @mcp.resource("debug://breakpoints")
    async def debug_breakpoints_resource() -> str:
        """All active breakpoints."""
        import json
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

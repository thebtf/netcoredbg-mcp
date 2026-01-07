"""State inspection MCP tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import Server
    from ..session import SessionManager


def register_inspection_tools(server: "Server", session: "SessionManager") -> None:
    """Register inspection tools with MCP server."""

    @server.tool()
    async def get_threads() -> dict:
        """
        Get all threads in the debugged process.

        Returns:
            List of threads with id and name
        """
        try:
            threads = await session.get_threads()
            return {
                "success": True,
                "data": [{"id": t.id, "name": t.name} for t in threads],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_call_stack(
        thread_id: int | None = None,
        start_frame: int = 0,
        levels: int = 20,
    ) -> dict:
        """
        Get the call stack for a thread.

        Args:
            thread_id: Optional thread ID (defaults to current stopped thread)
            start_frame: Starting frame index (for pagination)
            levels: Maximum number of frames to return

        Returns:
            List of stack frames with id, name, source, line, column
        """
        try:
            frames = await session.get_stack_trace(thread_id, start_frame, levels)
            return {
                "success": True,
                "data": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "source": f.source,
                        "line": f.line,
                        "column": f.column,
                    }
                    for f in frames
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_scopes(frame_id: int | None = None) -> dict:
        """
        Get variable scopes for a stack frame.

        Args:
            frame_id: Optional frame ID (defaults to current frame)

        Returns:
            List of scopes (Locals, Arguments, etc.) with variablesReference
        """
        try:
            scopes = await session.get_scopes(frame_id)
            return {
                "success": True,
                "data": [
                    {
                        "name": s.get("name", ""),
                        "variablesReference": s.get("variablesReference", 0),
                        "expensive": s.get("expensive", False),
                    }
                    for s in scopes
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_variables(variables_reference: int) -> dict:
        """
        Get variables for a scope or structured variable.

        Args:
            variables_reference: The reference ID from scopes or parent variable

        Returns:
            List of variables with name, value, type, and nested reference
        """
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
                        "namedVariables": v.named_variables,
                        "indexedVariables": v.indexed_variables,
                    }
                    for v in variables
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def evaluate_expression(
        expression: str,
        frame_id: int | None = None,
        context: str = "watch",
    ) -> dict:
        """
        Evaluate an expression in the current debug context.

        Args:
            expression: The expression to evaluate (e.g., "myVar.ToString()")
            frame_id: Optional frame ID for context (defaults to current frame)
            context: Evaluation context: "watch", "repl", or "hover"

        Returns:
            Evaluation result with value, type, and nested reference
        """
        try:
            result = await session.evaluate(expression, frame_id, context)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_exception_info(thread_id: int | None = None) -> dict:
        """
        Get information about the current exception.

        Args:
            thread_id: Optional thread ID (defaults to current thread)

        Returns:
            Exception details including exceptionId, description, and breakMode
        """
        try:
            info = await session.get_exception_info(thread_id)
            if info:
                return {"success": True, "data": info}
            else:
                return {"success": True, "data": None, "message": "No exception"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_output(clear: bool = False) -> dict:
        """
        Get debug output (stdout/stderr from debugged program).

        Args:
            clear: Whether to clear the output buffer after reading

        Returns:
            Output text from the debug session
        """
        try:
            output = "".join(session.state.output_buffer)
            if clear:
                session.state.output_buffer.clear()
            return {"success": True, "data": {"output": output}}
        except Exception as e:
            return {"success": False, "error": str(e)}

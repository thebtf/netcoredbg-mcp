"""Output and build diagnostics tools."""

import logging
import re

from mcp.server.fastmcp import FastMCP

from ..session import SessionManager

from ..response import build_error_response, build_response

logger = logging.getLogger(__name__)


def register_output_tools(
    mcp: FastMCP,
    session: SessionManager,
) -> None:
    """Register output and build diagnostics tools on the MCP server."""
    from mcp.types import ToolAnnotations

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
    async def get_output(clear: bool = False, category: str | None = None) -> dict:
        """Get stdout/stderr output from the debugged program.

        IMPORTANT: The user cannot see this output directly.
        YOU must read it and summarize relevant information for the user.
        Never tell the user to "check the console" or "look at output".

        Call periodically during debugging to catch log messages and errors.

        Args:
            clear: Clear the output buffer after reading (default False)
            category: Filter by category: "stdout", "stderr", or "console" (default: all)
        """
        try:
            entries = session.state.output_buffer
            if category:
                entries = [e for e in entries if e.category == category]
            output = "".join(e.text for e in entries)
            if clear and not category:
                session.state.output_buffer.clear()
            return build_response(
                data={"output": output},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def search_output(
        pattern: str, context_lines: int = 2, category: str | None = None,
    ) -> dict:
        """Search program output for a pattern (regex supported).

        Use this instead of get_output when looking for specific messages,
        errors, or log entries in large output. Returns matching lines with context.

        Args:
            pattern: Regex pattern to search for (case-insensitive)
            context_lines: Number of lines before/after each match (default 2)
            category: Filter by category: "stdout", "stderr", or "console" (default: all)

        Returns:
            List of matches with line numbers and context
        """
        try:
            entries = session.state.output_buffer
            if category:
                entries = [e for e in entries if e.category == category]
            output = "".join(e.text for e in entries)
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
    async def get_output_tail(lines: int = 50, category: str | None = None) -> dict:
        """Get the last N lines of program output.

        Useful for checking recent output without loading everything.
        The user cannot see this - summarize relevant info for them.

        Args:
            lines: Number of lines to return (default 50)
            category: Filter by category: "stdout", "stderr", or "console" (default: all)
        """
        try:
            entries = session.state.output_buffer
            if category:
                entries = [e for e in entries if e.category == category]
            output = "".join(e.text for e in entries)
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

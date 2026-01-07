"""Breakpoint management MCP tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import Server
    from ..session import SessionManager


def register_breakpoint_tools(server: "Server", session: "SessionManager") -> None:
    """Register breakpoint tools with MCP server."""

    @server.tool()
    async def add_breakpoint(
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> dict:
        """
        Add a breakpoint at a specific line in a source file.

        Args:
            file: Absolute path to the source file
            line: Line number (1-based)
            condition: Optional condition expression (e.g., "x > 5")
            hit_condition: Optional hit count condition (e.g., "5" or ">= 10")

        Returns:
            Breakpoint info including verified status
        """
        try:
            bp = await session.add_breakpoint(
                file=file,
                line=line,
                condition=condition,
                hit_condition=hit_condition,
            )
            return {
                "success": True,
                "data": {
                    "file": bp.file,
                    "line": bp.line,
                    "condition": bp.condition,
                    "hitCondition": bp.hit_condition,
                    "verified": bp.verified,
                    "id": bp.id,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def remove_breakpoint(file: str, line: int) -> dict:
        """
        Remove a breakpoint from a specific line.

        Args:
            file: Absolute path to the source file
            line: Line number where breakpoint is set

        Returns:
            Result with removed status
        """
        try:
            removed = await session.remove_breakpoint(file, line)
            return {
                "success": True,
                "data": {"removed": removed, "file": file, "line": line},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def list_breakpoints(file: str | None = None) -> dict:
        """
        List all breakpoints or breakpoints in a specific file.

        Args:
            file: Optional file path to filter breakpoints

        Returns:
            List of breakpoints grouped by file
        """
        try:
            if file:
                bps = session.breakpoints.get_for_file(file)
                result = {
                    file: [
                        {
                            "line": bp.line,
                            "condition": bp.condition,
                            "hitCondition": bp.hit_condition,
                            "verified": bp.verified,
                            "id": bp.id,
                        }
                        for bp in bps
                    ]
                }
            else:
                all_bps = session.breakpoints.get_all()
                result = {
                    f: [
                        {
                            "line": bp.line,
                            "condition": bp.condition,
                            "hitCondition": bp.hit_condition,
                            "verified": bp.verified,
                            "id": bp.id,
                        }
                        for bp in bps
                    ]
                    for f, bps in all_bps.items()
                }
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def clear_breakpoints(file: str | None = None) -> dict:
        """
        Clear breakpoints from a file or all files.

        Args:
            file: Optional file path. If not specified, clears ALL breakpoints.

        Returns:
            Count of removed breakpoints
        """
        try:
            count = await session.clear_breakpoints(file)
            return {
                "success": True,
                "data": {
                    "removed": count,
                    "file": file if file else "all",
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

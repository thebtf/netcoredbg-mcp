"""Debug control MCP tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import Server
    from ..session import SessionManager


def register_control_tools(server: "Server", session: "SessionManager") -> None:
    """Register debug control tools with MCP server."""

    @server.tool()
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
            cwd: Working directory for the program (defaults to program's directory)
            args: Command line arguments to pass to the program
            env: Environment variables to set
            stop_at_entry: Whether to stop at the entry point

        Returns:
            Result with success status and program path
        """
        try:
            result = await session.launch(
                program=program,
                cwd=cwd,
                args=args,
                env=env,
                stop_at_entry=stop_at_entry,
            )
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def attach_debug(process_id: int) -> dict:
        """
        Attach to a running .NET process.

        Args:
            process_id: PID of the process to attach to

        Returns:
            Result with success status
        """
        try:
            result = await session.attach(process_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def stop_debug() -> dict:
        """
        Stop the current debug session.

        Returns:
            Result with success status
        """
        try:
            result = await session.stop()
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def continue_execution(thread_id: int | None = None) -> dict:
        """
        Continue program execution.

        Args:
            thread_id: Optional thread ID to continue (defaults to current thread)

        Returns:
            Result with success status
        """
        try:
            result = await session.continue_execution(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def pause_execution(thread_id: int | None = None) -> dict:
        """
        Pause program execution.

        Args:
            thread_id: Optional thread ID to pause (defaults to all threads)

        Returns:
            Result with success status
        """
        try:
            result = await session.pause(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def step_over(thread_id: int | None = None) -> dict:
        """
        Step over to the next line (don't enter functions).

        Args:
            thread_id: Optional thread ID (defaults to current thread)

        Returns:
            Result with success status
        """
        try:
            result = await session.step_over(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def step_into(thread_id: int | None = None) -> dict:
        """
        Step into the next function call.

        Args:
            thread_id: Optional thread ID (defaults to current thread)

        Returns:
            Result with success status
        """
        try:
            result = await session.step_in(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def step_out(thread_id: int | None = None) -> dict:
        """
        Step out of the current function.

        Args:
            thread_id: Optional thread ID (defaults to current thread)

        Returns:
            Result with success status
        """
        try:
            result = await session.step_out(thread_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @server.tool()
    async def get_debug_state() -> dict:
        """
        Get the current debug session state.

        Returns:
            Current state including: state (idle/running/stopped/etc),
            currentThreadId, stopReason, threads, exitCode
        """
        return {
            "success": True,
            "data": session.state.to_dict(),
        }

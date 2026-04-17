"""Entry point for netcoredbg-mcp server."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .server import create_server, get_session
from .utils.project import configure_project_root, get_project_root_sync

# Transport errors that indicate the stdio pipe was closed from the other
# side — either the MCP client (Claude Code / IDE) itself, or an intermediate
# multiplexer like mcp-mux. These are not server bugs; the correct response is
# a clean, silent shutdown so the parent process can restart us on demand.
#
# BrokenPipeError / ConnectionResetError: OS-level pipe closure on write.
# EOFError: stdin returned empty read (other side hung up).
# anyio.ClosedResourceError / anyio.EndOfStream: anyio stdio_server wrappers
#   used by FastMCP translate pipe closures into these. Importing anyio at the
#   module level keeps the tuple static so the except clause can reference it
#   by name with no runtime import cost.
try:
    import anyio as _anyio
    _TRANSPORT_SHUTDOWN_ERRORS: tuple[type[BaseException], ...] = (
        BrokenPipeError,
        ConnectionResetError,
        EOFError,
        _anyio.ClosedResourceError,
        _anyio.EndOfStream,
    )
except ImportError:
    _TRANSPORT_SHUTDOWN_ERRORS = (BrokenPipeError, ConnectionResetError, EOFError)


def configure_logging() -> None:
    """Configure logging based on environment."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Prevent duplicate handlers if called multiple times
    if root_logger.handlers:
        return

    # Console handler (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)

    # File handler for debugging
    log_file = os.environ.get("LOG_FILE", "")
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(log_format))
        root_logger.addHandler(file_handler)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="NetCoreDbg MCP Server - Debug .NET applications via MCP"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project root path for debugging. "
        "All debug operations will be constrained to this path.",
    )
    parser.add_argument(
        "--project-from-cwd",
        action="store_true",
        default=False,
        help="Auto-detect project from current working directory. "
        "Searches upward for .sln, .csproj/.vbproj/.fsproj, or .git markers. "
        "Also uses MCP roots from client if available. "
        "Intended for CLI-based agents like Claude Code. "
        "Cannot be used with --project.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        default=False,
        help="Run first-time setup: download netcoredbg, scan dbgshim "
        "versions, build FlaUI bridge, generate MCP configuration.",
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    configure_logging()
    logger = logging.getLogger(__name__)

    args = parse_args()

    # Handle --setup: run wizard and exit (don't start MCP server)
    if getattr(args, "setup", False):
        from .setup.wizard import run_setup
        sys.exit(run_setup())

    # Capture CWD at startup (before any chdir)
    startup_cwd = Path.cwd()
    logger.info(f"[DIAGNOSTIC] Startup CWD: {startup_cwd}")
    logger.info(f"[DIAGNOSTIC] __file__: {__file__}")

    # Validate mutually exclusive options
    project_from_cwd = getattr(args, "project_from_cwd", False)
    if project_from_cwd and args.project is not None:
        logger.error("--project-from-cwd cannot be used with --project")
        sys.exit(1)

    # Configure project root detection
    configure_project_root(
        use_project_from_cwd=project_from_cwd,
        explicit_project_path=args.project,
        startup_cwd=startup_cwd,
    )

    # Get initial project root (without MCP context - that comes later from tools)
    project_path = get_project_root_sync()

    if project_path:
        logger.info(f"Starting NetCoreDbg MCP Server (project: {project_path})")
    else:
        logger.info(
            "Starting NetCoreDbg MCP Server (project root will be determined from MCP client)"
        )

    # Create server - pass project_path for backwards compatibility
    # The server will also use get_project_root() with Context for dynamic resolution
    mcp = create_server(str(project_path) if project_path else None)

    # Startup: configure process registry PID file and clean up orphans
    session_obj = get_session()
    if project_path:
        pidfile = project_path / ".netcoredbg-mcp.pid"
        session_obj.process_registry.set_pidfile_path(pidfile)
        reaped = session_obj.process_registry.load_and_reap()
        if reaped:
            logger.info(f"Startup cleanup: reaped {reaped} orphaned processes")

    try:
        # Run with x-mux experimental capability for mcp-mux session awareness.
        # This tells mcp-mux to inject _meta.muxSessionId into every request,
        # enabling session ownership tracking for multi-agent environments.
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            init_options = mcp._mcp_server.create_initialization_options(
                experimental_capabilities={
                    "x-mux": {"sharing": "isolated"},
                },
            )
            await mcp._mcp_server.run(read_stream, write_stream, init_options)
    except _TRANSPORT_SHUTDOWN_ERRORS as exc:
        # Transport was closed from the client side (Claude Code dropped stdio,
        # mcp-mux broadcast collision, pipe buffer full under backpressure, etc.).
        # Don't log a traceback — a closed pipe is a normal shutdown signal, not
        # a bug in this server. The parent process (IDE / mcp-mux) will restart
        # this subprocess if the user runs another debug command.
        logger.info("Transport closed by client: %s: %s", type(exc).__name__, exc)
    except Exception:
        logger.exception("Server error")
        raise
    finally:
        # Cleanup resources
        session_obj = get_session()
        if session_obj.is_active:
            await session_obj.stop()
        # Shutdown process registry (terminate tracked processes, delete pidfile)
        session_obj.process_registry.shutdown()
        logger.info("Server stopped")


def run() -> None:
    """Run the server."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except _TRANSPORT_SHUTDOWN_ERRORS:
        # Exit code 0 on transport shutdown so the process manager (systemd,
        # mcp-mux, IDE) treats us as cleanly exited, not crashed.
        sys.exit(0)


if __name__ == "__main__":
    run()

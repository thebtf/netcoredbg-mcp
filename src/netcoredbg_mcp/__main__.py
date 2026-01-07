"""Entry point for netcoredbg-mcp server."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .server import create_server, get_session
from .utils.project import configure_project_root, get_project_root_sync


def configure_logging() -> None:
    """Configure logging based on environment."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
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
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    configure_logging()
    logger = logging.getLogger(__name__)

    args = parse_args()

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

    try:
        await mcp.run_stdio_async()
    except Exception:
        logger.exception("Server error")
        raise
    finally:
        # Cleanup session
        session = get_session()
        if session.is_active:
            await session.stop()
        logger.info("Server stopped")


def run() -> None:
    """Run the server."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()

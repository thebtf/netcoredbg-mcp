"""Entry point for netcoredbg-mcp server."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Iterator

from .server import create_server, get_session


def find_project_root(root: str | Path | None = None) -> str:
    """Find .NET project root by walking up from CWD.

    Searches for project markers in this order:
    1. .sln (solution file) - preferred for multi-project setups
    2. .csproj/.vbproj/.fsproj (project files)
    3. .git (git root as fallback)

    Falls back to CWD if no marker is found.

    Args:
        root: If provided, constrains search to this directory and below.
              Search stops at this boundary.

    Returns:
        Absolute path to project root (falls back to CWD if no marker found)
    """
    current = Path.cwd().resolve()
    boundary = Path(root).resolve() if root is not None else None

    def ancestors() -> Iterator[Path]:
        """Yield current directory and ancestors up to boundary."""
        yield current
        for parent in current.parents:
            yield parent
            if boundary is not None and parent == boundary:
                return

    # First pass: look for .sln (solution - most specific for .NET)
    for directory in ancestors():
        if any(directory.glob("*.sln")):
            return str(directory)

    # Second pass: look for project files (.csproj, .vbproj, .fsproj)
    for directory in ancestors():
        if any(directory.glob("*.csproj")) or any(directory.glob("*.vbproj")) or any(directory.glob("*.fsproj")):
            return str(directory)

    # Third pass: look for .git
    for directory in ancestors():
        if (directory / ".git").exists():  # .git can be file (worktree) or dir
            return str(directory)

    # Fall back to CWD
    return str(current)


def configure_logging() -> None:
    """Configure logging based on environment."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )


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
        "Intended for CLI-based agents like Claude Code. "
        "Cannot be used with --project.",
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    configure_logging()
    logger = logging.getLogger(__name__)

    args = parse_args()

    # Handle --project-from-cwd
    project_from_cwd = getattr(args, "project_from_cwd", False)
    if project_from_cwd:
        if args.project is not None:
            logger.error("--project-from-cwd cannot be used with --project")
            sys.exit(1)
        project_path = find_project_root()
        logger.info(f"Auto-detected project root: {project_path}")
    else:
        project_path = args.project or os.getcwd()

    logger.info(f"Starting NetCoreDbg MCP Server (project: {project_path})...")

    mcp = create_server(project_path)

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

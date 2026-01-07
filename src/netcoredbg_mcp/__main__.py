"""Entry point for netcoredbg-mcp server."""

import argparse
import asyncio
import logging
import os
import sys

from .server import create_server, get_session


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
        help="Project root path for debugging (default: current working directory). "
        "All debug operations will be constrained to this path.",
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    configure_logging()
    logger = logging.getLogger(__name__)

    args = parse_args()
    project_path = args.project or os.getcwd()

    logger.info(f"Starting NetCoreDbg MCP Server (project: {project_path})...")

    mcp = create_server(project_path)

    try:
        await mcp.run_stdio_async()
    except Exception as e:
        logger.error(f"Server error: {e}")
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

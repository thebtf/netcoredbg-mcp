"""Entry point for netcoredbg-mcp server."""

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


async def main() -> None:
    """Main entry point."""
    configure_logging()
    logger = logging.getLogger(__name__)

    logger.info("Starting NetCoreDbg MCP Server...")

    mcp = create_server()

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

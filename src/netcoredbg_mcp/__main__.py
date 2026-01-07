"""Entry point for netcoredbg-mcp server."""

import asyncio
import logging
import sys

from mcp.server.stdio import stdio_server

from .server import create_server


def configure_logging() -> None:
    """Configure logging based on environment."""
    import os

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

    server, session = create_server()

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    except Exception as e:
        logger.error(f"Server error: {e}")
        raise
    finally:
        # Cleanup session
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

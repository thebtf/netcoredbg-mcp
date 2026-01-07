"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import logging
import os

from mcp.server import Server

from .session import SessionManager
from .tools import (
    register_control_tools,
    register_breakpoint_tools,
    register_inspection_tools,
)
from .resources import register_resources

logger = logging.getLogger(__name__)


def create_server() -> tuple[Server, SessionManager]:
    """Create and configure the MCP server."""
    # Get netcoredbg path from environment
    netcoredbg_path = os.environ.get("NETCOREDBG_PATH")

    # Create server and session
    server = Server("netcoredbg-mcp")
    session = SessionManager(netcoredbg_path)

    # Register all tools
    register_control_tools(server, session)
    register_breakpoint_tools(server, session)
    register_inspection_tools(server, session)

    # Register resources
    register_resources(server, session)

    logger.info("NetCoreDbg MCP Server initialized")
    return server, session

"""MCP Tools for debugging."""

from .control import register_control_tools
from .breakpoints import register_breakpoint_tools
from .inspection import register_inspection_tools

__all__ = [
    "register_control_tools",
    "register_breakpoint_tools",
    "register_inspection_tools",
]

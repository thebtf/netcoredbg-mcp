"""Distribution & auto-setup for netcoredbg-mcp.

Manages ~/.netcoredbg-mcp/ home directory with:
- config.json for version tracking and settings
- netcoredbg/ for the debugger binary
- bridge/ for the FlaUI bridge EXE
- dbgshim/ for cached dbgshim.dll versions
"""

from .home import get_config, get_home_dir, save_config

__all__ = ["get_home_dir", "get_config", "save_config"]

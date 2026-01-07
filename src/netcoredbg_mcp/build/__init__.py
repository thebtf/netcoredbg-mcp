"""Build orchestration module for .NET projects.

Provides VSCode-like pre-launch build functionality with:
- Clean, restore, build, rebuild operations
- Per-workspace async lock with state machine
- Windows Job Objects for process tree cleanup
- Process cleanup before build (kills processes holding file locks)
- Security: argument whitelisting, path validation, TOCTOU prevention
"""

from .policy import BuildPolicy, BuildCommand
from .state import BuildState, BuildResult, BuildError
from .session import BuildSession
from .manager import BuildManager
from .cleanup import cleanup_for_build, kill_processes_in_directory, kill_debugger_processes

__all__ = [
    "BuildPolicy",
    "BuildCommand",
    "BuildState",
    "BuildResult",
    "BuildError",
    "BuildSession",
    "BuildManager",
    "cleanup_for_build",
    "kill_processes_in_directory",
    "kill_debugger_processes",
]

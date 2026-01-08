"""Build orchestration module for .NET projects.

Provides VSCode-like pre-launch build functionality with:
- Clean, restore, build, rebuild operations
- Per-workspace async lock with state machine
- Windows Job Objects for process tree cleanup
- Process cleanup before build (kills processes holding file locks)
- Security: argument whitelisting, path validation, TOCTOU prevention
"""

from .cleanup import cleanup_for_build, kill_debugger_processes, kill_processes_in_directory
from .manager import BuildManager
from .policy import BuildCommand, BuildPolicy
from .session import BuildSession
from .state import BuildError, BuildResult, BuildState

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

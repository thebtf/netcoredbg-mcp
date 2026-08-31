"""Build orchestration module for .NET projects.

Provides VSCode-like pre-launch build functionality with:
- Restore, build, clean, and rebuild operations
- Per-workspace async lock with state machine
- Explicit owner-gated pre-launch sequencing
- Security: argument whitelisting, path validation, TOCTOU prevention
"""

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
]

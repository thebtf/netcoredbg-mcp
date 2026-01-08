"""Build manager - singleton orchestrating all build sessions.

Provides:
- Per-workspace session management
- Pre-launch build integration
- Global build cancellation
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from .policy import BuildCommand
from .session import BuildSession
from .state import BuildError, BuildResult, BuildState

logger = logging.getLogger(__name__)


class BuildManager:
    """Singleton manager for build sessions across workspaces.

    Usage:
        manager = BuildManager()
        result = await manager.build("/path/to/workspace", "Project.csproj")
    """

    _instance: BuildManager | None = None
    _lock = asyncio.Lock()

    def __new__(cls) -> BuildManager:
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize manager (only once)."""
        if self._initialized:
            return
        self._sessions: dict[str, BuildSession] = {}
        self._global_listeners: list[Callable[[str, BuildState], None]] = []
        self._initialized = True

    def _normalize_path(self, path: str) -> str:
        """Normalize path for consistent key lookup."""
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def get_session(self, workspace_root: str) -> BuildSession:
        """Get or create build session for workspace.

        Note: This method is not thread-safe. It should be called from a single
        asyncio event loop. For MCP servers, this is guaranteed by the framework.

        Args:
            workspace_root: Root directory of workspace

        Returns:
            Build session for workspace
        """
        key = self._normalize_path(workspace_root)
        if key not in self._sessions:
            session = BuildSession(workspace_root)
            # Wire up global listeners
            session.on_state_change(
                lambda state: self._notify_listeners(workspace_root, state)
            )
            self._sessions[key] = session
        return self._sessions[key]

    def _notify_listeners(self, workspace: str, state: BuildState) -> None:
        """Notify global state listeners."""
        for listener in self._global_listeners:
            try:
                listener(workspace, state)
            except Exception:
                logger.exception("Global build listener error")

    def on_build_state_change(
        self, listener: Callable[[str, BuildState], None]
    ) -> None:
        """Register global build state change listener.

        Listener receives (workspace_path, new_state).
        """
        self._global_listeners.append(listener)

    async def build(
        self,
        workspace_root: str,
        project_path: str,
        command: BuildCommand = BuildCommand.BUILD,
        configuration: str = "Debug",
        extra_args: list[str] | None = None,
        timeout: float = 300.0,
    ) -> BuildResult:
        """Execute build in workspace.

        Args:
            workspace_root: Root directory of workspace
            project_path: Path to project file (relative or absolute)
            command: Build command to execute
            configuration: Build configuration
            extra_args: Additional arguments
            timeout: Timeout in seconds

        Returns:
            Build result
        """
        session = self.get_session(workspace_root)

        # Make project path absolute if relative
        if not os.path.isabs(project_path):
            project_path = os.path.join(workspace_root, project_path)

        return await session.build(
            project_path, command, configuration, extra_args, timeout
        )

    async def pre_launch_build(
        self,
        workspace_root: str,
        project_path: str,
        configuration: str = "Debug",
        restore_first: bool = True,
        cleanup_before_build: bool = True,
        timeout: float = 300.0,
    ) -> BuildResult:
        """Execute pre-launch build sequence (restore + build).

        This is the equivalent of VSCode's preLaunchTask for debugging.
        Automatically cleans up processes holding file locks before building.

        Args:
            workspace_root: Root directory of workspace
            project_path: Path to project file
            configuration: Build configuration
            restore_first: Whether to run restore before build
            cleanup_before_build: Kill processes in output dirs (default True)
            timeout: Total timeout for all operations

        Returns:
            Build result

        Raises:
            BuildError: If build fails
        """
        session = self.get_session(workspace_root)

        # Make project path absolute if relative
        if not os.path.isabs(project_path):
            project_path = os.path.join(workspace_root, project_path)

        if restore_first:
            # Run restore first (with half the timeout)
            restore_result = await session.restore(project_path, timeout=timeout / 2)
            if not restore_result.success:
                raise BuildError(
                    f"Restore failed: {restore_result.error_count} errors",
                    diagnostics=restore_result.diagnostics,
                    exit_code=restore_result.exit_code,
                )

        # Run build with cleanup and retry
        result = await session.build(
            project_path,
            BuildCommand.BUILD,
            configuration,
            timeout=timeout if not restore_first else timeout / 2,
            cleanup_before_build=cleanup_before_build,
            retry_on_lock=True,
        )

        if not result.success:
            raise BuildError(
                f"Build failed: {result.error_count} errors",
                diagnostics=result.diagnostics,
                exit_code=result.exit_code,
            )

        return result

    async def cancel(self, workspace_root: str) -> bool:
        """Cancel build in workspace.

        Args:
            workspace_root: Root directory of workspace

        Returns:
            True if a build was cancelled
        """
        key = self._normalize_path(workspace_root)
        if key in self._sessions:
            return await self._sessions[key].cancel()
        return False

    async def cancel_all(self) -> int:
        """Cancel all running builds.

        Returns:
            Number of builds cancelled
        """
        cancelled = 0
        for session in self._sessions.values():
            if await session.cancel():
                cancelled += 1
        return cancelled

    def get_state(self, workspace_root: str) -> BuildState | None:
        """Get current build state for workspace.

        Args:
            workspace_root: Root directory of workspace

        Returns:
            Build state or None if no session exists
        """
        key = self._normalize_path(workspace_root)
        if key in self._sessions:
            return self._sessions[key].state
        return None

    def get_last_result(self, workspace_root: str) -> BuildResult | None:
        """Get last build result for workspace.

        Args:
            workspace_root: Root directory of workspace

        Returns:
            Last build result or None
        """
        key = self._normalize_path(workspace_root)
        if key in self._sessions:
            return self._sessions[key].last_result
        return None

    def get_all_states(self) -> dict[str, BuildState]:
        """Get build states for all workspaces.

        Returns:
            Dictionary of workspace path to build state
        """
        return {path: session.state for path, session in self._sessions.items()}

    def clear_session(self, workspace_root: str) -> bool:
        """Remove session for workspace.

        Args:
            workspace_root: Root directory of workspace

        Returns:
            True if session was removed
        """
        key = self._normalize_path(workspace_root)
        if key in self._sessions:
            del self._sessions[key]
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Get manager status as dictionary.

        Returns:
            Status dictionary with all sessions
        """
        return {
            "sessions": {
                path: {
                    "state": session.state.value,
                    "lastResult": (
                        session.last_result.to_dict() if session.last_result else None
                    ),
                }
                for path, session in self._sessions.items()
            }
        }

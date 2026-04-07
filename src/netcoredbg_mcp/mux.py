"""mcp-mux session awareness helpers.

When netcoredbg-mcp runs behind mcp-mux (transparent MCP multiplexer),
each request carries _meta.muxSessionId identifying the calling agent.
This module provides helpers for extracting session identity and guarding
mutating operations so only the session owner can control the debug session.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

logger = logging.getLogger(__name__)

# How long before an owning session is considered stale (seconds)
SESSION_OWNERSHIP_TIMEOUT = float(os.environ.get("NETCOREDBG_SESSION_TIMEOUT", "60.0"))


def get_mux_session_id(ctx: Context) -> str | None:
    """Extract muxSessionId from request context.

    When behind mcp-mux in session-aware mode, every request has
    _meta.muxSessionId injected. Returns None if not behind mux.

    Args:
        ctx: MCP Context from a tool call

    Returns:
        Session ID string (e.g., "sess_a1b2c3d4") or None
    """
    try:
        request_ctx = ctx.request_context
        meta = request_ctx.meta
        if meta is None:
            return None

        # muxSessionId is an extra field on Meta (pydantic extra="allow")
        session_id = getattr(meta, "muxSessionId", None)
        if session_id is not None:
            return str(session_id)
        extra = getattr(meta, "model_extra", None)
        if extra and isinstance(extra, dict):
            raw = extra.get("muxSessionId")
            return str(raw) if raw is not None else None
        return None
    except (AttributeError, ValueError):
        return None


class SessionOwnership:
    """Tracks which mux session owns the debug session.

    Only one session can own the debug session at a time.
    Read-only operations are allowed from any session.
    Ownership auto-releases after SESSION_OWNERSHIP_TIMEOUT of inactivity.
    """

    def __init__(self):
        self._owner_session_id: str | None = None
        self._last_activity: float = 0.0

    @property
    def owner(self) -> str | None:
        """Current owner session ID, or None if unclaimed."""
        if self._owner_session_id and self._is_stale():
            logger.info(
                f"Session ownership expired for {self._owner_session_id} "
                f"(inactive for {SESSION_OWNERSHIP_TIMEOUT}s)"
            )
            self._owner_session_id = None
        return self._owner_session_id

    def claim(self, session_id: str) -> None:
        """Claim ownership of the debug session."""
        self._owner_session_id = session_id
        self._last_activity = time.monotonic()
        logger.info(f"Debug session claimed by {session_id}")

    def release(self) -> None:
        """Release ownership."""
        if self._owner_session_id:
            logger.info(f"Debug session released by {self._owner_session_id}")
        self._owner_session_id = None

    def touch(self) -> None:
        """Update last activity timestamp (called on each mutating operation)."""
        self._last_activity = time.monotonic()

    def check_access(self, session_id: str | None) -> str | None:
        """Check if a session has access to mutating operations.

        Args:
            session_id: The requesting session's mux ID, or None if not behind mux

        Returns:
            None if access granted, error message string if denied
        """
        # Not behind mux — single-client mode, always allowed
        if session_id is None:
            return None

        owner = self.owner  # triggers stale check

        # No owner — auto-claim
        if owner is None:
            self.claim(session_id)
            return None

        # Same session — allowed
        if owner == session_id:
            self.touch()
            return None

        # Different session — denied
        return (
            f"Debug session is owned by another agent (session {owner}). "
            f"Use cleanup_processes(force=True) to terminate the session "
            f"and take over, or wait for the owner to finish."
        )

    def _is_stale(self) -> bool:
        """Check if ownership has timed out."""
        if self._last_activity == 0.0:
            return False
        return (time.monotonic() - self._last_activity) > SESSION_OWNERSHIP_TIMEOUT

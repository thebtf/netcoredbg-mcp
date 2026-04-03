"""Per-session temporary file manager for screenshots and artifacts.

Provides isolated temp directories per debug session with automatic
cleanup on session end, server exit, and stale directory GC.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

TEMP_PREFIX = "mcp-netcoredbg-"


class SessionTempManager:
    """Manages per-session temp directories for screenshot storage.

    Thread-safe. Each session gets an isolated directory that is cleaned
    up on stop_debug, atexit, or stale GC.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Path] = {}
        self._lock = threading.Lock()

    def get_session_dir(self, session_id: str | None = None) -> Path | None:
        """Get or create a temp directory for the given session.

        Args:
            session_id: Session identifier. If None, generates a UUID4 prefix.

        Returns:
            Path to session temp directory, or None if creation fails.
        """
        if session_id is None:
            session_id = uuid.uuid4().hex[:12]

        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.exists():
                return existing

            # Create under lock to prevent duplicate dirs for same session_id
            try:
                dir_path = Path(tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{session_id}-"))
                self._sessions[session_id] = dir_path
                logger.info("Created session temp dir: %s", dir_path)
                return dir_path
            except OSError as e:
                logger.warning("Failed to create session temp dir: %s", e)
                return None

    def save_screenshot(self, session_id: str, data: bytes, name: str) -> Path | None:
        """Save screenshot data to the session temp directory.

        Args:
            session_id: Session identifier.
            data: Screenshot bytes to save.
            name: Filename for the screenshot.

        Returns:
            Absolute path to saved file, or None if save fails.
        """
        session_dir = self.get_session_dir(session_id)
        if session_dir is None:
            return None

        # Sanitize filename: strip path separators to prevent traversal
        safe_name = Path(name).name
        if not safe_name or safe_name in (".", ".."):
            logger.warning("Invalid screenshot name rejected: %s", name)
            return None

        file_path = session_dir / safe_name
        try:
            file_path.write_bytes(data)
            return file_path
        except OSError as e:
            logger.warning("Failed to save screenshot %s: %s", name, e)
            return None

    def cleanup_session(self, session_id: str) -> None:
        """Remove the temp directory for a specific session.

        Args:
            session_id: Session identifier to clean up.
        """
        with self._lock:
            dir_path = self._sessions.pop(session_id, None)

        if dir_path is not None:
            shutil.rmtree(dir_path, ignore_errors=True)
            logger.info("Cleaned up session temp dir: %s", dir_path)

    def cleanup_all(self) -> None:
        """Remove all managed temp directories. Suitable for atexit handler."""
        with self._lock:
            sessions_copy = dict(self._sessions)
            self._sessions.clear()

        for session_id, dir_path in sessions_copy.items():
            shutil.rmtree(dir_path, ignore_errors=True)
            logger.debug("Cleaned up temp dir for session %s", session_id)

        if sessions_copy:
            logger.info("Cleaned up %d session temp directories", len(sessions_copy))

    @staticmethod
    def gc_stale(max_age_hours: float = 4.0) -> int:
        """Remove stale temp directories from previous crashed sessions.

        Scans the system temp directory for dirs matching the prefix
        that are older than max_age_hours. Default 4h to avoid removing
        dirs from concurrent server instances or long debug sessions.

        Args:
            max_age_hours: Maximum age in hours before a dir is considered stale.

        Returns:
            Number of stale directories removed.
        """
        temp_root = Path(tempfile.gettempdir())
        max_age_seconds = max_age_hours * 3600
        now = time.time()
        removed = 0

        try:
            for entry in temp_root.iterdir():
                if not entry.is_dir() or not entry.name.startswith(TEMP_PREFIX):
                    continue

                try:
                    mtime = entry.stat().st_mtime
                    if (now - mtime) > max_age_seconds:
                        shutil.rmtree(entry, ignore_errors=True)
                        logger.info("Removed stale temp dir: %s (age: %.1fh)",
                                    entry, (now - mtime) / 3600)
                        removed += 1
                except OSError:
                    continue
        except OSError as e:
            logger.warning("Failed to scan temp directory for stale dirs: %s", e)

        return removed

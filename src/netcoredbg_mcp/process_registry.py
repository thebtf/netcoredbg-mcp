"""Process registry for tracking spawned debug processes.

Tracks PIDs of netcoredbg and debuggee processes with persistence
to a PID file, enabling cleanup after MCP server crashes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessEntry:
    """A tracked process entry."""
    pid: int
    role: str  # "netcoredbg", "debuggee", "build"
    program: str | None = None
    session_id: str | None = None
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessEntry:
        return cls(
            pid=data["pid"],
            role=data["role"],
            program=data.get("program"),
            session_id=data.get("session_id"),
            registered_at=data.get("registered_at", 0.0),
        )


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive. Cross-platform."""
    if pid <= 0:
        return False

    if os.name == "nt":
        return _is_pid_alive_windows(pid)
    return _is_pid_alive_unix(pid)


def _is_pid_alive_unix(pid: int) -> bool:
    """Check PID liveness on Unix via signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Check PID liveness on Windows via OpenProcess."""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle == 0:
            return False

        # Check if the process has exited
        exit_code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)

        STILL_ACTIVE = 259
        return exit_code.value == STILL_ACTIVE
    except (OSError, AttributeError):
        return False


def _terminate_pid(pid: int, timeout: float = 5.0) -> bool:
    """Terminate a process gracefully, then force kill. Cross-platform.

    Returns True if the process was successfully terminated.
    """
    if not _is_pid_alive(pid):
        return True  # Already dead

    if os.name == "nt":
        return _terminate_pid_windows(pid, timeout)
    return _terminate_pid_unix(pid, timeout)


def _terminate_pid_unix(pid: int, timeout: float) -> bool:
    """Terminate on Unix: SIGTERM → wait → SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.1)

    # Force kill
    try:
        os.kill(pid, getattr(signal, "SIGKILL", 9))
        return True
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return False


def _terminate_pid_windows(pid: int, timeout: float) -> bool:
    """Terminate on Windows: TerminateProcess."""
    try:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle == 0:
            return not _is_pid_alive(pid)

        result = ctypes.windll.kernel32.TerminateProcess(handle, 1)
        ctypes.windll.kernel32.CloseHandle(handle)

        if result:
            logger.debug(f"Terminated process PID {pid} via TerminateProcess")
            return True
        return False
    except (OSError, AttributeError):
        return False


class ProcessRegistry:
    """Tracks spawned debug processes with PID file persistence.

    Usage:
        registry = ProcessRegistry(pidfile_path="/tmp/netcoredbg-mcp.pid")
        registry.register(pid=1234, role="netcoredbg")
        registry.register(pid=5678, role="debuggee", program="App.dll")

        # Check status
        for entry in registry.get_all():
            print(f"{entry.pid}: alive={registry.is_alive(entry.pid)}")

        # Cleanup stale
        reaped = registry.reap_stale()

        # Cleanup all on shutdown
        cleaned = registry.cleanup_all()
    """

    def __init__(self, pidfile_path: str | Path | None = None):
        self._entries: dict[int, ProcessEntry] = {}
        self._pidfile_path = Path(pidfile_path) if pidfile_path else None

    @property
    def pidfile_path(self) -> Path | None:
        return self._pidfile_path

    def set_pidfile_path(self, path: str | Path) -> None:
        """Set the PID file path (e.g., after project root is determined)."""
        self._pidfile_path = Path(path)

    def register(
        self,
        pid: int,
        role: str,
        program: str | None = None,
        session_id: str | None = None,
    ) -> ProcessEntry:
        """Register a spawned process.

        Args:
            pid: Process ID
            role: Process role ("netcoredbg", "debuggee", "build")
            program: Path to the program being debugged (for debuggee)
            session_id: MCP session ID (for mux-aware tracking)

        Returns:
            The registered ProcessEntry
        """
        entry = ProcessEntry(
            pid=pid,
            role=role,
            program=program,
            session_id=session_id,
        )
        self._entries[pid] = entry
        logger.info(f"Registered process: PID={pid}, role={role}, program={program}")
        self._save()
        return entry

    def unregister(self, pid: int) -> bool:
        """Unregister a process.

        Returns True if the process was found and removed.
        """
        if pid in self._entries:
            entry = self._entries.pop(pid)
            logger.info(f"Unregistered process: PID={pid}, role={entry.role}")
            self._save()
            return True
        return False

    def is_alive(self, pid: int) -> bool:
        """Check if a registered process is still alive."""
        return _is_pid_alive(pid)

    def get_all(self) -> list[ProcessEntry]:
        """Get all registered processes."""
        return list(self._entries.values())

    def get_by_role(self, role: str) -> list[ProcessEntry]:
        """Get processes by role."""
        return [e for e in self._entries.values() if e.role == role]

    def get_stale(self) -> list[ProcessEntry]:
        """Get registered processes that are no longer alive."""
        return [e for e in self._entries.values() if not _is_pid_alive(e.pid)]

    def reap_stale(self) -> int:
        """Remove entries for dead processes.

        Returns count of entries removed.
        """
        stale = self.get_stale()
        for entry in stale:
            del self._entries[entry.pid]
            logger.info(f"Reaped stale process: PID={entry.pid}, role={entry.role}")

        if stale:
            self._save()
        return len(stale)

    def cleanup_all(self, timeout: float = 5.0) -> int:
        """Terminate all tracked processes and clear the registry.

        Terminates gracefully with timeout, then force-kills.

        Returns count of processes terminated.
        """
        terminated = 0
        for entry in list(self._entries.values()):
            if _is_pid_alive(entry.pid):
                if _terminate_pid(entry.pid, timeout):
                    logger.info(
                        f"Cleaned up process: PID={entry.pid}, role={entry.role}"
                    )
                    terminated += 1
                else:
                    logger.warning(
                        f"Failed to terminate process: PID={entry.pid}, role={entry.role}"
                    )

        self._entries.clear()
        self._save()
        return terminated

    def cleanup_session(self, session_id: str, timeout: float = 5.0) -> int:
        """Terminate all processes belonging to a specific session.

        Returns count of processes terminated.
        """
        terminated = 0
        to_remove = [
            e for e in self._entries.values() if e.session_id == session_id
        ]
        for entry in to_remove:
            if _is_pid_alive(entry.pid):
                if _terminate_pid(entry.pid, timeout):
                    terminated += 1
            del self._entries[entry.pid]

        if to_remove:
            self._save()
        return terminated

    def status(self) -> list[dict[str, Any]]:
        """Get status of all tracked processes.

        Returns list of dicts with pid, role, program, alive, registered_at.
        """
        return [
            {
                **entry.to_dict(),
                "alive": _is_pid_alive(entry.pid),
            }
            for entry in self._entries.values()
        ]

    # --- PID File Persistence ---

    def _save(self) -> None:
        """Save registry to PID file (atomic write)."""
        if not self._pidfile_path:
            return

        data = {
            "server_pid": os.getpid(),
            "saved_at": time.time(),
            "processes": [e.to_dict() for e in self._entries.values()],
        }

        try:
            self._pidfile_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to temp file, then rename
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._pidfile_path.parent),
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, str(self._pidfile_path))
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"Failed to save PID file {self._pidfile_path}: {e}")

    def load_and_reap(self) -> int:
        """Load PID file and reap stale processes from a previous server run.

        Call this at server startup to clean up after crashes.

        Returns count of stale processes reaped.
        """
        if not self._pidfile_path or not self._pidfile_path.exists():
            return 0

        try:
            data = json.loads(self._pidfile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read PID file {self._pidfile_path}: {e}")
            self._delete_pidfile()
            return 0

        prev_server_pid = data.get("server_pid")
        processes = data.get("processes", [])

        if not processes:
            self._delete_pidfile()
            return 0

        # If previous server is still alive, don't touch its processes or PID file
        if prev_server_pid and _is_pid_alive(prev_server_pid):
            logger.info(
                f"Previous server (PID {prev_server_pid}) is still alive — "
                f"leaving PID file intact"
            )
            return 0

        # Previous server is dead — clean up its orphaned processes
        logger.info(
            f"Previous server (PID {prev_server_pid}) is dead — "
            f"cleaning up {len(processes)} tracked processes"
        )

        terminated = 0
        for proc_data in processes:
            try:
                entry = ProcessEntry.from_dict(proc_data)
            except (KeyError, TypeError) as e:
                logger.debug(f"Skipping malformed process entry: {e}")
                continue

            if _is_pid_alive(entry.pid):
                if _terminate_pid(entry.pid):
                    logger.info(
                        f"Terminated orphaned process: PID={entry.pid}, "
                        f"role={entry.role}, program={entry.program}"
                    )
                    terminated += 1
                else:
                    logger.warning(
                        f"Failed to terminate orphaned process: PID={entry.pid}"
                    )
            else:
                logger.debug(f"Orphaned process already dead: PID={entry.pid}")

        self._delete_pidfile()
        return terminated

    def _delete_pidfile(self) -> None:
        """Delete the PID file."""
        if self._pidfile_path:
            try:
                self._pidfile_path.unlink(missing_ok=True)
            except OSError as e:
                logger.debug(f"Failed to delete PID file: {e}")

    def shutdown(self) -> None:
        """Cleanup all processes and delete PID file. Call on server shutdown."""
        cleaned = self.cleanup_all()
        if cleaned:
            logger.info(f"Shutdown cleanup: {cleaned} processes terminated")
        self._delete_pidfile()

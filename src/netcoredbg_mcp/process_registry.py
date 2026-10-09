"""Process observations and process-local trusted-producer cleanup callbacks."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProcessEntry:
    """Status metadata; neither its PID nor its generation grants cleanup rights."""

    pid: int
    role: str
    generation: object = field(repr=False)
    program: str | None = None
    session_id: str | None = None
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "role": self.role,
            "program": self.program,
            "session_id": self.session_id,
            "registered_at": self.registered_at,
        }


@dataclass(frozen=True)
class CleanupOutcome:
    complete: bool
    terminated: int = 0
    error: str | None = None


@dataclass(frozen=True)
class CleanupReport:
    complete: bool
    terminated: int
    remaining_owners: int
    errors: list[str]


@dataclass(eq=False)
class _RegisteredOwner:
    generation: object
    owner: object
    cleanup: Callable[[], Awaitable[CleanupOutcome]]
    task: asyncio.Task[CleanupOutcome] | None = None


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

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Check if process exists but we lack permission
            error = ctypes.GetLastError()
            error_access_denied = 5
            if error == error_access_denied:
                return True  # Process exists, we just can't open it
            return False

        # Check if the process has exited
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)

        still_active = 259
        return exit_code.value == still_active
    except (OSError, AttributeError):
        return False


class ProcessRegistry:
    """Keep observations separate from cleanup capabilities, without persistence.

    Producers retain the actual native owner in their captured callback. Entries,
    old PID files, and public parameters cannot reconstruct that capability.
    Mutation is synchronous on the server event loop; no lock spans an await.
    """

    def __init__(self) -> None:
        self._entries: dict[int, ProcessEntry] = {}
        self._owners: dict[int, _RegisteredOwner] = {}

    def observe(
        self,
        pid: int,
        role: str,
        *,
        generation: object,
        program: str | None = None,
        session_id: str | None = None,
    ) -> ProcessEntry:
        entry = ProcessEntry(pid, role, generation, program, session_id)
        self._entries[pid] = entry
        return entry

    def forget(self, observation: ProcessEntry) -> bool:
        if self._entries.get(observation.pid) is not observation:
            return False
        del self._entries[observation.pid]
        return True

    def register_owner(
        self,
        *,
        generation: object,
        owner: object,
        cleanup: Callable[[], Awaitable[CleanupOutcome]],
    ) -> object:
        key = id(owner)
        existing = self._owners.get(key)
        if existing is not None:
            if existing.generation != generation:
                raise ValueError("Captured owner cannot change generation")
            return existing
        registration = _RegisteredOwner(generation, owner, cleanup)
        self._owners[key] = registration
        return registration

    def release_owner(self, token: object) -> bool:
        if not isinstance(token, _RegisteredOwner):
            return False
        key = id(token.owner)
        if self._owners.get(key) is not token:
            return False
        del self._owners[key]
        return True

    @property
    def owner_count(self) -> int:
        return len(self._owners)

    async def _cleanup_owner(self, registration: _RegisteredOwner) -> CleanupOutcome:
        try:
            outcome = await registration.cleanup()
        except asyncio.CancelledError:
            outcome = CleanupOutcome(False, error="Owner cleanup was cancelled")
        except Exception as error:
            outcome = CleanupOutcome(False, error=str(error))
        if outcome.complete:
            self.release_owner(registration)
        return outcome

    async def cleanup_all(self) -> CleanupReport:
        """Join each captured owner once; retain incomplete owners for retry."""
        registrations = tuple(self._owners.values())
        tasks = []
        for registration in registrations:
            if registration.task is None or registration.task.done():
                registration.task = asyncio.create_task(self._cleanup_owner(registration))
            tasks.append(registration.task)
        outcomes = await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        errors = [
            outcome.error or "Owner cleanup incomplete"
            for outcome in outcomes
            if not outcome.complete
        ]
        return CleanupReport(
            complete=not errors and not self._owners,
            terminated=sum(outcome.terminated for outcome in outcomes),
            remaining_owners=len(self._owners),
            errors=errors,
        )

    def is_alive(self, pid: int) -> bool:
        return _is_pid_alive(pid)

    def get_all(self) -> list[ProcessEntry]:
        return list(self._entries.values())

    def get_by_role(self, role: str) -> list[ProcessEntry]:
        return [entry for entry in self._entries.values() if entry.role == role]

    def get_stale(self) -> list[ProcessEntry]:
        return [entry for entry in self._entries.values() if not _is_pid_alive(entry.pid)]

    def reap_stale(self) -> int:
        stale = self.get_stale()
        return sum(self.forget(entry) for entry in stale)

    def status(self) -> list[dict[str, Any]]:
        return [
            {**entry.to_dict(), "alive": _is_pid_alive(entry.pid)}
            for entry in self._entries.values()
        ]

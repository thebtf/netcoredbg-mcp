"""Test-only ownership probes for the Wave 2 RED denominator.

These fakes deliberately model the observable boundary that production must own:
a retained Job plus direct process/thread handles.  A PID, image name, path,
WMI result, or ProcessRegistry row is only an observation and never gets to
make the decision to terminate a tree.  The seam records whether a child could
execute before assignment, membership/accounting verification, I/O setup, and
resume; it is designed to be wired to ``WindowsOwnedProcess`` once that private
boundary exists.

The current Python implementation is exercised through its existing
``asyncio.create_subprocess_exec`` and post-spawn Job calls.  Therefore a RED
result describes a current launch/cleanup behavior, not the absence of a
planned production type.
"""
# Ruff's lowercase-name rule is intentionally disabled here: these fake methods
# must mirror kernel32 export names exactly so production Win32 calls exercise
# the same seam rather than a test-only adapter with different spellings.
# ruff: noqa: N802

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


class ImmediateEofStream:
    """A stream that lets the legacy subprocess path complete deterministically."""

    async def readline(self) -> bytes:
        return b""

    async def read(self, _size: int = -1) -> bytes:
        return b""


class BlockingStream:
    """A stream held open until production cleanup cancels its observer."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.release = asyncio.Event()

    async def readline(self) -> bytes:
        self.read_started.set()
        await self.release.wait()
        return b""

    async def read(self, _size: int = -1) -> bytes:
        self.read_started.set()
        await self.release.wait()
        return b""


class PollingTimeoutStream:
    """A bounded blocked-output stand-in for ``BuildSession.cancel`` tests."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()

    async def readline(self) -> bytes:
        self.read_started.set()
        await asyncio.sleep(0.001)
        raise asyncio.TimeoutError()


class TreeProcess:
    """A process whose direct root can exit while its descendant remains alive.

    This models the fact the owner boundary must prove with Job accounting:
    root exit and a closed Job handle do not establish tree drain.  ``kill``
    and ``terminate`` intentionally affect only the root so the current
    root-process cleanup path is observable without creating OS processes.
    """

    def __init__(
        self,
        *,
        pid: int,
        stdout: Any | None = None,
        stderr: Any | None = None,
        root_exits_on_terminate: bool = True,
        initially_exited: bool = False,
    ) -> None:
        self.pid = pid
        self.stdout = stdout if stdout is not None else ImmediateEofStream()
        self.stderr = stderr if stderr is not None else ImmediateEofStream()
        self.returncode: int | None = 0 if initially_exited else None
        self.child_alive = True
        self.root_exits_on_terminate = root_exits_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._root_exit = asyncio.Event()
        if initially_exited:
            self._root_exit.set()

    @property
    def active_processes(self) -> int:
        """Test-only accounting observation: root plus any live descendant."""

        return int(self.returncode is None) + int(self.child_alive)

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._root_exit.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.root_exits_on_terminate:
            self.returncode = 0
            self._root_exit.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._root_exit.set()


class RecordingKernel32:
    """One focused fake Win32 admission seam.

    It exposes the calls a future ``WindowsOwnedProcess`` must make while the
    current ``BuildSession`` still calls ``asyncio.create_subprocess_exec`` and
    then reopens a PID.  Tests inject assignment, membership, accounting, or
    resume failure here and observe that the legacy path has already allowed
    execution.  The fake contains no selector/PID discovery fallback because
    those are precisely the authority paths being removed.
    """

    def __init__(
        self,
        events: list[str],
        *,
        assign_result: bool = True,
        membership_result: bool = True,
        accounting_result: bool = True,
        resume_result: int = 1,
    ) -> None:
        self.events = events
        self.assign_result = assign_result
        self.membership_result = membership_result
        self.accounting_result = accounting_result
        self.resume_result = resume_result
        self.resume_calls = 0
        self.membership_checks = 0
        self.accounting_checks = 0
        self.terminate_job_calls = 0
        self.closed_handles: list[int] = []

    def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
        self.events.append("create-job")
        return 101

    def SetInformationJobObject(self, _job: int, *_args: object) -> bool:
        self.events.append("set-job-limits")
        return True

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:
        self.events.append("open-root-handle")
        return 202

    def AssignProcessToJobObject(self, _job: int, _process: int) -> bool:
        self.events.append("assign")
        return self.assign_result

    def IsProcessInJob(self, _process: int, _job: int, _result: object) -> bool:
        self.events.append("verify-membership")
        self.membership_checks += 1
        return self.membership_result

    def QueryInformationJobObject(self, _job: int, *_args: object) -> bool:
        self.events.append("query-accounting")
        self.accounting_checks += 1
        return self.accounting_result

    def ResumeThread(self, _thread: int) -> int:
        self.events.append("resume-thread")
        self.resume_calls += 1
        return self.resume_result

    def TerminateJobObject(self, _job: int, _exit_code: int) -> bool:
        self.events.append("terminate-job")
        self.terminate_job_calls += 1
        return True

    def CloseHandle(self, handle: int) -> bool:
        self.events.append(f"close:{handle}")
        self.closed_handles.append(handle)
        return True


def install_recording_kernel32(monkeypatch: Any, kernel32: RecordingKernel32) -> None:
    """Route current ctypes calls through the focused test seam."""

    import ctypes

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32))

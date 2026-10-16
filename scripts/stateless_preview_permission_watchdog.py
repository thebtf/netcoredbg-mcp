"""One-use Windows Job watchdog for the permission timing diagnostic.

Retire with the timing driver after its single experiment. The existing private
owner admits the suspended root before child code runs. Timeout proof additionally
requires covered pre/post-termination Job membership and signaled retained handles.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import cast

if __package__ in (None, ""):
    root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(root), str(root / "src")]

from netcoredbg_mcp.windows_process_owner import (
    AdmissionStage,
    DrainStatus,
    WindowsOwnedProcess,
    _create_suspended_process,
    _Kernel32,
    _Win32CallError,
)
from scripts import stateless_preview_permission_timing_diagnostic as driver


async def _discard(stream: asyncio.StreamReader) -> None:
    # Do not retain or echo child output, including exception text.
    while await stream.read(64 * 1024):
        pass


def _finalize_timeout(repository: Path, code: int) -> int:
    return driver.finalize_timeout(repository / driver._DIRECTORY, code)


def _allow_artifacts() -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as output:
        output.write("cleanup_verified=true\n")


class _DiagnosticApi(_Kernel32):
    def __init__(self) -> None:
        super().__init__()
        ctypes, wintypes = self._ctypes, self._wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._open_process.restype = wintypes.HANDLE
        self._get_times = kernel32.GetProcessTimes
        self._get_times.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        self._get_times.restype = wintypes.BOOL

        class ProcessIdList(ctypes.Structure):
            _fields_ = [
                ("assigned", wintypes.DWORD),
                ("returned", wintypes.DWORD),
                ("pids", ctypes.c_size_t * 64),
            ]

        self._process_id_list = ProcessIdList
        self._members: dict[tuple[int, int], int] = {}
        self._termination_started = False
        self.capture_enabled = False
        self._coverage_verified = True
        self._sealed_total: int | None = None
        self.cleanup_deadline = time.monotonic() + 5.0

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.cleanup_deadline:
            raise _Win32CallError(AdmissionStage.DRAIN, None)

    def _total_processes(self, job: int) -> int:
        self._check_deadline()
        info = self._basic_accounting_information()
        if not self._query_information(
            job, 1, self._ctypes.byref(info), self._ctypes.sizeof(info), None
        ):
            raise self._error(AdmissionStage.DRAIN)
        return int(info.TotalProcesses)

    def _snapshot(self, job: int) -> tuple[int, tuple[int, ...]]:
        while True:
            self._check_deadline()
            total_before = self._total_processes(job)
            info = self._process_id_list()
            if not self._query_information(
                job, 3, self._ctypes.byref(info), self._ctypes.sizeof(info), None
            ):
                raise self._error(AdmissionStage.DRAIN)
            if info.assigned != info.returned or info.returned > 64:
                raise _Win32CallError(AdmissionStage.DRAIN, None)
            pids = tuple(info.pids[: info.returned])
            total_after = self._total_processes(job)
            if total_before == total_after:
                return total_after, pids

    def _capture(self, job: int, pids: tuple[int, ...]) -> set[tuple[int, int]]:
        captured = set()
        for pid in pids:
            self._check_deadline()
            handle = self._open_process(0x00100000 | 0x1000, False, pid)
            if not handle:
                raise self._error(AdmissionStage.DRAIN)
            try:
                if not self.is_process_in_job(handle, job):
                    raise _Win32CallError(AdmissionStage.DRAIN, None)
                created, exited, kernel, user = (self._wintypes.FILETIME() for _ in range(4))
                if not self._get_times(
                    handle,
                    self._ctypes.byref(created),
                    self._ctypes.byref(exited),
                    self._ctypes.byref(kernel),
                    self._ctypes.byref(user),
                ):
                    raise self._error(AdmissionStage.DRAIN)
                self._check_deadline()
                started = (created.dwHighDateTime << 32) | created.dwLowDateTime
                if not started:
                    raise _Win32CallError(AdmissionStage.DRAIN, None)
                identity = (pid, started)
                captured.add(identity)
                if identity not in self._members:
                    self._members[identity] = handle
                    handle = None
            finally:
                if handle is not None:
                    self.close_handle(handle)
        return captured

    def terminate_job(self, job_handle: int) -> None:
        if not self.capture_enabled:
            # Failed admission remains entirely owned by the house cleanup/reaper.
            super().terminate_job(job_handle)
            return
        if self._termination_started:
            return
        before_total, before_members = None, set()
        try:
            before_total, pids = self._snapshot(job_handle)
            before_members = self._capture(job_handle, pids)
        except _Win32CallError:
            self._coverage_verified = False
        # AssignProcessToJobObject refuses assignment to a terminating Job.
        # https://learn.microsoft.com/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
        super().terminate_job(job_handle)
        self._termination_started = True
        try:
            after_total, pids = self._snapshot(job_handle)
            after_members = self._capture(job_handle, pids)
            if before_total is None or after_total - before_total != len(
                after_members - before_members
            ):
                self._coverage_verified = False
            self._sealed_total = after_total
        except _Win32CallError:
            self._coverage_verified = False

    def members_signaled(self, job: int) -> bool:
        self._check_deadline()
        if not self._coverage_verified:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        if self._termination_started and self._total_processes(job) != self._sealed_total:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        return all(self.wait_for_process(handle, 0) for handle in self._members.values())

    def close_members(self) -> None:
        failure = None
        for handle in self._members.values():
            try:
                self.close_handle(handle)
            except _Win32CallError as error:
                failure = error
        self._members.clear()
        if failure is not None:
            raise failure


class _DiagnosticProcess(WindowsOwnedProcess):
    _api: _DiagnosticApi
    _cleanup_deadline: float = float("inf")

    @classmethod
    async def launch(cls, **kwargs) -> _DiagnosticProcess:
        owner = await cls._launch_with(
            **kwargs,
            api=_DiagnosticApi(),
            pipe_ends=None,
            process_creator=_create_suspended_process,
        )
        return cast(_DiagnosticProcess, owner)

    async def _wait_for_zero(
        self, timeout: float, *, forced: bool, root_was_forced: bool | None = None
    ):
        self._api.capture_enabled = True
        self._api.cleanup_deadline = self._cleanup_deadline
        deadline = min(time.monotonic() + max(0.0, timeout), self._cleanup_deadline)
        while True:
            active = None
            try:
                if self._job_handle is None:
                    raise _Win32CallError(AdmissionStage.DRAIN, None)
                active = self._query_active_processes()
                if active == 0 and self._api.members_signaled(self._job_handle):
                    return self._receipt(
                        status=DrainStatus.DRAINED,
                        forced=forced,
                        active_processes=0,
                        failure_stage=None,
                        winerror=None,
                        root_was_forced=root_was_forced,
                    )
            except _Win32CallError as error:
                return self._receipt(
                    status=DrainStatus.FAILED,
                    forced=forced,
                    active_processes=active,
                    failure_stage=error.stage,
                    winerror=error.winerror,
                    root_was_forced=root_was_forced,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._receipt(
                    status=DrainStatus.TIMED_OUT,
                    forced=forced,
                    active_processes=active,
                    failure_stage=None,
                    winerror=None,
                    root_was_forced=root_was_forced,
                )
            await asyncio.sleep(min(0.01, remaining))

    async def aclose(self):
        self._cleanup_deadline = min(self._cleanup_deadline, time.monotonic() + 5.0)
        try:
            return await super().aclose()
        finally:
            self._api.close_members()


async def _run(repository: Path, *, observation_seconds: float = 120.0) -> int:
    deadline = time.monotonic() + observation_seconds
    owner = await _DiagnosticProcess.launch(
        generation=object(),
        argv=[
            sys.executable,
            str(repository / "scripts/stateless_preview_permission_timing_diagnostic.py"),
        ],
        cwd=str(repository),
        env=None,
        stdin_mode="devnull",
    )
    owner._cleanup_deadline = deadline + 5.0
    readers = [asyncio.create_task(_discard(stream)) for stream in (owner.stdout, owner.stderr)]
    try:
        observation = await owner.drain_after_grace(
            grace_timeout=max(0.0, deadline - time.monotonic()),
            # Request Job-wide termination at the cutoff, without a second observation wait.
            force_timeout=0.0,
        )
        observation_deadline_reached = time.monotonic() >= deadline
    finally:
        try:
            # Snapshots, handle acquisition and handle waits share the cutoff + five seconds.
            # Neither a failed receipt nor KILL_ON_JOB_CLOSE proves writer closure.
            closure = await owner.aclose()
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
    if (
        observation.status is DrainStatus.FAILED
        or closure.status is not DrainStatus.DRAINED
        or closure.active_processes != 0
        or type(closure.root_returncode) is not int
    ):
        print("OWNED_DIAGNOSTIC_CLEANUP_UNVERIFIED", file=sys.stderr)
        return 1
    code = closure.root_returncode
    if (
        observation.forced
        or observation.status is not DrainStatus.DRAINED
        or observation_deadline_reached
    ):
        code = _finalize_timeout(repository, code)
    _allow_artifacts()
    return code


def main() -> int:
    if os.name != "nt" or sys.argv[1:]:
        return 1
    try:
        return asyncio.run(_run(Path(__file__).resolve().parent.parent))
    except Exception:
        # No report reads or upload authorization after admission, accounting or I/O failure.
        print("OWNED_DIAGNOSTIC_WATCHDOG_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

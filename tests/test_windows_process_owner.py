"""Focused owner-admission proof for the private Windows boundary."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import time
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

import netcoredbg_mcp.windows_process_owner as windows_process_owner
from netcoredbg_mcp.build.manager import BuildManager
from netcoredbg_mcp.dap.client import DAPClient, DapTransportTerminal
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.windows_process_owner import (
    AdmissionCleanupError,
    AdmissionStage,
    DrainStatus,
    ProcessAdmissionError,
    WindowsOwnedProcess,
    _create_suspended_process,
    _FailedAdmissionReaper,
    _Kernel32,
    _Win32CallError,
)

FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "OwnerScopeAdapter"
FIXTURE_EXE = FIXTURE_PROJECT / "bin" / "Debug" / "net8.0" / "OwnerScopeAdapter.exe"


def _child_is_in_job(child_pid: int, job_handle: int) -> bool:
    """Observe controlled fixture membership; never use a PID as authority."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.IsProcessInJob.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    )
    kernel32.IsProcessInJob.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int

    process_handle = kernel32.OpenProcess(0x1000, 0, child_pid)
    if not process_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        in_job = ctypes.c_int()
        if not kernel32.IsProcessInJob(process_handle, job_handle, ctypes.byref(in_job)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(in_job.value)
    finally:
        kernel32.CloseHandle(process_handle)


async def _wait_for_path(path: Path) -> None:
    for _ in range(300):
        if path.is_file():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"fixture marker was not written: {path}")


async def _read_marker_pid(path: Path) -> int:
    for _ in range(300):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            await asyncio.sleep(0.01)
            continue
        if type(value.get("pid")) is int:
            return value["pid"]
        await asyncio.sleep(0.01)
    pytest.fail(f"fixture marker did not contain a PID: {path}")


async def _wait_for_pid_exit(pid: int) -> None:
    for _ in range(300):
        if not psutil.pid_exists(pid):
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"fixture descendant survived owner drain: {pid}")


class _FakeApi:
    def __init__(
        self,
        events: list[str],
        *,
        assign_ok: bool = True,
        membership_ok: bool = True,
        accounting_ok: bool = True,
        resume_ok: bool = True,
    ) -> None:
        self.events = events
        self.assign_ok = assign_ok
        self.membership_ok = membership_ok
        self.accounting_ok = accounting_ok
        self.resume_ok = resume_ok
        self._active_counts = [1, 1, 0]
        self.messages: list[tuple[int, int]] = [(6, 41)]

    def create_job(self) -> int:
        self.events.append("create-job")
        return 11

    def set_kill_on_close(self, _job: int) -> None:
        self.events.append("set-job-limits")

    def create_completion_port(self) -> int:
        self.events.append("create-port")
        return 12

    def attach_completion_port(self, _job: int, _port: int) -> None:
        self.events.append("attach-port")

    def job_messages(self, _port: int) -> tuple[tuple[int, int], ...]:
        messages, self.messages = self.messages, []
        return tuple(messages)

    def assign_process(self, _job: int, _process: int) -> None:
        self.events.append("assign")
        if not self.assign_ok:
            raise _Win32CallError(AdmissionStage.ASSIGN, 5)

    def is_process_in_job(self, _process: int, _job: int) -> bool:
        self.events.append("verify-membership")
        return self.membership_ok

    def active_processes(self, _job: int) -> int:
        self.events.append("query-accounting")
        if not self.accounting_ok:
            raise _Win32CallError(AdmissionStage.VERIFY, 6)
        return self._active_counts.pop(0) if self._active_counts else 0

    def total_processes(self, _job: int) -> int:
        return 1

    def member_process_ids(self, _job: int) -> tuple[int, ...]:
        self.events.append("list-members")
        return (41,)

    def open_job_member(self, _job: int, _pid: int) -> int | None:
        raise AssertionError("fake root must use its retained process handle")

    def resume_thread(self, _thread: int) -> int:
        self.events.append("resume-thread")
        if not self.resume_ok:
            raise _Win32CallError(AdmissionStage.RESUME, 7)
        return 1

    def terminate_job(self, _job: int) -> None:
        self.events.append("terminate-job")

    def terminate_process(self, _process: int) -> None:
        self.events.append("terminate-process")

    def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
        self.events.append("wait-process")
        return True

    def exit_code(self, _process: int) -> int | None:
        return 0

    def close_handle(self, handle: int) -> None:
        self.events.append(f"close:{handle}")


@pytest.mark.asyncio
async def test_zero_accounting_waits_for_exact_descendant_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsignaled Job member cannot be reported drained after accounting reaches zero."""

    class DelayedExitApi(_FakeApi):
        child_exited = False

        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            self.events.append("list-members")
            return (41, 42) if not self.child_exited else ()

        def open_job_member(self, _job: int, pid: int) -> int | None:
            self.events.append(f"open-member:{pid}")
            return 22 if pid == 42 else 21

        def wait_for_process(self, process: int, _timeout_ms: int) -> bool:
            self.events.append(f"wait:{process}")
            return process != 22 or self.child_exited

    events: list[str] = []
    api = DelayedExitApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [1, 0, 0, 0]

    monkeypatch.setattr(windows_process_owner, "_ADMISSION_CLEANUP_TIMEOUT", 0.0)
    first = await owner.force_and_drain(timeout=0.0)
    assert first.status is DrainStatus.TIMED_OUT
    assert first.active_processes == 0
    assert "wait:22" in events
    still_active = await owner.aclose()
    assert still_active.status is DrainStatus.TIMED_OUT
    assert owner._job_handle == 11
    assert "close:22" not in events
    api.child_exited = True
    second = await owner.aclose()
    assert second.status is DrainStatus.DRAINED
    assert events.index("wait:22") < events.index("close:22")
    assert owner._close_reaper is not None
    await asyncio.wait_for(owner._close_reaper, timeout=1.0)


@pytest.mark.asyncio
async def test_graceful_zero_refuses_historical_unseen_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnseenApi(_FakeApi):
        def total_processes(self, _job: int) -> int:
            return 2

    events: list[str] = []
    api = UnseenApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [0]

    receipt = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)

    assert receipt.status is DrainStatus.FAILED
    assert receipt.forced is True
    closed = await owner.aclose()
    assert closed.status is DrainStatus.FAILED
    assert "close:11" not in events and "close:21" not in events


@pytest.mark.asyncio
async def test_member_born_during_force_gap_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GapApi(_FakeApi):
        forced = False

        def total_processes(self, _job: int) -> int:
            return 2 if self.forced else 1

        def terminate_job(self, job: int) -> None:
            self.forced = True
            super().terminate_job(job)

    events: list[str] = []
    api = GapApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 0]

    receipt = await owner.force_and_drain(timeout=0.0)

    assert receipt.status is DrainStatus.FAILED
    assert receipt.failure_stage is AdmissionStage.DRAIN
    retried = await owner.force_and_drain(timeout=0.0)
    assert retried.status is DrainStatus.FAILED
    assert events.count("terminate-job") == 2
    closed = await owner.aclose()
    assert closed.status is DrainStatus.FAILED
    assert "close:11" not in events and "close:21" not in events
    assert owner._close_reaper is not None


@pytest.mark.asyncio
async def test_short_lived_members_reconcile_without_reopening_historical_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HistoricalApi(_FakeApi):
        def total_processes(self, _job: int) -> int:
            return 3

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return ()

        def open_job_member(self, _job: int, _pid: int) -> int | None:
            raise AssertionError("retired PIDs must not be reopened")

    events: list[str] = []
    api = HistoricalApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.extend(((6, 42), (7, 42), (6, 43), (7, 43)))
    api._active_counts = [0]

    receipt = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)
    assert receipt.status is DrainStatus.DRAINED
    assert not receipt.forced
    assert "terminate-job" not in events
    assert (await owner.aclose()).status is DrainStatus.DRAINED


@pytest.mark.asyncio
async def test_high_churn_abnormal_exits_reconcile_every_birth_without_reopening_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HighChurnApi(_FakeApi):
        def total_processes(self, _job: int) -> int:
            return 736

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return ()

        def open_job_member(self, _job: int, _pid: int) -> int | None:
            raise AssertionError("short-lived historical PIDs must not be reopened")

    events: list[str] = []
    api = HighChurnApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((7, 41))
    for pid in range(1000, 1524):
        api.messages.extend(((6, pid), (7, pid)))
    for pid in range(2000, 2202):
        api.messages.extend(((6, pid), (8, pid)))
    for pid in range(2000, 2009):
        api.messages.extend(((6, pid), (8, pid)))
    api._active_counts = [0]

    receipt = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)

    assert receipt.status is DrainStatus.DRAINED, {
        "births": owner._birth_notifications,
        "live_without_handle": len(owner._live_births - owner._member_handles.keys()),
        "unverified_membership": owner._unverified_membership,
    }
    assert not receipt.forced
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert "terminate-job" not in events


@pytest.mark.parametrize("exit_message", (7, 8))
@pytest.mark.asyncio
async def test_reused_pid_counts_both_job_births_after_first_retirement(
    monkeypatch: pytest.MonkeyPatch, exit_message: int
) -> None:
    class ReusedPidApi(_FakeApi):
        snapshots = 0

        def total_processes(self, _job: int) -> int:
            return 3

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            self.snapshots += 1
            if self.snapshots == 1:
                return (41, 42)
            if self.snapshots == 2:
                self.messages.extend(((exit_message, 42), (6, 42), (exit_message, 42)))
            return ()

        def open_job_member(self, _job: int, pid: int) -> int | None:
            assert pid == 42
            return 22

    events: list[str] = []
    api = ReusedPidApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [1, 0]

    receipt = await owner.drain_after_grace(grace_timeout=0.1, force_timeout=0.0)

    assert receipt.status is DrainStatus.DRAINED
    assert not receipt.forced
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert events.count("close:22") == 1
    assert "terminate-job" not in events


@pytest.mark.parametrize("exit_message", (7, 8))
@pytest.mark.parametrize("inject_at", ("snapshot", "open"))
@pytest.mark.asyncio
async def test_recycled_pid_notifications_arrive_between_snapshot_and_first_open(
    monkeypatch: pytest.MonkeyPatch, inject_at: str, exit_message: int
) -> None:
    class LateRecycledApi(_FakeApi):
        forced = False
        notified = False
        opens = 0

        def total_processes(self, _job: int) -> int:
            return 3

        def job_messages(self, port: int) -> tuple[tuple[int, int], ...]:
            messages = super().job_messages(port)
            if (exit_message, 42) in messages and (6, 42) in messages:
                self.events.append("observe:B")
            return messages

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            if inject_at == "snapshot" and not self.notified:
                self.notified = True
                self.messages.extend(((exit_message, 42), (6, 42)))
            return (41, 42) if not self.forced else ()

        def open_job_member(self, _job: int, pid: int) -> int | None:
            assert pid == 42
            self.events.append("open:B")
            self.opens += 1
            if inject_at == "open" and not self.notified:
                self.notified = True
                self.messages.extend(((exit_message, 42), (6, 42)))
            return 22 + self.opens

        def wait_for_process(self, handle: int, _timeout_ms: int) -> bool:
            if handle in (23, 24):
                self.events.append("wait:B")
                return self.forced
            return True

        def terminate_job(self, job: int) -> None:
            self.forced = True
            self.messages.append((exit_message, 42))
            super().terminate_job(job)

    events: list[str] = []
    api = LateRecycledApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [1, 0]

    receipt = await owner.drain_after_grace(grace_timeout=0.02, force_timeout=0.02)

    assert receipt.status is DrainStatus.DRAINED
    assert receipt.forced
    assert (events.index("observe:B") < events.index("open:B")) == (inject_at == "snapshot")
    assert api.opens == (1 if inject_at == "snapshot" else 2)
    assert events.index("wait:B") < events.index("terminate-job")
    assert not owner._unverified_membership
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert events.count("close:23") == 1
    assert events.count("close:24") == (inject_at == "open")


@pytest.mark.parametrize("exit_message", (7, 8))
@pytest.mark.asyncio
async def test_reused_live_pid_replaces_retired_handle_before_claiming_drain(
    monkeypatch: pytest.MonkeyPatch, exit_message: int
) -> None:
    class ReusedLiveApi(_FakeApi):
        recycled = False
        opens = 0
        second_exited = False

        def total_processes(self, _job: int) -> int:
            return 3

        def job_messages(self, port: int) -> tuple[tuple[int, int], ...]:
            if self.opens == 1 and not self.recycled:
                self.recycled = True
                self.messages.extend(((exit_message, 42), (6, 42)))
            return super().job_messages(port)

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return () if self.second_exited else (41, 42)

        def open_job_member(self, _job: int, pid: int) -> int | None:
            assert pid == 42
            self.opens += 1
            return 21 + self.opens

        def wait_for_process(self, handle: int, _timeout_ms: int) -> bool:
            self.events.append(f"wait:{handle}")
            return handle != 23 or self.second_exited

        def terminate_job(self, job: int) -> None:
            self.second_exited = True
            self.messages.append((exit_message, 42))
            super().terminate_job(job)

    events: list[str] = []
    api = ReusedLiveApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [1, 0]

    receipt = await owner.drain_after_grace(grace_timeout=0.02, force_timeout=0.02)

    assert receipt.status is DrainStatus.DRAINED
    assert receipt.forced
    assert "wait:23" in events
    assert api.opens == 2
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert events.count("close:22") == events.count("close:23") == 1


@pytest.mark.asyncio
async def test_duplicate_live_birth_refuses_drain_despite_matching_job_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AmbiguousApi(_FakeApi):
        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return ()

    events: list[str] = []
    api = AmbiguousApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.extend(((6, 42), (6, 42), (7, 42)))
    api._active_counts = [0]

    receipt = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)

    assert receipt.status is DrainStatus.FAILED
    assert receipt.failure_stage is AdmissionStage.DRAIN
    assert "close:11" not in events


@pytest.mark.asyncio
async def test_open_member_denial_never_prevents_job_force_or_same_owner_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeniedApi(_FakeApi):
        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return (41, 42) if not self.messages_exited else ()

        messages_exited = False

        def open_job_member(self, _job: int, pid: int) -> int | None:
            assert pid == 42
            raise _Win32CallError(AdmissionStage.DRAIN, 5)

        def terminate_job(self, job: int) -> None:
            self.messages_exited = True
            self.messages.extend(((6, 42), (7, 42)))
            super().terminate_job(job)

    events: list[str] = []
    api = DeniedApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [0]

    receipt = await owner.force_and_drain(timeout=0.0)
    assert receipt.status is DrainStatus.DRAINED
    assert receipt.forced
    assert events.count("terminate-job") == 1
    assert (await owner.aclose()).status is DrainStatus.DRAINED


@pytest.mark.asyncio
async def test_force_waits_through_denied_open_until_child_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedNotificationApi(_FakeApi):
        forced = False
        observations = 0

        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return (41, 42) if self.observations < 3 else ()

        def open_job_member(self, _job: int, _pid: int) -> int | None:
            raise _Win32CallError(AdmissionStage.DRAIN, 5)

        def terminate_job(self, job: int) -> None:
            self.forced = True
            super().terminate_job(job)

        def job_messages(self, port: int) -> tuple[tuple[int, int], ...]:
            if self.forced:
                self.observations += 1
                if self.observations == 3:
                    self.messages.extend(((6, 42), (7, 42)))
            return super().job_messages(port)

    events: list[str] = []
    api = DelayedNotificationApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 1, 0]
    receipt = await owner.force_and_drain(timeout=0.1)
    assert receipt.status is DrainStatus.DRAINED
    assert receipt.forced and api.observations >= 3
    assert (await owner.aclose()).status is DrainStatus.DRAINED


@pytest.mark.asyncio
async def test_concurrent_close_releases_each_controlling_handle_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    api = _FakeApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [0]

    first, second = await asyncio.gather(owner.aclose(), owner.aclose())

    assert first is second and first.status is DrainStatus.DRAINED
    assert events.count("close:11") == 1
    assert events.count("close:12") == 1
    assert events.count("close:21") == 1


@pytest.mark.asyncio
async def test_repeated_cancelled_close_retains_worker_until_descendant_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PendingApi(_FakeApi):
        exited = False

        def __init__(self, events: list[str]) -> None:
            super().__init__(events)
            self.forced = asyncio.Event()

        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return (41, 42) if not self.exited else ()

        def open_job_member(self, _job: int, pid: int) -> int | None:
            assert pid == 42
            return 22

        def wait_for_process(self, process: int, _timeout_ms: int) -> bool:
            return process != 22 or self.exited

        def terminate_job(self, job: int) -> None:
            super().terminate_job(job)
            self.forced.set()

    events: list[str] = []
    api = PendingApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [0]
    monkeypatch.setattr(windows_process_owner, "_ADMISSION_CLEANUP_TIMEOUT", 0.02)
    monkeypatch.setattr(
        windows_process_owner, "_FAILED_ADMISSION_REAPER_INITIAL_BACKOFF_SECONDS", 0.01
    )

    for _ in range(2):
        caller = asyncio.create_task(owner.aclose())
        await asyncio.wait_for(api.forced.wait(), timeout=1.0)
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    assert "close:22" not in events
    await asyncio.sleep(0.05)
    api.exited = True
    api.messages.append((7, 42))
    for _ in range(100):
        if "close:11" in events:
            break
        await asyncio.sleep(0.01)

    assert "close:11" in events
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert events.count("close:22") == 1
    assert events.count("close:11") == 1
    assert events.count("close:12") == 1
    assert events.count("close:21") == 1


@pytest.mark.asyncio
async def test_zero_waits_for_delayed_retirement_notification_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedZeroApi(_FakeApi):
        forced = False
        observations = 0

        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return ()

        def terminate_job(self, job: int) -> None:
            self.forced = True
            super().terminate_job(job)

        def job_messages(self, port: int) -> tuple[tuple[int, int], ...]:
            if self.forced:
                self.observations += 1
                if self.observations == 3:
                    self.messages.extend(((6, 42), (7, 42)))
            return super().job_messages(port)

    events: list[str] = []
    api = DelayedZeroApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 0]
    receipt = await owner.force_and_drain(timeout=0.1)
    assert receipt.status is DrainStatus.DRAINED
    assert api.observations >= 3
    assert (await owner.aclose()).status is DrainStatus.DRAINED


@pytest.mark.asyncio
async def test_forced_zero_waits_for_async_child_termination_and_keeps_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PendingApi(_FakeApi):
        exited = False

        def total_processes(self, _job: int) -> int:
            return 2

        def member_process_ids(self, _job: int) -> tuple[int, ...]:
            return (41, 42) if not self.exited else ()

        def open_job_member(self, _job: int, _pid: int) -> int | None:
            return 22

        def wait_for_process(self, process: int, _timeout_ms: int) -> bool:
            return process != 22 or self.exited

    events: list[str] = []
    api = PendingApi(events)
    owner = await _launch(monkeypatch, api, events)
    api.messages.append((6, 42))
    api._active_counts = [1, 0, 0]
    first = await owner.force_and_drain(timeout=0.0)
    assert first.status is DrainStatus.TIMED_OUT
    monkeypatch.setattr(windows_process_owner, "_ADMISSION_CLEANUP_TIMEOUT", 0.0)
    assert (await owner.aclose()).status is DrainStatus.TIMED_OUT
    assert owner._job_handle == 11
    api.exited = True
    api.messages.append((7, 42))
    recovered = await owner.force_and_drain(timeout=0.0)
    assert recovered.status is DrainStatus.DRAINED
    assert recovered.owner == first.owner
    assert (await owner.aclose()).status is DrainStatus.DRAINED


class _FakePipes:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close_child_ends(self, _api: _FakeApi) -> None:
        self.events.append("close-child-ends")

    def close_unwired(self, _api: _FakeApi) -> None:
        self.events.append("close-unwired")

    async def wire(
        self,
        _loop: asyncio.AbstractEventLoop,
    ) -> tuple[None, asyncio.StreamReader, asyncio.StreamReader, tuple[Any, ...]]:
        self.events.append("wire-io")
        return None, asyncio.StreamReader(), asyncio.StreamReader(), ()


def _creator(events: list[str]):
    def create(
        *,
        argv: Sequence[str],
        cwd: str | None,
        env: dict[str, str] | None,
        pipe_ends: _FakePipes,
    ) -> tuple[int, int, int]:
        assert argv == ("fixture.exe", "--interpreter=vscode")
        assert cwd is None
        assert env is None
        assert isinstance(pipe_ends, _FakePipes)
        events.append("create-suspended")
        return 21, 31, 41

    return create


async def _launch(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeApi,
    events: list[str],
) -> WindowsOwnedProcess:
    monkeypatch.setattr(os, "set_handle_inheritable", lambda *_args: None, raising=False)
    return await WindowsOwnedProcess._launch_with(
        generation="owner-generation",
        argv=("fixture.exe", "--interpreter=vscode"),
        cwd=None,
        env=None,
        stdin_mode="pipe",
        api=api,
        pipe_ends=_FakePipes(events),
        process_creator=_creator(events),
    )


@pytest.mark.asyncio
async def test_owner_drain_snapshot_preserves_closed_receipt_without_private_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    api = _FakeApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [0]
    receipt = await owner.drain_after_grace(grace_timeout=0.1, force_timeout=0.1)
    before = owner.drain_snapshot(receipt)
    assert before["status"] == "drained"
    assert before["retained_exact_handles"] == before["signaled_exact_handles"] == 1
    assert before["total_processes"] == before["birth_notifications"] == 1
    assert set(before) == {
        "status",
        "forced",
        "root_was_forced",
        "active_processes",
        "total_processes",
        "birth_notifications",
        "exit_notifications",
        "unverified_membership",
        "root_birth_seen",
        "live_members_without_handle",
        "retained_exact_handles",
        "signaled_exact_handles",
        "handle_probe_failed",
        "failure_stage",
        "winerror",
    }
    await owner.aclose()
    assert owner.drain_snapshot(receipt) == before
    json.dumps(before)


@pytest.mark.asyncio
async def test_admission_orders_private_job_before_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    owner = await _launch(monkeypatch, _FakeApi(events), events)

    required_order = [
        "create-job",
        "set-job-limits",
        "create-port",
        "attach-port",
        "create-suspended",
        "assign",
        "verify-membership",
        "query-accounting",
        "wire-io",
        "resume-thread",
    ]
    assert [event for event in events if event in required_order] == required_order
    assert owner.owner.generation == "owner-generation"
    assert owner.owner.root_pid == 41

    receipt = await owner.force_and_drain(timeout=0.1)
    assert receipt.status is DrainStatus.DRAINED
    assert receipt.active_processes == 0
    assert receipt.forced is True
    await owner.aclose()


@pytest.mark.asyncio
async def test_forced_job_drain_records_an_already_exited_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced descendant drain must retain the root's prior natural exit fact."""

    events: list[str] = []
    api = _FakeApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 0]

    try:
        receipt = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.1)

        assert receipt.status is DrainStatus.DRAINED
        assert receipt.forced is True
        assert receipt.root_returncode == 0
        assert receipt.root_was_forced is False
        assert events.count("terminate-job") == 1
    finally:
        await owner.aclose()


def test_exit_code_distinguishes_a_signaled_259_from_still_active() -> None:
    """WAIT_OBJECT_0 makes a terminated root's literal 259 observable."""

    wait = MagicMock(return_value=0)

    def get_exit_code(_handle: int, value: Any) -> bool:
        value._obj.value = 259
        return True

    kernel32 = _Kernel32.__new__(_Kernel32)
    kernel32._wait_for_single_object = wait
    kernel32._get_exit_code = MagicMock(side_effect=get_exit_code)
    kernel32._wintypes = ctypes.wintypes
    kernel32._ctypes = ctypes

    assert kernel32.exit_code(41) == 259
    wait.assert_called_once_with(41, 0)


def test_exit_code_returns_none_without_reading_a_live_process_exit_code() -> None:
    """WAIT_TIMEOUT identifies the live sentinel before GetExitCodeProcess."""

    wait = MagicMock(return_value=258)
    get_exit_code = MagicMock()
    kernel32 = _Kernel32.__new__(_Kernel32)
    kernel32._wait_for_single_object = wait
    kernel32._get_exit_code = get_exit_code
    kernel32._wintypes = ctypes.wintypes
    kernel32._ctypes = ctypes

    assert kernel32.exit_code(41) is None
    wait.assert_called_once_with(41, 0)
    get_exit_code.assert_not_called()


def test_exit_code_fails_closed_when_liveness_probe_fails() -> None:
    """A failed zero-time wait never turns into an exit-code observation."""

    wait = MagicMock(return_value=0xFFFFFFFF)
    get_exit_code = MagicMock()
    failure = _Win32CallError(AdmissionStage.DRAIN, 5)
    kernel32 = _Kernel32.__new__(_Kernel32)
    kernel32._wait_for_single_object = wait
    kernel32._get_exit_code = get_exit_code
    kernel32._error = MagicMock(return_value=failure)

    with pytest.raises(_Win32CallError) as raised:
        kernel32.exit_code(41)

    assert raised.value is failure
    wait.assert_called_once_with(41, 0)
    get_exit_code.assert_not_called()


def test_job_member_list_grows_before_claiming_complete_snapshot() -> None:
    class MemberList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", ctypes.c_uint32),
            ("NumberOfProcessIdsInList", ctypes.c_uint32),
            ("ProcessIdList", ctypes.c_size_t * 1),
        ]

    capacities: list[int] = []

    def query(_job: int, _kind: int, buffer: Any, length: int, _returned: Any) -> bool:
        capacity = (length - MemberList.ProcessIdList.offset) // ctypes.sizeof(ctypes.c_size_t)
        capacities.append(capacity)
        info = ctypes.cast(buffer, ctypes.POINTER(MemberList)).contents
        info.NumberOfAssignedProcesses = 9
        info.NumberOfProcessIdsInList = min(capacity, 9)
        values = (ctypes.c_size_t * info.NumberOfProcessIdsInList).from_buffer(
            buffer, MemberList.ProcessIdList.offset
        )
        for index in range(len(values)):
            values[index] = 200 + index
        return True

    kernel32 = _Kernel32.__new__(_Kernel32)
    kernel32._ctypes = ctypes
    kernel32._basic_process_id_list = MemberList
    kernel32._query_information = query

    assert kernel32.member_process_ids(11) == tuple(range(200, 209))
    assert capacities == [8, 16]


def test_recycled_nonmember_handle_is_never_accepted_as_job_member() -> None:
    kernel32 = _Kernel32.__new__(_Kernel32)
    kernel32._open_process = MagicMock(return_value=22)
    kernel32.is_process_in_job = MagicMock(return_value=False)
    kernel32.wait_for_process = MagicMock(return_value=False)
    kernel32.close_handle = MagicMock()

    with pytest.raises(_Win32CallError) as raised:
        kernel32.open_job_member(11, 42)

    assert raised.value.stage is AdmissionStage.DRAIN
    kernel32._open_process.assert_called_once_with(0x101000, False, 42)
    kernel32.close_handle.assert_called_once_with(22)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_kwargs", "stage"),
    [
        ({"assign_ok": False}, AdmissionStage.ASSIGN),
        ({"membership_ok": False}, AdmissionStage.VERIFY),
        ({"accounting_ok": False}, AdmissionStage.VERIFY),
    ],
)
async def test_pre_resume_admission_failure_never_resumes_child(
    monkeypatch: pytest.MonkeyPatch,
    api_kwargs: dict[str, bool],
    stage: AdmissionStage,
) -> None:
    events: list[str] = []
    api = _FakeApi(events, **api_kwargs)

    with pytest.raises(ProcessAdmissionError) as raised:
        await _launch(monkeypatch, api, events)

    assert raised.value.stage is stage
    assert "resume-thread" not in events
    assert "terminate-process" in events
    assert "close:21" in events
    assert "close:31" in events
    assert "close:11" in events
    if stage is AdmissionStage.VERIFY:
        assert "terminate-job" in events


@pytest.mark.asyncio
async def test_resume_failure_terminates_admitted_job_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    api = _FakeApi(events, resume_ok=False)
    with pytest.raises(ProcessAdmissionError) as raised:
        await _launch(monkeypatch, api, events)

    assert raised.value.stage is AdmissionStage.RESUME
    assert events.count("resume-thread") == 1
    assert "terminate-job" in events
    assert "terminate-process" in events
    assert {"close:11", "close:21", "close:31"}.issubset(events)


@pytest.mark.asyncio
async def test_pre_admission_terminate_failure_retains_controlling_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed root termination cannot release the only suspended-root handles."""

    class RootTerminateFailureApi(_FakeApi):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events, assign_ok=False)
            self.terminate_attempts = 0
            self.wait_attempts = 0

        def terminate_process(self, _process: int) -> None:
            self.events.append("terminate-process")
            self.terminate_attempts += 1
            if self.terminate_attempts == 1:
                raise _Win32CallError(AdmissionStage.DRAIN, 55)

        def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
            self.events.append("wait-process")
            self.wait_attempts += 1
            return self.wait_attempts >= 2

    events: list[str] = []
    api = RootTerminateFailureApi(events)

    with pytest.raises(AdmissionCleanupError) as raised:
        await _launch(monkeypatch, api, events)

    failure = raised.value
    assert failure.admission_stage is AdmissionStage.ASSIGN
    assert failure.cleanup_stage is AdmissionStage.DRAIN
    assert failure.cleanup_winerror == 55
    assert "wait-process" in events
    assert {"close:11", "close:21", "close:31"}.isdisjoint(events)
    assert await failure.wait_for_cleanup(timeout=1.0) is True
    assert {"close:11", "close:21", "close:31"}.issubset(events)


@pytest.mark.asyncio
async def test_pre_admission_terminate_access_denied_closes_already_exited_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERROR_ACCESS_DENIED still probes the retained handle for prior exit."""

    class AlreadyExitedApi(_FakeApi):
        def terminate_process(self, _process: int) -> None:
            self.events.append("terminate-process")
            raise _Win32CallError(AdmissionStage.DRAIN, 5)

        def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
            self.events.append("wait-process")
            return True

    events: list[str] = []
    api = AlreadyExitedApi(events, assign_ok=False)

    with pytest.raises(ProcessAdmissionError) as raised:
        await _launch(monkeypatch, api, events)

    assert raised.value.stage is AdmissionStage.ASSIGN
    assert events.index("terminate-process") < events.index("wait-process")
    assert events.index("wait-process") < events.index("close:31")
    assert {"close:11", "close:21", "close:31"}.issubset(events)


@pytest.mark.asyncio
async def test_pre_admission_wait_timeout_retains_controlling_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded wait timeout is not evidence that the suspended root exited."""

    class WaitTimeoutApi(_FakeApi):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events, assign_ok=False)
            self.wait_attempts = 0

        def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
            self.events.append("wait-process")
            self.wait_attempts += 1
            return self.wait_attempts >= 2

    events: list[str] = []
    api = WaitTimeoutApi(events)

    with pytest.raises(AdmissionCleanupError) as raised:
        await _launch(monkeypatch, api, events)

    failure = raised.value
    assert failure.admission_stage is AdmissionStage.ASSIGN
    assert failure.cleanup_stage is AdmissionStage.DRAIN
    assert failure.cleanup_winerror is None
    assert events.index("terminate-process") < events.index("wait-process")
    assert {"close:11", "close:21", "close:31"}.isdisjoint(events)
    assert await failure.wait_for_cleanup(timeout=1.0) is True
    assert {"close:11", "close:21", "close:31"}.issubset(events)


@pytest.mark.asyncio
async def test_failed_admission_reaper_retries_until_root_exit_then_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first cleanup retains one retry owner until its root exits."""

    class RetryCleanupApi(_FakeApi):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events, assign_ok=False)
            self.terminate_attempts = 0
            self.wait_attempts = 0

        def terminate_process(self, _process: int) -> None:
            self.events.append("terminate-process")
            self.terminate_attempts += 1
            if self.terminate_attempts == 1:
                raise _Win32CallError(AdmissionStage.DRAIN, 55)

        def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
            self.events.append("wait-process")
            self.wait_attempts += 1
            return self.wait_attempts >= 2

    events: list[str] = []
    api = RetryCleanupApi(events)

    with pytest.raises(AdmissionCleanupError) as raised:
        await _launch(monkeypatch, api, events)

    failure = raised.value
    assert failure.controlling_handles_retained is True
    assert await failure.wait_for_cleanup(timeout=1.0) is True
    assert api.terminate_attempts >= 2
    assert api.wait_attempts >= 2
    assert {"close:11", "close:21", "close:31"}.issubset(events)
    assert len(events) - 1 - events[::-1].index("wait-process") < events.index("close:31")


@pytest.mark.asyncio
async def test_failed_admission_reaper_backs_off_and_logs_failures(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Retained cleanup slows repeated errors without hiding their cause."""

    class RetryApi(_FakeApi):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events, assign_ok=False)
            self.wait_attempts = 0

        def wait_for_process(self, _process: int, _timeout_ms: int) -> bool:
            self.events.append("wait-process")
            self.wait_attempts += 1
            if self.wait_attempts == 1:
                raise RuntimeError("wait observation failed")
            return self.wait_attempts >= 5

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(
        windows_process_owner,
        "_FAILED_ADMISSION_REAPER_INITIAL_BACKOFF_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        windows_process_owner,
        "_FAILED_ADMISSION_REAPER_MAX_BACKOFF_SECONDS",
        0.04,
    )
    monkeypatch.setattr(windows_process_owner.asyncio, "sleep", record_sleep)
    events: list[str] = []
    reaper = _FailedAdmissionReaper(
        api=RetryApi(events),
        job_handle=11,
        port_handle=None,
        process_handle=21,
        thread_handle=31,
        pipe_ends=None,
        transports=(),
        admitted=False,
    )

    with caplog.at_level(logging.WARNING, logger=windows_process_owner.__name__):
        reaper.schedule()
        assert await reaper.wait_for_completion(timeout=1.0) is True

    messages = [record.getMessage() for record in caplog.records]
    assert delays == pytest.approx([0.01, 0.02, 0.04, 0.04, 0.04])
    assert "Failed-admission reaper cleanup retry raised" in messages
    assert (
        messages.count("Failed-admission reaper cleanup retry failed: stage=drain winerror=None")
        == 3
    )
    assert {"close:11", "close:21", "close:31"}.issubset(events)


@pytest.mark.asyncio
async def test_admitted_job_fallback_confirms_root_before_releasing_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted Job may recover root termination only after its root exit is observed."""

    class RootTerminateFailureApi(_FakeApi):
        def terminate_process(self, _process: int) -> None:
            self.events.append("terminate-process")
            raise _Win32CallError(AdmissionStage.DRAIN, 55)

    events: list[str] = []
    api = RootTerminateFailureApi(events, resume_ok=False)

    with pytest.raises(ProcessAdmissionError) as raised:
        await _launch(monkeypatch, api, events)

    assert not isinstance(raised.value, AdmissionCleanupError)
    assert raised.value.stage is AdmissionStage.RESUME
    assert events.index("terminate-process") < events.index("terminate-job")
    assert events.index("terminate-job") < events.index("wait-process")
    assert events.index("wait-process") < events.index("close:31")
    assert {"close:11", "close:21", "close:31"}.issubset(events)


def _prepare_fake_create_process(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    created = MagicMock(return_value=(21, 31, 41, 51))
    monkeypatch.setitem(sys.modules, "_winapi", SimpleNamespace(CreateProcess=created))
    monkeypatch.setattr(
        subprocess,
        "STARTUPINFO",
        lambda: SimpleNamespace(dwFlags=0),
        raising=False,
    )
    monkeypatch.setattr(subprocess, "STARTF_USESTDHANDLES", 1, raising=False)
    return created


def _fake_pipe_ends() -> SimpleNamespace:
    return SimpleNamespace(
        stdin_child=11,
        stdout_child=12,
        stderr_child=13,
        handle_list=lambda: (11, 12, 13),
    )


def test_bare_executable_uses_child_path_for_create_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bare command must resolve through the supplied child PATH."""

    created = _prepare_fake_create_process(monkeypatch)
    resolved = str(tmp_path / "toolchain" / "dotnet.exe")
    which = MagicMock(return_value=resolved)
    monkeypatch.setattr(shutil, "which", which)
    child_path = str(tmp_path / "toolchain")

    _create_suspended_process(
        argv=("dotnet", "build"),
        cwd=str(tmp_path),
        env={"Path": child_path},
        pipe_ends=_fake_pipe_ends(),
    )

    assert created.call_args.args[0] == resolved
    which.assert_called_once_with("dotnet", path=child_path)


def test_unresolvable_bare_executable_fails_before_create_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit application name is never guessed from the current directory."""

    created = _prepare_fake_create_process(monkeypatch)
    monkeypatch.setattr(shutil, "which", MagicMock(return_value=None))

    pipe_ends = _fake_pipe_ends()
    with pytest.raises(_Win32CallError) as raised:
        _create_suspended_process(
            argv=("dotnet", "build"),
            cwd=str(tmp_path),
            env={"Path": str(tmp_path / "toolchain")},
            pipe_ends=pipe_ends,
        )

    assert raised.value.stage is AdmissionStage.CREATE_PROCESS
    created.assert_not_called()


def test_partial_pipe_allocation_closes_every_created_raw_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later pipe allocation failure releases every earlier raw pipe handle."""

    import netcoredbg_mcp.windows_process_owner as owner_module

    outcomes: list[tuple[int, int] | OSError] = [
        (11, 12),
        (21, 22),
        OSError("stderr pipe allocation failed"),
    ]
    closed: list[int] = []

    def pipe(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome

    monkeypatch.setattr(asyncio, "windows_utils", SimpleNamespace(pipe=pipe), raising=False)
    monkeypatch.setattr(os, "set_handle_inheritable", lambda *_args: None, raising=False)
    monkeypatch.setattr(owner_module, "_close_raw_handle", closed.append, raising=False)

    with pytest.raises(OSError, match="stderr pipe allocation failed"):
        owner_module._PipeEnds.create("pipe")

    assert sorted(closed) == [11, 12, 21, 22]


def test_pipe_inheritability_failure_closes_every_created_raw_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handle-attribute failure releases all pipe ends before admission owns them."""

    import netcoredbg_mcp.windows_process_owner as owner_module

    outcomes = iter(((11, 12), (21, 22), (31, 32)))
    closed: list[int] = []

    def set_handle_inheritable(handle: int, _inheritable: bool) -> None:
        if handle == 22:
            raise OSError("stdout handle inheritance failed")

    monkeypatch.setattr(
        asyncio,
        "windows_utils",
        SimpleNamespace(pipe=lambda *_args, **_kwargs: next(outcomes)),
        raising=False,
    )
    monkeypatch.setattr(os, "set_handle_inheritable", set_handle_inheritable, raising=False)
    monkeypatch.setattr(owner_module, "_close_raw_handle", closed.append, raising=False)

    with pytest.raises(OSError, match="stdout handle inheritance failed"):
        owner_module._PipeEnds.create("pipe")

    assert sorted(closed) == [11, 12, 21, 22, 31, 32]


@pytest.mark.asyncio
async def test_non_drained_receipt_allows_later_force_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out drain cannot prevent a later explicit force attempt."""

    events: list[str] = []
    api = _FakeApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 1, 1, 0]

    timed_out = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)
    drained = await owner.force_and_drain(timeout=0.1)

    assert timed_out.status is DrainStatus.TIMED_OUT
    assert drained.status is DrainStatus.DRAINED
    assert drained.active_processes == 0
    assert events.count("terminate-job") == 2


@pytest.mark.asyncio
async def test_aclose_retries_non_drained_receipt_with_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing an owner cannot treat a timed-out receipt as cleanup evidence."""

    events: list[str] = []
    api = _FakeApi(events)
    owner = await _launch(monkeypatch, api, events)
    api._active_counts = [1, 1, 1, 0]

    timed_out = await owner.drain_after_grace(grace_timeout=0.0, force_timeout=0.0)
    await owner.aclose()

    assert timed_out.status is DrainStatus.TIMED_OUT
    assert owner._drain_receipt is not None
    assert owner._drain_receipt.status is DrainStatus.DRAINED
    assert owner._drain_receipt.active_processes == 0
    assert events.count("terminate-job") == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object proof")
@pytest.mark.skipif(shutil.which("dotnet") is None, reason="dotnet CLI is required")
@pytest.mark.asyncio
async def test_production_dap_path_inherits_descendant_and_drains_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real DAP launch path wires I/O and drains an inherited descendant."""

    build = subprocess.run(
        ["dotnet", "build", str(FIXTURE_PROJECT), "-c", "Debug", "-v", "quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert FIXTURE_EXE.is_file(), f"fixture build did not produce {FIXTURE_EXE}"

    root_marker = tmp_path / "root.json"
    child_marker = tmp_path / "child.json"
    monkeypatch.setenv("OWNER_SCOPE_ROOT_MARKER", str(root_marker))
    monkeypatch.setenv("OWNER_SCOPE_CHILD_MARKER", str(child_marker))

    output_seen = asyncio.Event()
    terminal_seen = asyncio.Event()
    terminals: list[DapTransportTerminal] = []
    client = DAPClient(str(FIXTURE_EXE))
    client.on_event(
        "output",
        lambda event: output_seen.set()
        if event.body.get("output") == "owner-scope-ready"
        else None,
    )

    def record_terminal(terminal: DapTransportTerminal) -> None:
        terminals.append(terminal)
        terminal_seen.set()

    client.set_transport_terminal_handler(record_terminal)
    child_pid: int | None = None
    try:
        await client.start(generation="real-owner-fixture")
        await asyncio.wait_for(output_seen.wait(), timeout=10.0)
        await _wait_for_path(root_marker)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)

        run = client._run
        assert run is not None and run.owner is not None
        assert run.owner._job_handle is not None
        assert _child_is_in_job(child_pid, run.owner._job_handle) is True

        await client.stop()
        await asyncio.wait_for(terminal_seen.wait(), timeout=10.0)

        receipt = run.owner_drain_receipt
        assert receipt is not None
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.active_processes == 0
        assert b"owner-scope-stderr-ready" in terminals[0].stderr_tail
        await _wait_for_pid_exit(child_pid)
    finally:
        if client.is_running:
            await client.stop()
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object proof")
@pytest.mark.skipif(shutil.which("dotnet") is None, reason="dotnet CLI is required")
@pytest.mark.asyncio
async def test_real_exit_259_is_natural_when_the_job_forces_its_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A natural root exit code of 259 is not the live-process sentinel."""

    fixture_output = tmp_path / "fixture"
    fixture_exe = fixture_output / "OwnerScopeAdapter.exe"
    build = subprocess.run(
        [
            "dotnet",
            "build",
            str(FIXTURE_PROJECT),
            "-c",
            "Debug",
            "-v",
            "quiet",
            "-o",
            str(fixture_output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert fixture_exe.is_file(), f"fixture build did not produce {fixture_exe}"

    root_marker = tmp_path / "root.json"
    child_marker = tmp_path / "child.json"
    monkeypatch.setenv("OWNER_SCOPE_ROOT_MARKER", str(root_marker))
    monkeypatch.setenv("OWNER_SCOPE_CHILD_MARKER", str(child_marker))
    monkeypatch.setenv("OWNER_SCOPE_ROOT_EXIT_CODE", "259")

    output_seen = asyncio.Event()
    terminal_seen = asyncio.Event()
    terminals: list[DapTransportTerminal] = []
    client = DAPClient(str(fixture_exe))
    client.on_event(
        "output",
        lambda event: output_seen.set()
        if event.body.get("output") == "owner-scope-ready"
        else None,
    )
    client.set_transport_terminal_handler(
        lambda terminal: (terminals.append(terminal), terminal_seen.set())
    )
    child_pid: int | None = None
    try:
        await client.start(generation="real-owner-exit-259")
        run = client._run
        assert run is not None and run.owner is not None
        await asyncio.wait_for(output_seen.wait(), timeout=10.0)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)

        await asyncio.wait_for(terminal_seen.wait(), timeout=10.0)

        receipt = run.owner_drain_receipt
        assert receipt is not None
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.active_processes == 0
        assert receipt.forced is True
        assert receipt.root_returncode == 259
        assert receipt.root_was_forced is False
        assert terminals[0].returncode == 259
        assert terminals[0].cleanup_outcome.value == "natural_exit"
        await _wait_for_pid_exit(child_pid)
    finally:
        if client.is_running:
            await client.stop()
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object proof")
@pytest.mark.skipif(shutil.which("dotnet") is None, reason="dotnet CLI is required")
@pytest.mark.asyncio
async def test_real_prebuild_drains_only_captured_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O10: production capture drains A while B and same-image sentinel survive."""

    build = subprocess.run(
        ["dotnet", "build", str(FIXTURE_PROJECT), "-c", "Debug", "-v", "quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    async def start_client(name: str) -> tuple[DAPClient, int]:
        root_marker = tmp_path / f"{name}-root.json"
        child_marker = tmp_path / f"{name}-child.json"
        monkeypatch.setenv("OWNER_SCOPE_ROOT_MARKER", str(root_marker))
        monkeypatch.setenv("OWNER_SCOPE_CHILD_MARKER", str(child_marker))
        client = DAPClient(str(FIXTURE_EXE))
        await client.start(generation=name)
        await _wait_for_path(root_marker)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)
        return client, child_pid

    client_a, child_a = await start_client("owner-a")
    client_b, child_b = await start_client("owner-b")

    sentinel_root_marker = tmp_path / "sentinel-root.json"
    sentinel_child_marker = tmp_path / "sentinel-child.json"
    sentinel_env = dict(os.environ)
    sentinel_env["OWNER_SCOPE_ROOT_MARKER"] = str(sentinel_root_marker)
    sentinel_env["OWNER_SCOPE_CHILD_MARKER"] = str(sentinel_child_marker)
    sentinel = subprocess.Popen(
        [str(FIXTURE_EXE), "--foreign-sentinel"],
        env=sentinel_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sentinel_child: int | None = None
    try:
        await _wait_for_path(sentinel_child_marker)
        sentinel_child = await _read_marker_pid(sentinel_child_marker)

        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager_a = SessionManager()
        manager_a._client = client_a
        manager_a._active_dap_run = "owner-a"
        captured = manager_a.capture_prebuild_owner()

        build_manager = BuildManager()
        project = tmp_path / "OwnerA.csproj"
        project.touch()
        build_session = build_manager.get_session(str(tmp_path))
        build_session.build = AsyncMock(return_value=MagicMock(success=True))

        result = await build_manager.pre_launch_build(
            str(tmp_path),
            str(project),
            owner=captured,
            restore_first=False,
        )

        assert result.success is True
        await _wait_for_pid_exit(child_a)
        assert client_b.is_running is True
        assert psutil.pid_exists(child_b) is True
        assert sentinel.poll() is None
        assert sentinel_child is not None and psutil.pid_exists(sentinel_child) is True
        build_session.build.assert_awaited_once()
    finally:
        if client_a.is_running:
            await client_a.stop()
        if client_b.is_running:
            await client_b.stop()
        sentinel.terminate()
        try:
            sentinel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sentinel.kill()
            sentinel.wait(timeout=5)
        for pid in (sentinel_child, child_b):
            if pid is not None and psutil.pid_exists(pid):
                psutil.Process(pid).kill()


def _native_python() -> tuple[str, dict[str, str]]:
    """Keep controlled process counts independent of Windows venv redirectors."""
    executable = Path(sys._base_executable).resolve()
    assert executable.is_file(), executable
    module_path = windows_process_owner.__file__
    assert module_path is not None
    environment = dict(os.environ)
    environment["PATH"] = str(executable.parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONPATH"] = str(Path(module_path).resolve().parents[1])
    return str(executable), environment


@pytest.mark.skipif(os.name != "nt", reason="Windows direct process capability proof")
@pytest.mark.asyncio
async def test_direct_capture_drains_nested_jobs_without_cooperation_despite_delayed_pump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    native_wait = _Kernel32.wait_debug_event

    def delayed_wait(api, timeout_ms):
        event = native_wait(api, timeout_ms)
        if event is not None and event.dwDebugEventCode == 3:
            time.sleep(0.03)
        return event

    monkeypatch.setattr(_Kernel32, "wait_debug_event", delayed_wait)
    python, environment = _native_python()
    member_code = f"""
import subprocess, time
children = [subprocess.Popen(
    [{python!r}, '-c', 'import time;time.sleep(30)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
) for _ in range(3)]
print('ready', flush=True)
time.sleep(30)
"""
    child_code = f"""
import asyncio
from netcoredbg_mcp.windows_process_owner import WindowsOwnedProcess, DrainStatus
async def run():
    for index in range(8):
        inner = await WindowsOwnedProcess.launch(
            generation=index, argv=({python!r}, '-c', {member_code!r}),
            cwd=None, env=None, stdin_mode='devnull',
        )
        try:
            assert (await asyncio.wait_for(inner.stdout.readline(), 10)).strip() == b'ready'
            receipt = await inner.force_and_drain(timeout=10)
            assert receipt.status is DrainStatus.DRAINED, inner.drain_snapshot(receipt)
            facts = inner.drain_snapshot(receipt)
            assert facts['retained_exact_handles'] == facts['signaled_exact_handles'] == 4, facts
            print(index, flush=True)
        finally:
            assert (await inner.aclose()).status is DrainStatus.DRAINED
asyncio.run(run())
"""
    owner = await WindowsOwnedProcess.launch(
        generation="nested-direct-no-barrier",
        argv=(python, "-c", child_code),
        cwd=str(Path(windows_process_owner.__file__).resolve().parents[2]),
        env=environment,
        stdin_mode="devnull",
        capture_process_handles=True,
    )
    stdout = asyncio.create_task(owner.stdout.read())
    stderr = asyncio.create_task(owner.stderr.read())
    try:
        returncode = await asyncio.wait_for(owner.wait_root(), 120)
        receipt = await owner.drain_after_grace(grace_timeout=5, force_timeout=5)
        facts = owner.drain_snapshot(receipt)
        (tmp_path / "nested-direct-receipt.json").write_text(json.dumps(facts), encoding="utf-8")
        assert returncode == 0, (await stderr).decode(errors="replace")
        assert (await stdout).splitlines() == [str(index).encode() for index in range(8)]
        assert receipt.status is DrainStatus.DRAINED, facts
        assert not receipt.forced and not receipt.root_was_forced
        assert facts["active_processes"] == 0
        assert facts["total_processes"] == facts["retained_exact_handles"] == 33
        assert facts["signaled_exact_handles"] == 33
        assert facts["birth_notifications"] == 33 and facts["exit_notifications"] == 1
        assert not facts["unverified_membership"] and not facts["handle_probe_failed"]
    finally:
        closed = await owner.aclose()
        assert closed.status is DrainStatus.DRAINED, owner.drain_snapshot(closed)
        await asyncio.gather(stdout, stderr)


def _debug_event(code: int, pid: int = 41, handle: int = 101, *, startup=False):
    return SimpleNamespace(
        dwDebugEventCode=code,
        dwProcessId=pid,
        dwThreadId=51,
        startup=startup,
        u=SimpleNamespace(
            CreateProcessInfo=SimpleNamespace(hProcess=handle, hFile=0),
            LoadDll=SimpleNamespace(hFile=0),
        ),
    )


class _DebugApi(_FakeApi):
    """Controlled process objects and OS-owned events, with real pump threading."""

    def __init__(self, events: list[str], *, total=1, error=None):
        super().__init__(events)
        self.total = total
        self.error = error
        self.objects = {21: (1, 41), 101: (1, 41), 102: (2, 42), 103: (3, 42)}
        self.live_objects = {1}
        self.current_objects = {41: 1}
        self.debug_events = queue.Queue()
        self.debug_events.put(_debug_event(3))
        self.debug_threads: list[threading.Thread] = []
        self.continued: list[tuple[int, int, int]] = []
        self.root_exited = threading.Event()
        self.root_exit_queued = False
        self.exit_received = threading.Event()
        self.release_exit = threading.Event()
        self.hold_exit = False
        self.duplicate_count = 0
        self.continue_failed = False

    def enable_debug_capture(self):
        self.debug_threads.append(threading.current_thread())

    def wait_debug_event(self, timeout_ms):
        self.debug_threads.append(threading.current_thread())
        try:
            event = self.debug_events.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return None
        if self.hold_exit and event.dwDebugEventCode == 5:
            self.exit_received.set()
            assert self.release_exit.wait(2)
        return event

    def continue_debug_event(self, event, status):
        self.debug_threads.append(threading.current_thread())
        if self.error == "continue" and not self.continue_failed:
            self.continue_failed = True
            raise _Win32CallError(AdmissionStage.DRAIN, 56)
        self.continued.append((event.dwDebugEventCode, event.dwProcessId, status))
        if event.dwDebugEventCode == 3:
            identity, pid = self.objects.get(
                event.u.CreateProcessInfo.hProcess,
                (2, event.dwProcessId),
            )
            self.current_objects[pid] = identity
            self.live_objects.add(identity)
        elif event.dwDebugEventCode == 5:
            self.live_objects.discard(self.current_objects[event.dwProcessId])
            if event.dwProcessId == 41:
                self.root_exited.set()

    def duplicate_process(self, handle):
        if not handle or (self.error == "duplicate" and handle == 102):
            raise _Win32CallError(AdmissionStage.DRAIN, 55)
        self.duplicate_count += 1
        duplicate = 1000 + self.duplicate_count
        self.objects[duplicate] = self.objects[handle]
        return duplicate

    def same_process(self, first, second):
        return self.objects[first][0] == self.objects[second][0]

    def process_id(self, handle):
        return (
            99
            if self.error == "identity" and self.objects[handle][0] == 2
            else self.objects[handle][1]
        )

    def is_process_in_job(self, process, job):
        if not self.assign_ok or (self.error == "membership" and self.objects[process][0] == 2):
            return False
        return super().is_process_in_job(process, job)

    def is_startup_breakpoint(self, event):
        return event.startup

    def active_processes(self, job):
        return len(self.live_objects)

    def total_processes(self, job):
        return self.total

    def wait_for_process(self, process, timeout_ms):
        if process == 21 and timeout_ms:
            return self.root_exited.wait(None if timeout_ms == 0xFFFFFFFF else timeout_ms / 1000)
        return self.objects[process][0] not in self.live_objects

    def exit_code(self, process):
        return 0 if self.wait_for_process(process, 0) else None

    def terminate_job(self, job):
        super().terminate_job(job)
        self._queue_root_exit()

    def terminate_process(self, process):
        super().terminate_process(process)
        self._queue_root_exit()

    def _queue_root_exit(self):
        if not self.root_exited.is_set() and not self.root_exit_queued:
            self.root_exit_queued = True
            self.debug_events.put(_debug_event(5))

    def member_process_ids(self, job):
        raise AssertionError("direct capture must not discover capabilities from PIDs")


async def _launch_debug(monkeypatch, api, events, *, pipes=None):
    monkeypatch.setattr(os, "set_handle_inheritable", lambda *_args: None, raising=False)
    ordinary_creator = _creator(events)

    def creator(*, capture_process_handles, **kwargs):
        assert capture_process_handles
        api.debug_threads.append(threading.current_thread())
        return ordinary_creator(**kwargs)

    return await WindowsOwnedProcess._launch_with(
        generation="direct-fake",
        argv=("fixture.exe", "--interpreter=vscode"),
        cwd=None,
        env=None,
        stdin_mode="pipe",
        api=api,
        pipe_ends=pipes or _FakePipes(events),
        process_creator=creator,
        capture_process_handles=True,
    )


@pytest.mark.asyncio
async def test_direct_capture_counts_distinct_objects_not_reused_pids_or_debug_handles(monkeypatch):
    events = []
    api = _DebugApi(events, total=3)
    for event in (
        _debug_event(3),
        _debug_event(3, 42, 102),
        _debug_event(3, 42, 102),
        _debug_event(5, 42),
        _debug_event(3, 42, 103),
        _debug_event(5, 42),
        _debug_event(5),
    ):
        api.debug_events.put(event)
    owner = await _launch_debug(monkeypatch, api, events)
    receipt = await owner.drain_after_grace(grace_timeout=1, force_timeout=1)
    facts = owner.drain_snapshot(receipt)
    assert receipt.status is DrainStatus.DRAINED and not receipt.forced, facts
    assert facts["total_processes"] == facts["retained_exact_handles"] == 3
    assert facts["signaled_exact_handles"] == 3
    assert facts["birth_notifications"] == 1  # Diagnostic omissions cannot manufacture or veto C.
    assert len(set(api.debug_threads)) == 1
    assert api.debug_threads[0] is not threading.current_thread()
    assert (await owner.aclose()).status is DrainStatus.DRAINED
    assert not api.debug_threads[0].is_alive()
    for handle in (11, 12, 21, 31, 1001, 1002, 1003, 1004, 1005):
        assert events.count(f"close:{handle}") == 1


@pytest.mark.parametrize("total", (2, 65537))
@pytest.mark.asyncio
async def test_direct_capture_never_repairs_raw_lifetime_count_gaps(monkeypatch, total):
    events = []
    api = _DebugApi(events, total=total)
    api.debug_events.put(_debug_event(5))
    owner = await _launch_debug(monkeypatch, api, events)
    await asyncio.wait_for(owner.wait_root(), 2)
    receipt = await owner.drain_after_grace(grace_timeout=0, force_timeout=0)
    facts = owner.drain_snapshot(receipt)
    assert receipt.status is DrainStatus.FAILED and facts["active_processes"] == 0
    assert facts["retained_exact_handles"] == facts["signaled_exact_handles"] == 1
    assert facts["total_processes"] == api.total
    assert "close:11" not in events and "close:21" not in events
    capture = owner._debug_capture
    assert capture is not None and capture._thread.is_alive()
    # The simulator has no native capabilities; join only after asserting
    # production failed closed and retained its controlling handles.
    assert await capture.join_exited(1)


@pytest.mark.parametrize("error", ("duplicate", "membership", "identity", "null", "continue"))
@pytest.mark.asyncio
async def test_direct_capture_api_identity_and_continue_failures_remain_causal(monkeypatch, error):
    events = []
    api = _DebugApi(events, total=2, error=error)
    api.debug_events.put(_debug_event(3, 42, 0 if error == "null" else 102))
    api.debug_events.put(_debug_event(5, 42))
    api.debug_events.put(_debug_event(5))
    owner = await _launch_debug(monkeypatch, api, events)
    await asyncio.wait_for(owner.wait_root(), 2)
    first = await owner.drain_after_grace(grace_timeout=0, force_timeout=0)
    second = await owner.force_and_drain(timeout=0)
    assert first.status is second.status is DrainStatus.FAILED
    assert (
        first.winerror
        == second.winerror
        == (56 if error == "continue" else 55 if error in ("duplicate", "null") else None)
    )
    assert "close:11" not in events and "close:21" not in events
    capture = owner._debug_capture
    assert capture.failure is not None and capture._thread.is_alive()
    assert await capture.join_exited(1)


@pytest.mark.asyncio
async def test_direct_capture_exception_forwarding_only_handles_identified_startup(monkeypatch):
    events = []
    api = _DebugApi(events)
    root_event = api.debug_events.get_nowait()
    root_event.u.CreateProcessInfo.hFile = 70
    api.debug_events.put(root_event)
    dll_event = _debug_event(6)
    dll_event.u.LoadDll.hFile = 71
    api.debug_events.put(dll_event)
    for event in (
        _debug_event(1, startup=True),
        _debug_event(1, startup=True),
        _debug_event(1),
        _debug_event(5),
    ):
        api.debug_events.put(event)
    owner = await _launch_debug(monkeypatch, api, events)
    receipt = await owner.drain_after_grace(grace_timeout=1, force_timeout=1)
    assert receipt.status is DrainStatus.DRAINED
    assert [status for code, pid, status in api.continued if code == 1] == [
        0x00010002,
        0x80010001,
        0x80010001,
    ]
    await owner.aclose()
    assert events.count("close:70") == events.count("close:71") == 1
    assert "close:101" not in events  # The OS, not the owner, releases debug handles.


@pytest.mark.asyncio
async def test_direct_capture_repeated_launch_cancellation_joins_admission_and_cleanup(monkeypatch):
    events = []
    api = _DebugApi(events)
    wire_entered, release_wire = asyncio.Event(), asyncio.Event()

    class PausedPipes(_FakePipes):
        async def wire(self, loop):
            wire_entered.set()
            await release_wire.wait()
            return await super().wire(loop)

    caller = asyncio.create_task(_launch_debug(monkeypatch, api, events, pipes=PausedPipes(events)))
    await asyncio.wait_for(wire_entered.wait(), 1)
    for _ in range(2):
        caller.cancel()
        await asyncio.sleep(0)
    assert not caller.done() and "resume-thread" not in events
    release_wire.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, 2)
    assert not api.debug_threads[0].is_alive()
    for handle in (11, 12, 21, 31):
        assert events.count(f"close:{handle}") == 1


@pytest.mark.asyncio
async def test_direct_capture_cancelled_close_pumps_exit_before_signaling_and_join(monkeypatch):
    events = []
    api = _DebugApi(events)
    api.hold_exit = True
    owner = await _launch_debug(monkeypatch, api, events)
    caller = asyncio.create_task(owner.aclose())
    assert await asyncio.to_thread(api.exit_received.wait, 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    second = asyncio.create_task(owner.aclose())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert "close:11" not in events and "close:21" not in events
    assert not api.wait_for_process(21, 0)
    api.release_exit.set()
    receipt = await asyncio.wait_for(owner.aclose(), 2)
    assert receipt.status is DrainStatus.DRAINED
    assert not api.debug_threads[0].is_alive()
    assert events.count("terminate-job") == 1
    assert events.count("close:21") == events.count("close:11") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-child and exception proof")
@pytest.mark.parametrize(
    "code, expected, total",
    (
        (
            "import subprocess; child=subprocess.Popen([python,'-c','raise SystemExit(77)'],creationflags=4,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); child.terminate(); child.wait()",
            0,
            2,
        ),
        ("import ctypes; ctypes.windll.kernel32.DebugBreak()", 0x80000003, 1),
    ),
)
@pytest.mark.asyncio
async def test_direct_capture_native_suspended_termination_and_app_breakpoint(
    code, expected, total
):
    python, environment = _native_python()
    owner = await WindowsOwnedProcess.launch(
        generation="native-capture-edge",
        argv=(python, "-c", f"python = {python!r}\n{code}"),
        cwd=None,
        env=environment,
        stdin_mode="devnull",
        capture_process_handles=True,
    )
    output = asyncio.create_task(owner.stdout.read())
    error = asyncio.create_task(owner.stderr.read())
    try:
        assert await asyncio.wait_for(owner.wait_root(), 10) == expected
        receipt = await owner.drain_after_grace(grace_timeout=1, force_timeout=1)
        facts = owner.drain_snapshot(receipt)
        assert receipt.status is DrainStatus.DRAINED and not receipt.forced, facts
        assert facts["total_processes"] == facts["retained_exact_handles"] == total
        assert facts["signaled_exact_handles"] == total
    finally:
        assert (await owner.aclose()).status is DrainStatus.DRAINED
        await asyncio.gather(output, error)


@pytest.mark.parametrize(
    "stage", (AdmissionStage.ASSIGN, AdmissionStage.WIRE_IO, AdmissionStage.RESUME)
)
@pytest.mark.asyncio
async def test_direct_capture_failed_admission_joins_creator_before_releasing_caps(
    monkeypatch, stage
):
    events = []
    api = _DebugApi(events)
    api.assign_ok = stage is not AdmissionStage.ASSIGN
    api.resume_ok = stage is not AdmissionStage.RESUME

    class FailingPipes(_FakePipes):
        async def wire(self, loop):
            if stage is AdmissionStage.WIRE_IO:
                raise OSError("controlled wiring failure")
            return await super().wire(loop)

    with pytest.raises(ProcessAdmissionError) as raised:
        await _launch_debug(monkeypatch, api, events, pipes=FailingPipes(events))
    assert raised.value.stage is stage
    assert raised.value.winerror == (
        5 if stage is AdmissionStage.ASSIGN else 7 if stage is AdmissionStage.RESUME else None
    )
    if stage is AdmissionStage.ASSIGN:
        assert "terminate-job" not in events
    assert events.count("terminate-process") == 1
    assert api.root_exited.is_set()
    assert not api.debug_threads[0].is_alive()
    if stage is not AdmissionStage.RESUME:
        assert "resume-thread" not in events
    for handle in (11, 12, 21, 31):
        assert events.count(f"close:{handle}") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows descendant-created debug chain proof")
def test_direct_capture_native_new_debug_chain_stays_failed_with_zero_accounting(tmp_path):
    # Isolate a deliberately retained failed owner. Its exact handles and live
    # pump remain owned until the probe process's last consumer has recorded them.
    python, environment = _native_python()
    inner_code = f"""
import asyncio, json
from netcoredbg_mcp.windows_process_owner import WindowsOwnedProcess, DrainStatus
async def run():
    owner = await WindowsOwnedProcess.launch(
        generation='independent-chain', argv=({python!r}, '-c', 'pass'),
        cwd=None, env=None, stdin_mode='devnull', capture_process_handles=True,
    )
    await owner.wait_root()
    receipt = await owner.drain_after_grace(grace_timeout=1, force_timeout=1)
    assert receipt.status is DrainStatus.DRAINED
    print(json.dumps(owner.drain_snapshot(receipt)), flush=True)
    assert (await owner.aclose()).status is DrainStatus.DRAINED
asyncio.run(run())
"""
    probe_code = f"""
import asyncio, json
from netcoredbg_mcp.windows_process_owner import WindowsOwnedProcess, DrainStatus
async def run():
    owner = await WindowsOwnedProcess.launch(
        generation='chain-ancestor', argv=({python!r}, '-c', {inner_code!r}),
        cwd=None, env=None, stdin_mode='devnull', capture_process_handles=True,
    )
    out = asyncio.create_task(owner.stdout.read())
    err = asyncio.create_task(owner.stderr.read())
    assert await asyncio.wait_for(owner.wait_root(), 10) == 0, (await err).decode()
    receipt = await owner.drain_after_grace(grace_timeout=0.1, force_timeout=0.1)
    facts = owner.drain_snapshot(receipt)
    assert receipt.status is DrainStatus.FAILED, facts
    assert facts['total_processes'] == 2 and facts['retained_exact_handles'] == 1, facts
    assert facts['signaled_exact_handles'] == 1 and facts['active_processes'] == 0, facts
    assert owner._job_handle is not None and owner._process_handle is not None
    assert owner._debug_capture._thread.is_alive()
    print(json.dumps({{'ancestor': facts, 'inner': json.loads(await out)}}), flush=True)
asyncio.run(run())
"""
    probe = subprocess.run(
        (python, "-c", probe_code),
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    facts = json.loads(probe.stdout)
    (tmp_path / "split-chain-receipt.json").write_text(json.dumps(facts), encoding="utf-8")
    assert facts["ancestor"]["status"] == "failed"
    assert facts["inner"]["status"] == "drained"
    assert facts["ancestor"]["total_processes"] == 2
    assert facts["ancestor"]["retained_exact_handles"] == 1

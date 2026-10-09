"""Focused owner-admission proof for the private Windows boundary."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _duplicate_fixture_handle(handle: int) -> int:
    """Retain an existing launch capability without opening any numeric PID."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.DuplicateHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    )
    kernel32.DuplicateHandle.restype = ctypes.c_int
    current_process = kernel32.GetCurrentProcess()
    retained = ctypes.c_void_p()
    if not kernel32.DuplicateHandle(
        current_process, handle, current_process, ctypes.byref(retained), 0, 0, 0x00000002
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    assert retained.value is not None
    return retained.value


def _retain_fixture_owner(owner: WindowsOwnedProcess) -> WindowsOwnedProcess:
    """Keep the admitted Job and root alive through production finalizer closure."""
    assert owner._job_handle is not None and owner._process_handle is not None
    job_handle = _duplicate_fixture_handle(owner._job_handle)
    process_handle: int | None = None
    try:
        process_handle = _duplicate_fixture_handle(owner._process_handle)
        return WindowsOwnedProcess(
            owner=owner.owner,
            api=owner._api,
            job_handle=job_handle,
            process_handle=process_handle,
            stdin=None,
            stdout=asyncio.StreamReader(),
            stderr=asyncio.StreamReader(),
            transports=(),
        )
    except BaseException:
        if process_handle is not None:
            owner._api.close_handle(process_handle)
        owner._api.close_handle(job_handle)
        raise


def _retain_fixture_child(child_pid: int, owner: WindowsOwnedProcess) -> int:
    """Capture a read-only child handle at readiness and verify its admitted Job."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    process_handle = kernel32.OpenProcess(0x00100000 | 0x1000, 0, child_pid)
    if not process_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assert owner._job_handle is not None
        assert owner._api.is_process_in_job(process_handle, owner._job_handle) is True
        return process_handle
    except BaseException:
        owner._api.close_handle(process_handle)
        raise


async def _cleanup_fixture_owner(owner: WindowsOwnedProcess) -> None:
    """Drain only the retained launch Job, including when assertions fail."""
    try:
        receipt = await owner.force_and_drain(timeout=5.0)
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.active_processes == 0
    finally:
        await owner.aclose()


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


async def _wait_for_fixture_child_exit(process_handle: int) -> None:
    api = _Kernel32()
    exited = await asyncio.to_thread(api.wait_for_process, process_handle, 3_000)
    assert exited, "fixture descendant survived owner drain"


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

    def create_job(self) -> int:
        self.events.append("create-job")
        return 11

    def set_kill_on_close(self, _job: int) -> None:
        self.events.append("set-job-limits")

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
        *,
        stdout_limit: int | None = None,
    ) -> tuple[None, asyncio.StreamReader, asyncio.StreamReader, tuple[Any, ...]]:
        self.events.append("wire-io")
        stdout = (
            asyncio.StreamReader()
            if stdout_limit is None
            else asyncio.StreamReader(limit=stdout_limit)
        )
        return None, stdout, asyncio.StreamReader(), ()


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
@pytest.mark.parametrize("stdout_limit", [None, 256 * 1024 * 1024])
async def test_pipe_wiring_keeps_large_limit_local_to_bridge_stdout(
    monkeypatch: pytest.MonkeyPatch, stdout_limit: int | None
) -> None:
    protocols: list[asyncio.StreamReaderProtocol] = []
    loop = asyncio.get_running_loop()

    async def connect_read_pipe(protocol_factory, _handle):
        protocol = protocol_factory()
        transport = MagicMock(spec=asyncio.ReadTransport)
        transport.get_extra_info.return_value = None
        protocol.connection_made(transport)
        protocols.append(protocol)
        return transport, protocol

    monkeypatch.setitem(
        sys.modules, "asyncio.windows_utils", SimpleNamespace(PipeHandle=lambda handle: handle)
    )
    monkeypatch.setattr(loop, "connect_read_pipe", connect_read_pipe)
    endpoints = windows_process_owner._PipeEnds(
        stdin_child=0,
        stdin_parent=None,
        stdout_parent=21,
        stdout_child=22,
        stderr_parent=31,
        stderr_child=32,
    )
    _stdin, stdout, stderr, transports = await endpoints.wire(loop, stdout_limit=stdout_limit)
    line = b"A" * (64 * 1024 + 1) + b"\n"
    try:
        protocols[0].data_received(line)
        protocols[1].data_received(line)
        if stdout_limit is None:
            with pytest.raises(ValueError, match="LimitOverrunError|Separator is found"):
                await stdout.readline()
        else:
            assert await stdout.readline() == line
        with pytest.raises(ValueError, match="LimitOverrunError|Separator is found"):
            await stderr.readline()
    finally:
        for transport in transports:
            transport.close()


@pytest.mark.asyncio
async def test_admission_orders_private_job_before_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    owner = await _launch(monkeypatch, _FakeApi(events), events)

    required_order = [
        "create-job",
        "set-job-limits",
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
    fixture_owner: WindowsOwnedProcess | None = None
    child_handle: int | None = None
    try:
        await client.start(generation="real-owner-fixture")
        run = client._run
        assert run is not None and run.owner is not None
        fixture_owner = _retain_fixture_owner(run.owner)
        await asyncio.wait_for(output_seen.wait(), timeout=10.0)
        await _wait_for_path(root_marker)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)
        child_handle = _retain_fixture_child(child_pid, fixture_owner)

        await client.stop()
        await asyncio.wait_for(terminal_seen.wait(), timeout=10.0)

        receipt = run.owner_drain_receipt
        assert receipt is not None
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.active_processes == 0
        assert b"owner-scope-stderr-ready" in terminals[0].stderr_tail
        await _wait_for_fixture_child_exit(child_handle)
    finally:
        try:
            if client.is_running:
                await client.stop()
        finally:
            try:
                if fixture_owner is not None:
                    await _cleanup_fixture_owner(fixture_owner)
            finally:
                if child_handle is not None:
                    _Kernel32().close_handle(child_handle)


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
    root_exit_gate = tmp_path / "root-exit-gate"
    monkeypatch.setenv("OWNER_SCOPE_ROOT_EXIT_GATE", str(root_exit_gate))

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
    fixture_owner: WindowsOwnedProcess | None = None
    child_handle: int | None = None
    try:
        await client.start(generation="real-owner-exit-259")
        run = client._run
        assert run is not None and run.owner is not None
        fixture_owner = _retain_fixture_owner(run.owner)
        await asyncio.wait_for(output_seen.wait(), timeout=10.0)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)
        child_handle = _retain_fixture_child(child_pid, fixture_owner)
        root_exit_gate.touch()

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
        await _wait_for_fixture_child_exit(child_handle)
    finally:
        try:
            if client.is_running:
                await client.stop()
        finally:
            try:
                if fixture_owner is not None:
                    await _cleanup_fixture_owner(fixture_owner)
            finally:
                if child_handle is not None:
                    _Kernel32().close_handle(child_handle)


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

    clients: list[DAPClient] = []
    fixture_owners: list[WindowsOwnedProcess] = []
    child_handles: list[int] = []

    async def start_client(name: str) -> tuple[DAPClient, int]:
        root_marker = tmp_path / f"{name}-root.json"
        child_marker = tmp_path / f"{name}-child.json"
        monkeypatch.setenv("OWNER_SCOPE_ROOT_MARKER", str(root_marker))
        monkeypatch.setenv("OWNER_SCOPE_CHILD_MARKER", str(child_marker))
        client = DAPClient(str(FIXTURE_EXE))
        clients.append(client)
        await client.start(generation=name)
        run = client._run
        assert run is not None and run.owner is not None
        fixture_owner = _retain_fixture_owner(run.owner)
        fixture_owners.append(fixture_owner)
        await _wait_for_path(root_marker)
        await _wait_for_path(child_marker)
        child_pid = await _read_marker_pid(child_marker)
        child_handle = _retain_fixture_child(child_pid, fixture_owner)
        child_handles.append(child_handle)
        return client, child_handle

    try:
        client_a, child_a_handle = await start_client("owner-a")
        client_b, child_b_handle = await start_client("owner-b")

        sentinel_root_marker = tmp_path / "sentinel-root.json"
        sentinel_child_marker = tmp_path / "sentinel-child.json"
        sentinel_env = dict(os.environ)
        sentinel_env["OWNER_SCOPE_ROOT_MARKER"] = str(sentinel_root_marker)
        sentinel_env["OWNER_SCOPE_CHILD_MARKER"] = str(sentinel_child_marker)
        sentinel = await WindowsOwnedProcess.launch(
            generation=object(),
            argv=(str(FIXTURE_EXE), "--foreign-sentinel"),
            cwd=None,
            env=sentinel_env,
            stdin_mode="devnull",
        )
        fixture_owners.append(sentinel)
        await _wait_for_path(sentinel_child_marker)
        sentinel_child = await _read_marker_pid(sentinel_child_marker)
        sentinel_child_handle = _retain_fixture_child(sentinel_child, sentinel)
        child_handles.append(sentinel_child_handle)

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
        await _wait_for_fixture_child_exit(child_a_handle)
        assert client_b.is_running is True
        assert _Kernel32().exit_code(child_b_handle) is None
        assert sentinel.returncode is None
        assert _Kernel32().exit_code(sentinel_child_handle) is None
        build_session.build.assert_awaited_once()
    finally:
        try:
            stop_results = await asyncio.gather(
                *(client.stop() for client in clients if client.is_running),
                return_exceptions=True,
            )
        finally:
            try:
                owner_results = await asyncio.gather(
                    *(_cleanup_fixture_owner(owner) for owner in fixture_owners),
                    return_exceptions=True,
                )
            finally:
                for process_handle in child_handles:
                    _Kernel32().close_handle(process_handle)
        for result in (*stop_results, *owner_results):
            if isinstance(result, BaseException):
                raise result

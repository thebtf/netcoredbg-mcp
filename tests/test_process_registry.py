"""Tests for process registry cleanup behavior."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace
from typing import Any

from netcoredbg_mcp import process_registry


class FakeKernelCall:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.calls.append(tuple(args))
        return self.result


class FakeKernel32:
    def __init__(self, wait_result: int) -> None:
        self.OpenProcess = FakeKernelCall(777)
        self.TerminateProcess = FakeKernelCall(True)
        self.WaitForSingleObject = FakeKernelCall(wait_result)
        self.CloseHandle = FakeKernelCall(True)


class FakeGetExitCodeProcess:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> bool:
        self.calls.append(tuple(args))
        args[1]._obj.value = self.exit_code
        return True


def test_is_pid_alive_windows_configures_ctypes_argtypes(monkeypatch) -> None:
    kernel32 = SimpleNamespace(
        OpenProcess=FakeKernelCall(777),
        GetExitCodeProcess=FakeGetExitCodeProcess(259),
        CloseHandle=FakeKernelCall(True),
    )
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    result = process_registry._is_pid_alive_windows(1234)

    assert result is True
    assert kernel32.OpenProcess.argtypes is not None
    assert kernel32.GetExitCodeProcess.argtypes is not None
    assert kernel32.CloseHandle.argtypes is not None
    assert kernel32.OpenProcess.calls == [(0x1000, False, 1234)]
    assert kernel32.GetExitCodeProcess.calls[0][0] == 777
    assert kernel32.CloseHandle.calls == [(777,)]


def test_terminate_pid_windows_waits_for_process_exit(monkeypatch) -> None:
    kernel32 = FakeKernel32(wait_result=0x00000000)
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    result = process_registry._terminate_pid_windows(1234, timeout=1.5)

    assert result is True
    assert kernel32.OpenProcess.argtypes is not None
    assert kernel32.TerminateProcess.argtypes is not None
    assert kernel32.WaitForSingleObject.argtypes is not None
    assert kernel32.CloseHandle.argtypes is not None
    assert kernel32.OpenProcess.calls == [(0x00100001, False, 1234)]
    assert kernel32.TerminateProcess.calls == [(777, 1)]
    assert kernel32.WaitForSingleObject.calls == [(777, 1500)]
    assert kernel32.CloseHandle.calls == [(777,)]


def test_terminate_pid_windows_reports_timeout_when_process_stays_alive(
    monkeypatch,
) -> None:
    kernel32 = FakeKernel32(wait_result=0x00000102)
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )
    monkeypatch.setattr(process_registry, "_is_pid_alive", lambda pid: True)

    result = process_registry._terminate_pid_windows(1234, timeout=0.25)

    assert result is False
    assert kernel32.WaitForSingleObject.calls == [(777, 250)]
    assert kernel32.CloseHandle.calls == [(777,)]


def test_terminate_pid_windows_treats_false_terminate_as_success_when_dead(
    monkeypatch,
) -> None:
    kernel32 = FakeKernel32(wait_result=0x00000102)
    kernel32.TerminateProcess.result = False
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )
    monkeypatch.setattr(process_registry, "_is_pid_alive", lambda pid: False)

    result = process_registry._terminate_pid_windows(1234, timeout=0.25)

    assert result is True
    assert kernel32.TerminateProcess.calls == [(777, 1)]
    assert kernel32.WaitForSingleObject.calls == []
    assert kernel32.CloseHandle.calls == [(777,)]

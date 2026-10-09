"""Observation metadata must never become process-cleanup authority."""

from __future__ import annotations

import asyncio
import ctypes
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from netcoredbg_mcp import process_registry
from netcoredbg_mcp.build.cleanup import NoOwnedAdapter
from netcoredbg_mcp.session import SessionManager


class FakeKernelCall:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.calls.append(tuple(args))
        return self.result


class FakeGetExitCodeProcess(FakeKernelCall):
    def __call__(self, *args: Any) -> bool:
        self.calls.append(tuple(args))
        args[1]._obj.value = self.result
        return True


def test_is_pid_alive_windows_uses_query_only_handle(monkeypatch) -> None:
    kernel32 = SimpleNamespace(
        OpenProcess=FakeKernelCall(777),
        GetExitCodeProcess=FakeGetExitCodeProcess(259),
        CloseHandle=FakeKernelCall(True),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    assert process_registry._is_pid_alive_windows(1234) is True
    assert kernel32.OpenProcess.calls == [(0x1000, False, 1234)]
    assert kernel32.CloseHandle.calls == [(777,)]


@pytest.mark.asyncio
async def test_observed_attached_pid_has_no_cleanup_authority(monkeypatch) -> None:
    registry = process_registry.ProcessRegistry()
    observation = registry.observe(44009, "debuggee", generation="attach", session_id="forged")
    monkeypatch.setattr(process_registry, "_is_pid_alive", lambda pid: True)
    with patch.object(process_registry.os, "kill", side_effect=AssertionError("numeric signal")):
        result = await registry.cleanup_all()
    assert result.complete is True
    assert result.terminated == 0
    assert registry.get_all() == [observation]


@pytest.mark.asyncio
async def test_same_pid_replacement_does_not_transfer_or_forget_new_generation() -> None:
    registry = process_registry.ProcessRegistry()
    old = registry.observe(44009, "netcoredbg", generation="old")
    new = registry.observe(44009, "debuggee", generation="new")
    calls: list[str] = []

    async def cleanup():
        calls.append("old-owner")
        registry.forget(old)
        return process_registry.CleanupOutcome(complete=True, terminated=1)

    token = registry.register_owner(generation="old", owner=object(), cleanup=cleanup)
    result = await registry.cleanup_all()
    assert calls == ["old-owner"]
    assert result.terminated == 1
    assert registry.get_all() == [new]
    assert registry.release_owner(token) is False


@pytest.mark.asyncio
async def test_concurrent_cleanup_deduplicates_captured_owner() -> None:
    registry = process_registry.ProcessRegistry()
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def cleanup():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return process_registry.CleanupOutcome(complete=True, terminated=1)

    first_token = registry.register_owner(generation="one", owner=owner, cleanup=cleanup)
    assert registry.register_owner(generation="one", owner=owner, cleanup=cleanup) is first_token
    first = asyncio.create_task(registry.cleanup_all())
    await entered.wait()
    second = asyncio.create_task(registry.cleanup_all())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == 1
    assert all(result.complete and result.terminated == 1 for result in results)
    assert (await registry.cleanup_all()).terminated == 0


@pytest.mark.asyncio
async def test_failed_owner_retained_while_other_owner_finishes() -> None:
    registry = process_registry.ProcessRegistry()
    attempts = 0

    async def retryable():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("native drain failed")
        return process_registry.CleanupOutcome(complete=True)

    async def other():
        return process_registry.CleanupOutcome(complete=True, terminated=1)

    registry.register_owner(generation="bad", owner=object(), cleanup=retryable)
    registry.register_owner(generation="good", owner=object(), cleanup=other)
    failed = await registry.cleanup_all()
    assert failed.complete is False and failed.remaining_owners == 1
    assert failed.terminated == 1 and "native drain failed" in failed.errors[0]
    assert (await registry.cleanup_all()).complete is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_or_drop_owner() -> None:
    registry = process_registry.ProcessRegistry()
    entered, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        entered.set()
        await release.wait()
        return process_registry.CleanupOutcome(complete=True)

    registry.register_owner(generation="cancel", owner=object(), cleanup=cleanup)
    waiter = asyncio.create_task(registry.cleanup_all())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.owner_count == 1
    release.set()
    assert (await registry.cleanup_all()).complete is True


@pytest.mark.asyncio
async def test_incomplete_callback_remains_registered() -> None:
    registry = process_registry.ProcessRegistry()

    async def cleanup():
        return process_registry.CleanupOutcome(complete=False, error="exit unobserved")

    registry.register_owner(generation="incomplete", owner=object(), cleanup=cleanup)
    result = await registry.cleanup_all()
    assert result.complete is False and result.remaining_owners == 1
    assert result.errors == ["exit unobserved"]


@pytest.mark.asyncio
async def test_pid_only_observation_cannot_create_prebuild_owner_capability() -> None:
    registry = process_registry.ProcessRegistry()
    registry.observe(44009, "netcoredbg", generation="former-owner")
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    client = MagicMock()
    client.adapter_owner = None
    manager._client = client
    manager._active_dap_run = "current-generation"
    manager._process_registry = registry
    assert isinstance(manager.capture_prebuild_owner(), NoOwnedAdapter)
    client.stop.assert_not_called()


def test_captured_owner_cannot_be_registered_under_another_generation():
    registry = process_registry.ProcessRegistry()
    owner = object()

    async def cleanup():
        return process_registry.CleanupOutcome(complete=True)

    registry.register_owner(generation="old", owner=owner, cleanup=cleanup)
    with pytest.raises(ValueError, match="generation"):
        registry.register_owner(generation="new", owner=owner, cleanup=cleanup)
    assert registry.owner_count == 1

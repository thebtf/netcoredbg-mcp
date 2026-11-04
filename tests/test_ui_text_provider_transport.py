"""One-shot provider protocol and observation handles, without desktop effects."""

from __future__ import annotations

import asyncio
import ctypes
import json
from collections import deque
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.dap.client import DAPClient
from netcoredbg_mcp.session.manager import SessionManager
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.ui import provider_text
from netcoredbg_mcp.windows_process_owner import DrainStatus


def completed():
    return {"status": "PASS", "effect_state": "completed", "verified": True}


@pytest.mark.parametrize("text", ["", " \t\r\n ", "^+%{ENTER}()~", "A\U0001f680Z", "\ud83d\ude80"])
def test_valid_literal_unicode_has_no_length_or_keyboard_conversion(text):
    assert provider_text.validate_request({"automation_id": "Exact ID"}, text) is None


@pytest.mark.parametrize("text", ["\x00", "\ud800", "\udfff", "\ud800A", "A\udfff"])
def test_invalid_utf16_refuses_before_dispatch(text):
    assert provider_text.validate_request(
        {"automation_id": "Exact ID"}, text
    ) == provider_text.blocked("invalid_text")


@pytest.mark.parametrize(
    "line",
    [
        b'{"jsonrpc":"2.0","id":"stale","result":{"status":"PASS","effect_state":"completed","verified":true}}\n',
        b'{"jsonrpc":"2.0","id":"stale","id":"request","result":{"status":"PASS","effect_state":"completed","verified":true}}\n',
        b'{"jsonrpc":"2.0","id":"request","result":{"status":"PASS","effect_state":"unknown","verified":true}}\n',
        b'{"jsonrpc":"2.0","id":"request","result":{"status":"PASS","effect_state":"completed","verified":1}}\n',
        b'{"jsonrpc":"2.0","id":"request","result":{"status":"PASS","effect_state":"completed","verified":true,"text":"PRIVATE"}}\n',
        b'{"jsonrpc":"2.0","id":"request","error":{"message":"PRIVATE"}}\n',
    ],
)
def test_uncorrelated_or_malformed_wire_evidence_is_unknown_without_raw_data(line):
    assert provider_text._response(line, "request") == provider_text.unknown()


def test_observation_handle_requests_no_termination_right_and_binds_creation_filetime(monkeypatch):
    from ctypes import wintypes

    api = SimpleNamespace(
        OpenProcess=MagicMock(return_value=1234),
        GetProcessTimes=MagicMock(),
        WaitForSingleObject=MagicMock(return_value=258),
        CloseHandle=MagicMock(return_value=True),
    )

    def get_times(_handle, creation, *_unused):
        value = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwHighDateTime = 7
        value.dwLowDateTime = 123
        return True

    api.GetProcessTimes.side_effect = get_times
    monkeypatch.setattr(provider_text, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", MagicMock(return_value=api), raising=False)
    binding = provider_text.capture_process(42)
    try:
        api.OpenProcess.assert_called_once_with(0x00101000, False, 42)
        assert binding.identity == (42, str((7 << 32) | 123))
        assert binding.is_signaled() is False
        api.WaitForSingleObject.return_value = 0
        assert binding.is_signaled() is True
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        with pytest.raises(RuntimeError, match="process_liveness_unavailable"):
            binding.is_signaled()
    finally:
        binding.close()
    api.CloseHandle.assert_called_once_with(1234)


def test_unsupported_platform_refuses_without_native_api_initialization(monkeypatch):
    monkeypatch.setattr(provider_text, "os", SimpleNamespace(name="posix"))
    native_api = MagicMock(side_effect=AssertionError("Native API must not be loaded"))
    monkeypatch.setattr(ctypes, "WinDLL", native_api, raising=False)
    with pytest.raises(RuntimeError, match="unsupported_platform"):
        provider_text.capture_process(42)
    native_api.assert_not_called()


READY_BANNER = b'{"protocol":"netcoredbg.provider-text.v1","ready":true}\n'


class HelperWriter:
    def __init__(self, dispatched):
        self.dispatched = dispatched
        self.writes = []
        self.closed = False
        self.ready_received = False
        self.writes_before_ready = []

    def write(self, value):
        assert self.dispatched.is_set(), "Reservation must be marked immediately before first write"
        self.writes.append(value)
        if not self.ready_received:
            self.writes_before_ready.append(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class HelperOutput:
    def __init__(self, writer, *, banner=READY_BANNER, wrong_id=False, trailing=b"", lost=False):
        self.writer = writer
        self.banner = banner
        self.banner_reads = 0
        self.response_reads = 0
        self.wrong_id = wrong_id
        self.trailing = trailing
        self.lost = lost

    async def readline(self):
        if self.banner_reads == 0:
            self.banner_reads += 1
            if isinstance(self.banner, Exception):
                raise self.banner
            self.writer.ready_received = True
            return self.banner
        self.response_reads += 1
        assert self.writer.closed, "Helper requires one request and EOF before effect"
        assert len(self.writer.writes) == 1, "Helper accepts exactly one request"
        if self.lost:
            return b""
        request = json.loads(self.writer.writes[0])
        return (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "stale" if self.wrong_id else request["id"],
                    "result": completed(),
                }
            ).encode()
            + b"\n"
        )

    async def read(self, _size):
        return self.trailing


class FakeHelper:
    def __init__(
        self, dispatched, *, banner=READY_BANNER, wrong_id=False, trailing=b"", lost=False
    ):
        self.stdin = HelperWriter(dispatched)
        self.stdout = HelperOutput(
            self.stdin, banner=banner, wrong_id=wrong_id, trailing=trailing, lost=lost
        )
        self.stderr = SimpleNamespace(read=AsyncMock(return_value=b""))
        self.drain_after_grace = AsyncMock(
            return_value=SimpleNamespace(
                status=DrainStatus.DRAINED,
                active_processes=0,
            )
        )
        self.aclose = AsyncMock()


def install_helper(monkeypatch, helper):
    launch = AsyncMock(return_value=helper)
    monkeypatch.setattr(provider_text, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(provider_text.WindowsOwnedProcess, "launch", launch)
    monkeypatch.setattr(
        "netcoredbg_mcp.setup.bridge.find_or_build_bridge", lambda: "FlaUIBridge.exe"
    )
    return launch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "wrong-id", "extra-output", "write-error", "lost-result"]
)
async def test_transport_uses_one_restricted_owned_helper_and_sanitizes_uncertainty(
    monkeypatch, failure
):
    dispatched = asyncio.Event()
    helper = FakeHelper(
        dispatched,
        wrong_id=failure == "wrong-id",
        trailing=b"PRIVATE" if failure == "extra-output" else b"",
        lost=failure == "lost-result",
    )
    launch = install_helper(monkeypatch, helper)
    dispatch_readiness = []

    def on_dispatch():
        dispatch_readiness.append(helper.stdin.ready_received)
        dispatched.set()

    if failure == "write-error":
        helper.stdin.write = MagicMock(side_effect=OSError("PRIVATE provider detail"))
    binding = SimpleNamespace(pid=42, creation_time="123456789", is_signaled=lambda: False)
    text = "^+%{ENTER}()~\r\n\t\U0001f680"
    result = await provider_text.run_provider_text(
        binding=cast(provider_text.ProcessBinding, binding),
        selector={"automation_id": "Exact ID"},
        text=text,
        on_dispatch=on_dispatch,
    )
    assert result == (completed() if failure is None else provider_text.unknown())
    assert dispatch_readiness == [True]
    assert helper.stdout.banner_reads == 1
    assert helper.stdin.writes_before_ready == []
    if failure != "write-error":
        assert helper.stdout.response_reads == 1
    assert launch.await_count == 1
    assert launch.call_args.kwargs["argv"] == ("FlaUIBridge.exe", "--provider-text-stdio")
    assert launch.call_args.kwargs["stdin_mode"] == "pipe"
    if failure != "write-error":
        assert len(helper.stdin.writes) == 1
        wire = helper.stdin.writes[0]
        assert wire.count(b"\n") == 1
        request = json.loads(wire)
        assert request["method"] == "provider_text.set"
        assert request["params"] == {
            "pid": 42,
            "creation_time": "123456789",
            "selector": {"automation_id": "Exact ID"},
            "text": text,
        }
    helper.drain_after_grace.assert_awaited_once()
    helper.aclose.assert_awaited_once()
    assert helper.stdin.closed
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "banner",
    [
        pytest.param(b"", id="absent"),
        pytest.param(b"PRIVATE not JSON\n", id="malformed"),
        pytest.param(
            b'{"jsonrpc":"2.0","id":null,"error":{"message":"PRIVATE"}}\n',
            id="legacy-error-envelope",
        ),
        pytest.param(b'{"protocol":"PRIVATE-old-version","ready":true}\n', id="unsupported"),
        pytest.param(b'{"protocol":"netcoredbg.provider-text.v1","ready":false}\n', id="not-ready"),
        pytest.param(b'{"protocol":"netcoredbg.provider-text.v1","ready":1}\n', id="nonboolean"),
        pytest.param(b'{"protocol":"netcoredbg.provider-text.v1"}\n', id="missing-ready"),
        pytest.param(
            b'{"protocol":"netcoredbg.provider-text.v1","ready":false,"ready":true}\n',
            id="duplicate-key",
        ),
        pytest.param(
            b'{"protocol":"netcoredbg.provider-text.v1","ready":true,"detail":"PRIVATE"}\n',
            id="extra-field",
        ),
        pytest.param(READY_BANNER.rstrip(b"\n"), id="unterminated"),
        pytest.param(b" " * (16 * 1024) + READY_BANNER, id="oversized"),
        pytest.param(TimeoutError("PRIVATE startup timeout"), id="timeout"),
    ],
)
async def test_unready_helper_blocks_without_payload_or_dispatch_and_drains_only_owned_helper(
    monkeypatch, banner
):
    dispatched = asyncio.Event()
    helper = FakeHelper(dispatched, banner=banner)
    launch = install_helper(monkeypatch, helper)
    unrelated_helper = FakeHelper(asyncio.Event())
    launch.side_effect = [helper, unrelated_helper]
    binding = SimpleNamespace(
        pid=42, creation_time="123456789", is_signaled=lambda: False, close=MagicMock()
    )
    on_dispatch = MagicMock(side_effect=dispatched.set)

    result = await provider_text.run_provider_text(
        binding=cast(provider_text.ProcessBinding, binding),
        selector={"automation_id": "Exact ID"},
        text="PRIVATE literal payload",
        on_dispatch=on_dispatch,
    )

    assert helper.stdin.writes == []
    on_dispatch.assert_not_called()
    assert not dispatched.is_set()
    assert result["status"] == "BLOCKED"
    assert result["effect_state"] == "not_started"
    assert result["verified"] is False
    assert result == provider_text.sanitize_result(result)
    assert "PRIVATE" not in json.dumps(result)
    assert helper.stdout.banner_reads == 1
    assert helper.stdout.response_reads == 0
    launch.assert_awaited_once()
    assert helper.stdin.closed
    helper.drain_after_grace.assert_awaited_once()
    helper.aclose.assert_awaited_once()
    binding.close.assert_not_called()
    assert unrelated_helper.stdin.writes == []
    assert not unrelated_helper.stdin.closed
    unrelated_helper.drain_after_grace.assert_not_awaited()
    unrelated_helper.aclose.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "banner,ready",
    [
        pytest.param(b"", False, id="absent"),
        pytest.param(b"PRIVATE invalid banner\n", False, id="invalid"),
        pytest.param(
            b'{"protocol":"netcoredbg.provider-text.v1","ready":true,"extra":"PRIVATE"}\n',
            False,
            id="extra-field",
        ),
        pytest.param(READY_BANNER, True, id="ready-result-lost"),
    ],
)
async def test_only_ready_dispatched_uncertainty_fences_live_session(monkeypatch, banner, ready):
    monkeypatch.setattr("netcoredbg_mcp.session.manager.DAPClient", MagicMock())
    session = SessionManager(netcoredbg_path="/unused-in-memory-dap")
    session._client = DAPClient("/unused-in-memory-dap")
    session._client._process = MagicMock(pid=43, returncode=None)
    session._active_dap_run = session._client._run_for_direct_reader().generation
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    binding = SimpleNamespace(
        pid=42,
        creation_time="123456789",
        identity=(42, "123456789"),
        is_signaled=lambda: False,
        close=MagicMock(),
    )
    capture = MagicMock(return_value=binding)
    monkeypatch.setattr(provider_text, "capture_process", capture)
    helper = FakeHelper(asyncio.Event(), banner=banner, lost=True)
    launch = install_helper(monkeypatch, helper)
    real_run = provider_text.run_provider_text
    dispatch = MagicMock()

    async def run(**kwargs):
        original_dispatch = kwargs["on_dispatch"]

        def on_dispatch():
            dispatch(helper.stdin.ready_received)
            original_dispatch()
            helper.stdin.dispatched.set()

        kwargs["on_dispatch"] = on_dispatch
        return await real_run(**kwargs)

    monkeypatch.setattr(provider_text, "run_provider_text", run)
    result = await session.replace_text_provider({"automation_id": "Exact ID"}, "literal")

    if ready:
        dispatch.assert_called_once_with(True)
        assert result == provider_text.unknown()
        assert len(helper.stdin.writes) == 1
        assert helper.stdin.writes_before_ready == []
        assert binding.identity in session._provider_text_operations
        binding.close.assert_not_called()
        refused = await session.replace_text_provider({"automation_id": "Exact ID"}, "other")
        assert refused == provider_text.blocked("provider_operation_unresolved")
    else:
        dispatch.assert_not_called()
        assert result["status"] == "BLOCKED"
        assert result["effect_state"] == "not_started"
        assert result["verified"] is False
        assert result == provider_text.sanitize_result(result)
        assert helper.stdin.writes == []
        assert session._provider_text_operations == {}
        binding.close.assert_called_once()
    capture.assert_called_once_with(42)
    launch.assert_awaited_once()
    assert helper.stdin.closed
    helper.drain_after_grace.assert_awaited_once()
    helper.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_predispatch_cancellation_retains_launch_and_drains_only_its_helper(monkeypatch):
    dispatched = asyncio.Event()
    helper = FakeHelper(dispatched)
    entered, release = asyncio.Event(), asyncio.Event()

    async def launch(**_kwargs):
        entered.set()
        await release.wait()
        return helper

    monkeypatch.setattr(provider_text, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(provider_text.WindowsOwnedProcess, "launch", launch)
    monkeypatch.setattr(
        "netcoredbg_mcp.setup.bridge.find_or_build_bridge", lambda: "FlaUIBridge.exe"
    )
    binding = SimpleNamespace(pid=42, creation_time="123", is_signaled=lambda: False)
    task = asyncio.create_task(
        provider_text.run_provider_text(
            binding=cast(provider_text.ProcessBinding, binding),
            selector={"automation_id": "Exact ID"},
            text="literal",
            on_dispatch=dispatched.set,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert not dispatched.is_set()
        assert helper.stdin.writes == []
        helper.aclose.assert_awaited_once()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_original_completion_cannot_clear_reused_pid_operation(monkeypatch):
    monkeypatch.setattr("netcoredbg_mcp.session.manager.DAPClient", MagicMock())
    session = SessionManager(netcoredbg_path="/unused-in-memory-dap")
    session._client = DAPClient("/unused-in-memory-dap")
    session._client._process = MagicMock(pid=43, returncode=None)
    session._active_dap_run = session._client._run_for_direct_reader().generation
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    original_exited = False
    original = SimpleNamespace(
        identity=(42, "100"),
        is_signaled=lambda: original_exited,
        close=MagicMock(),
    )
    replacement = SimpleNamespace(
        identity=(42, "101"),
        is_signaled=lambda: False,
        close=MagicMock(),
    )
    bindings = deque([original, replacement])
    monkeypatch.setattr(provider_text, "capture_process", lambda _pid: bindings.popleft())
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    index = 0

    async def run(**kwargs):
        nonlocal index
        current = index
        index += 1
        kwargs["on_dispatch"]()
        entered[current].set()
        await release[current].wait()
        return completed()

    monkeypatch.setattr(provider_text, "run_provider_text", run)
    selector = {"automation_id": "Exact ID"}
    caller = asyncio.create_task(session.replace_text_provider(selector, "A"))
    newer = None
    try:
        await asyncio.wait_for(entered[0].wait(), timeout=1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        original_receiver = next(iter(session._provider_text_receivers))
        original_exited = True
        newer = asyncio.create_task(session.replace_text_provider(selector, "B"))
        await asyncio.wait_for(entered[1].wait(), timeout=1)
        release[0].set()
        await asyncio.wait_for(asyncio.shield(original_receiver), timeout=1)
        refused = await session.replace_text_provider(selector, "C")
        assert refused == provider_text.blocked("provider_operation_unresolved")
        assert index == 2
        release[1].set()
        assert await asyncio.wait_for(newer, timeout=1) == completed()
    finally:
        release[0].set()
        release[1].set()
        caller.cancel()
        if newer is not None:
            newer.cancel()
        await asyncio.gather(
            caller, *([newer] if newer is not None else []), return_exceptions=True
        )
        await asyncio.gather(*tuple(session._provider_text_receivers), return_exceptions=True)

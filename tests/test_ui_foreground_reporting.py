"""Behavioral regressions for #538; all desktop and bridge calls are fake.

The registered tool and FlaUI activation/connect methods remain real.  A result
is a point-in-time observation, not a promise that foreground ownership persists.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.ui import register_ui_tools
from netcoredbg_mcp.ui import foreground, screenshot
from netcoredbg_mcp.ui.flaui_client import FlaUIBackend, FlaUIBridgeClient
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

TARGET_HWND = 0x12345678
TARGET_PID = 42
OTHER_HWND = 0x23456789
OTHER_PID = 73


class FakeDesktop:
    """Independent Win32 facts, changed only by explicit test callbacks."""

    def __init__(self) -> None:
        self.windows = {TARGET_HWND, OTHER_HWND}
        self.owners = {TARGET_HWND: TARGET_PID, OTHER_HWND: OTHER_PID}
        self.foreground_hwnd = OTHER_HWND
        self.api_result = True
        self.on_activation = lambda: setattr(self, "foreground_hwnd", TARGET_HWND)
        self.on_lookup = lambda: None
        self.events: list[str] = []
        self.user32 = SimpleNamespace(
            IsWindow=Mock(side_effect=lambda hwnd: int(hwnd in self.windows)),
            IsWindowVisible=Mock(side_effect=lambda hwnd: int(hwnd in self.windows)),
            GetForegroundWindow=Mock(side_effect=self.get_foreground),
            GetWindowThreadProcessId=Mock(side_effect=self.get_owner),
            ShowWindow=Mock(return_value=True),
            BringWindowToTop=Mock(return_value=True),
            SetForegroundWindow=Mock(side_effect=self.activate),
        )
        self.forbidden = {}
        for name in (
            "AttachThreadInput",
            "SendInput",
            "keybd_event",
            "mouse_event",
            "SetCursorPos",
            "SendMessageW",
            "PostMessageW",
            "SwitchToThisWindow",
            "AllowSetForegroundWindow",
        ):
            # Record even calls caught by a public error envelope; never touch Win32.
            operation = Mock(name=name, return_value=False)
            self.forbidden[name] = operation
            setattr(self.user32, name, operation)
        self.kernel32 = SimpleNamespace(GetCurrentThreadId=Mock(return_value=7))
        self.lookup = Mock(side_effect=self.resolve_target)

    def resolve_target(self, pid: int) -> int:
        assert pid == TARGET_PID, "The request must not silently retarget another process"
        self.events.append("resolve-target")
        self.on_lookup()
        return TARGET_HWND

    def get_foreground(self) -> int:
        self.events.append("foreground-snapshot")
        return self.foreground_hwnd or 0

    def get_owner(self, hwnd: int, pid_out) -> int:
        pid = self.owners.get(hwnd) if hwnd in self.windows else None
        if pid_out is not None:
            ctypes.cast(pid_out, ctypes.POINTER(ctypes.c_ulong)).contents.value = pid or 0
        return pid + 1000 if pid else 0

    def activate(self, hwnd: int) -> bool:
        assert hwnd == TARGET_HWND
        self.events.append("set-foreground")
        self.on_activation()
        return self.api_result

    def invalidate_target(self, change: str) -> None:
        if change == "gone":
            self.windows.remove(TARGET_HWND)
        elif change == "different-owner":
            self.owners[TARGET_HWND] = OTHER_PID
        elif change == "unknown-owner":
            self.owners[TARGET_HWND] = None
        else:
            raise AssertionError(f"Unexpected scripted change: {change}")


@pytest.fixture
def foreground_case(monkeypatch, capturing_mcp):
    desktop = FakeDesktop()
    dlls = SimpleNamespace(user32=desktop.user32, kernel32=desktop.kernel32)
    monkeypatch.setattr(ctypes, "windll", dlls, raising=False)

    def load_fake_dll(name, *_args, **_kwargs):
        normalized = str(name).lower().removesuffix(".dll")
        assert normalized in {"user32", "kernel32"}, f"Unexpected native library: {name}"
        return getattr(dlls, normalized)

    monkeypatch.setattr(ctypes, "WinDLL", load_fake_dll, raising=False)
    # Do not alter the shared os module (pytest/pathlib use its platform flag).
    monkeypatch.setattr(foreground, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(screenshot, "get_hwnd_for_pid", desktop.lookup)
    no_sleep = AsyncMock(side_effect=AssertionError("Activation must not sleep or retry"))
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    session = SimpleNamespace(
        process_registry=None,
        session_id="foreground-session",
        state=SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=TARGET_PID,
            activity_epoch=object(),
        ),
        stealth_mode=True,
        cancel_pending_stealth_foreground_restore=AsyncMock(return_value=False),
    )
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = TARGET_PID
    backend._element_cache = {}
    case = SimpleNamespace(
        desktop=desktop,
        session=session,
        backend=backend,
        on_reconnect=None,
        reconnect_entered=asyncio.Event(),
        release_reconnect=asyncio.Event(),
        pause_reconnect=False,
    )

    async def bridge_call(method, params=None, **_kwargs):
        assert method == "connect", f"Unexpected bridge operation: {method}"
        assert params == {"pid": TARGET_PID}, "Stealth exit must reconnect without stealth"
        desktop.events.append("reconnect-entered")
        case.reconnect_entered.set()
        if case.pause_reconnect:
            await case.release_reconnect.wait()
        if case.on_reconnect is not None:
            case.on_reconnect()
        desktop.events.append("reconnect-finished")
        return {"connected": True}

    backend._client = SimpleNamespace(
        is_running=True,
        ensure_alive=AsyncMock(),
        call=AsyncMock(side_effect=bridge_call),
        stop=AsyncMock(),
    )
    fallback = PywinautoBackend.__new__(PywinautoBackend)
    fallback._ui = SimpleNamespace(process_id=TARGET_PID, _element_cache={})
    for name in ("send_keys", "click", "click_at", "set_focus", "type_keys", "press", "move"):
        operation = Mock(name=f"fallback.{name}", return_value=None)
        desktop.forbidden[f"fallback.{name}"] = operation
        setattr(fallback._ui, name, operation)
    case.fallback = fallback

    async def invoke(route: str):
        if route == "backend":
            return await backend.bring_to_front()
        selected = fallback if route == "fallback" else backend
        register_ui_tools(
            capturing_mcp,
            session,
            check_session_access=lambda _ctx: None,
            backend_holder={"instance": selected},
        )
        response = await capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())
        assert "error" not in response, response
        assert isinstance(response.get("data"), dict), response
        return response["data"]

    case.invoke = invoke
    yield case
    for operation in desktop.forbidden.values():
        operation.assert_not_called()
    assert desktop.user32.SetForegroundWindow.call_count <= 1, "No activation retries"
    assert desktop.lookup.call_count <= 1, "No re-resolution/re-targeting retries"
    assert backend.client.call.await_count <= 1, "No bridge reconnect retries"
    no_sleep.assert_not_awaited()


def assert_report(
    result,
    *,
    activated: bool,
    api_result: bool | None,
    attempted: bool = True,
    observed_pid: int | None = TARGET_PID,
    foreground_hwnd: int | None = TARGET_HWND,
    foreground_pid: int | None = TARGET_PID,
    matches: bool | None = True,
) -> None:
    assert result["activated"] is activated
    assert result["hwnd"] == TARGET_HWND
    assert result["activation_requested"] is True
    assert result["activation_attempted"] is attempted
    assert result["activation_api_result"] is api_result
    assert result["target"] == {
        "hwnd": TARGET_HWND,
        "expected_pid": TARGET_PID,
        "observed_pid": observed_pid,
    }
    assert result["foreground"] == {"hwnd": foreground_hwnd, "pid": foreground_pid}
    assert result["foreground_matches_target"] is matches


@pytest.mark.parametrize("route", ["backend", "public", "fallback"])
@pytest.mark.parametrize("api_result", [True, False], ids=["api-true", "api-false"])
@pytest.mark.parametrize("matches", [True, False], ids=["final-match", "final-mismatch"])
async def test_api_return_and_final_foreground_are_independent(
    foreground_case, route, api_result, matches
):
    case = foreground_case
    case.desktop.api_result = api_result
    final_hwnd = TARGET_HWND if matches else OTHER_HWND
    case.desktop.on_activation = lambda: setattr(case.desktop, "foreground_hwnd", final_hwnd)

    result = await case.invoke(route)

    assert_report(
        result,
        activated=matches,
        api_result=api_result,
        foreground_hwnd=final_hwnd,
        foreground_pid=TARGET_PID if matches else OTHER_PID,
        matches=matches,
    )
    case.desktop.user32.SetForegroundWindow.assert_called_once_with(TARGET_HWND)
    if route != "backend":
        assert result["stealth_mode"] is case.session.stealth_mode
        assert case.session.stealth_mode is (not matches)
    if route != "fallback":
        assert case.backend.client.call.await_count == int(matches)


@pytest.mark.parametrize("route", ["backend", "public", "fallback"])
@pytest.mark.parametrize("unknown", ["no-foreground", "foreground-pid-unavailable"])
async def test_unknown_foreground_is_null_not_false(foreground_case, route, unknown):
    case = foreground_case
    hwnd = None if unknown == "no-foreground" else OTHER_HWND
    case.desktop.owners[OTHER_HWND] = None
    case.desktop.on_activation = lambda: setattr(case.desktop, "foreground_hwnd", hwnd)

    result = await case.invoke(route)

    assert_report(
        result,
        activated=False,
        api_result=True,
        foreground_hwnd=hwnd,
        foreground_pid=None,
        matches=None,
    )
    if route != "backend":
        assert result["stealth_mode"] is True
        assert case.session.stealth_mode is True


@pytest.mark.parametrize("route", ["backend", "public", "fallback"])
@pytest.mark.parametrize("change", ["gone", "different-owner", "unknown-owner"])
async def test_invalid_target_before_activation_never_calls_activation(
    foreground_case, route, change
):
    case = foreground_case
    case.desktop.on_lookup = lambda: case.desktop.invalidate_target(change)

    result = await case.invoke(route)

    case.desktop.user32.SetForegroundWindow.assert_not_called()
    case.desktop.user32.ShowWindow.assert_not_called()
    case.desktop.user32.BringWindowToTop.assert_not_called()
    case.backend.client.call.assert_not_awaited()
    assert_report(
        result,
        activated=False,
        attempted=False,
        api_result=None,
        observed_pid=OTHER_PID if change == "different-owner" else None,
        foreground_hwnd=OTHER_HWND,
        foreground_pid=OTHER_PID,
        matches=False if change == "different-owner" else None,
    )
    if route != "backend":
        assert result["stealth_mode"] is True
        assert case.session.stealth_mode is True


@pytest.mark.parametrize("route", ["backend", "public"])
@pytest.mark.parametrize("change", ["gone", "different-owner", "unknown-owner"])
async def test_target_is_revalidated_after_reconnect(foreground_case, route, change):
    case = foreground_case
    case.on_reconnect = lambda: case.desktop.invalidate_target(change)

    result = await case.invoke(route)

    assert_report(
        result,
        activated=False,
        api_result=True,
        observed_pid=OTHER_PID if change == "different-owner" else None,
        foreground_pid=OTHER_PID if change == "different-owner" else None,
        matches=False if change == "different-owner" else None,
    )
    case.backend.client.call.assert_awaited_once()
    assert case.desktop.events[-1] == "foreground-snapshot"
    if route == "public":
        # Backend mode changed even though the final ownership proof failed.
        assert result["stealth_mode"] is False
        assert case.session.stealth_mode is False


@pytest.mark.parametrize("route", ["backend", "public"])
async def test_foreground_change_while_reconnecting_invalidates_legacy_success(
    foreground_case, route
):
    case = foreground_case
    case.pause_reconnect = True
    operation = asyncio.create_task(case.invoke(route))
    try:
        await asyncio.wait_for(case.reconnect_entered.wait(), timeout=2)
        assert not operation.done(), "The reconnect barrier must precede the final snapshot"
        case.desktop.foreground_hwnd = OTHER_HWND
    finally:
        case.release_reconnect.set()
        result = await asyncio.wait_for(operation, timeout=2)

    assert_report(
        result,
        activated=False,
        api_result=True,
        foreground_hwnd=OTHER_HWND,
        foreground_pid=OTHER_PID,
        matches=False,
    )
    assert case.desktop.events.index("reconnect-finished") < len(case.desktop.events) - 1
    assert case.desktop.events[-1] == "foreground-snapshot"
    case.backend.client.call.assert_awaited_once()
    if route == "public":
        assert result["stealth_mode"] is False
        assert case.session.stealth_mode is False


@pytest.mark.parametrize("change", ["pid", "epoch"])
async def test_public_session_replacement_during_reconnect_cannot_claim_success(
    foreground_case, change
):
    case = foreground_case
    case.pause_reconnect = True
    operation = asyncio.create_task(case.invoke("public"))
    try:
        await asyncio.wait_for(case.reconnect_entered.wait(), timeout=2)
        if change == "pid":
            case.session.state.process_id = OTHER_PID
        else:
            case.session.state.activity_epoch = object()
        # This is the replacement session's state, not the old backend's mode.
        case.session.stealth_mode = True
    finally:
        case.release_reconnect.set()
        result = await asyncio.wait_for(operation, timeout=2)

    assert result["activated"] is False
    assert result["hwnd"] == TARGET_HWND
    assert result["activation_requested"] is True
    assert result["activation_attempted"] is True
    assert result["activation_api_result"] is True
    assert result["target"]["expected_pid"] == TARGET_PID
    assert result["stealth_mode"] is True
    assert case.session.stealth_mode is True
    case.desktop.user32.SetForegroundWindow.assert_called_once_with(TARGET_HWND)
    case.backend.client.call.assert_awaited_once()


@pytest.mark.parametrize("route", ["backend", "public", "fallback"])
async def test_result_does_not_promise_foreground_after_return(foreground_case, route):
    case = foreground_case
    result = await case.invoke(route)
    # A later unrelated foreground change does not rewrite the earlier snapshot.
    case.desktop.foreground_hwnd = OTHER_HWND

    assert_report(result, activated=True, api_result=True)
    assert case.desktop.foreground_hwnd == OTHER_HWND


@pytest.mark.parametrize("route", ["backend", "public", "fallback"])
async def test_non_windows_activation_is_unavailable_without_native_effects(
    foreground_case, monkeypatch, route, capturing_mcp
):
    case = foreground_case
    monkeypatch.setattr(foreground, "os", SimpleNamespace(name="posix"))
    # Keep this global platform override inside the call, away from pytest/pathlib.
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(os, "name", "posix")
        if route == "backend":
            try:
                result = await case.backend.bring_to_front()
            except (RuntimeError, OSError, NotImplementedError):
                result = None
        else:
            selected = case.fallback if route == "fallback" else case.backend
            register_ui_tools(
                capturing_mcp,
                case.session,
                check_session_access=lambda _ctx: None,
                backend_holder={"instance": selected},
            )
            response = await capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())
            result = response.get("data")
            assert "error" in response or isinstance(result, dict), response

    if result is not None:
        assert result["activated"] is False
        assert result["activation_requested"] is True
        assert result["activation_attempted"] is False
        assert result["activation_api_result"] is None
        assert result["foreground_matches_target"] is None
    assert case.session.stealth_mode is True
    case.desktop.user32.SetForegroundWindow.assert_not_called()
    case.desktop.user32.ShowWindow.assert_not_called()
    case.desktop.user32.BringWindowToTop.assert_not_called()
    case.backend.client.call.assert_not_awaited()


@pytest.mark.parametrize("change", ["pid", "epoch", "session-id"])
async def test_public_session_change_during_client_ensure_alive_prevents_stale_connect_rpc(
    foreground_case, monkeypatch, change
):
    case = foreground_case
    ensure_entered = asyncio.Event()
    release_ensure = asyncio.Event()
    client = FlaUIBridgeClient("never-launched-fake-bridge.exe")
    # Only liveness metadata is needed; no process or stream is ever opened.
    client._process = Mock(returncode=None, pid=99999)

    async def hold_ensure_alive():
        ensure_entered.set()
        await release_ensure.wait()
        return True

    async def send_and_receive(request):
        # Return a valid response if the bug writes a stale request, so RED is
        # the forbidden RPC itself rather than a transport/protocol failure.
        assert request["method"] == "connect"
        assert request["params"] == {"pid": TARGET_PID}
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"connected": True}}

    ensure_alive_mock = AsyncMock(side_effect=hold_ensure_alive)
    transport_mock = AsyncMock(side_effect=send_and_receive)
    start_mock = AsyncMock(side_effect=AssertionError("No bridge process launch"))
    stop_mock = AsyncMock(side_effect=AssertionError("No bridge process stop"))
    with monkeypatch.context() as client_patch:
        client_patch.setattr(client, "ensure_alive", ensure_alive_mock)
        client_patch.setattr(client, "_send_and_receive", transport_mock)
        client_patch.setattr(client, "start", start_mock)
        client_patch.setattr(client, "stop", stop_mock)
        client_patch.setattr(case.backend, "_client", client)
        # FlaUIBridgeClient.call and its lock/request path remain real.  The
        # barrier is inside call's ensure_alive await, not inside a fake call.
        operation = asyncio.create_task(case.invoke("public"))
        try:
            await asyncio.wait_for(ensure_entered.wait(), timeout=2)
            assert not operation.done()
            case.desktop.user32.SetForegroundWindow.assert_called_once_with(TARGET_HWND)
            transport_mock.assert_not_awaited()
            if change == "pid":
                case.session.state.process_id = OTHER_PID
            elif change == "epoch":
                case.session.state.activity_epoch = object()
            else:
                case.session.session_id = "replacement-session"
            case.session.stealth_mode = True
        finally:
            release_ensure.set()
            result = await asyncio.wait_for(operation, timeout=2)

        transport_mock.assert_not_awaited()
        ensure_alive_mock.assert_awaited_once()
        start_mock.assert_not_awaited()
        stop_mock.assert_not_awaited()
        assert result["activated"] is False
        assert result["hwnd"] == TARGET_HWND
        assert result["activation_requested"] is True
        assert result["activation_attempted"] is True
        assert result["activation_api_result"] is True
        assert result["target"]["expected_pid"] == TARGET_PID
        assert result["stealth_mode"] is True
        assert case.session.stealth_mode is True


async def test_pywinauto_blocked_show_window_does_not_block_mcp_event_loop(
    foreground_case, monkeypatch
):
    case = foreground_case
    loop = asyncio.get_running_loop()
    show_entered = threading.Event()
    release_show = threading.Event()
    loop_progress = threading.Event()
    observed = {"show_entered": False, "loop_progress_while_blocked": False}

    def blocked_show_window(hwnd, command):
        assert hwnd == TARGET_HWND
        assert command == 9
        show_entered.set()
        assert release_show.wait(timeout=5), "Cross-thread ShowWindow barrier was not released"
        return True

    def observe_event_loop():
        try:
            if not show_entered.wait(timeout=2):
                return
            observed["show_entered"] = True
            # Only the MCP event-loop thread can execute this callback.  Keep
            # ShowWindow blocked until that thread progresses or the bound ends.
            loop.call_soon_threadsafe(loop_progress.set)
            observed["loop_progress_while_blocked"] = loop_progress.wait(timeout=2)
        finally:
            release_show.set()

    monkeypatch.setattr(
        case.desktop.user32, "ShowWindow", Mock(side_effect=blocked_show_window)
    )
    observer = threading.Thread(target=observe_event_loop, daemon=True)
    observer.start()
    try:
        result = await asyncio.wait_for(case.invoke("fallback"), timeout=8)
    finally:
        release_show.set()
        observer.join(timeout=3)

    assert not observer.is_alive(), "The bounded observer must not survive the test"
    assert observed["show_entered"] is True
    assert observed["loop_progress_while_blocked"] is True, (
        "A blocked fallback ShowWindow prevented the MCP asyncio loop from progressing"
    )
    assert_report(result, activated=True, api_result=True)
    assert result["stealth_mode"] is False
    assert case.session.stealth_mode is False


@pytest.mark.parametrize("backend_kind", ["flaui", "pywinauto"])
async def test_public_activation_can_use_current_target_with_backend_retained_from_prior_pid(
    foreground_case, capturing_mcp, backend_kind
):
    case = foreground_case
    # The session and HWND already name B; only the cached backend still names A.
    # Neither a transport double nor a native call may operate on A's target.
    if backend_kind == "flaui":
        retained = FlaUIBackend.__new__(FlaUIBackend)
        retained._process_id = OTHER_PID
        retained._stealth_mode = True
        retained._element_cache = {}

        async def bridge_call(method, params=None, **_kwargs):
            assert method == "connect", f"Unexpected bridge effect: {method}"
            assert isinstance(params, dict)
            assert params["pid"] == TARGET_PID, "A reconnect must target the pinned current PID"
            return {"connected": True}

        retained._client = SimpleNamespace(
            is_running=True,
            ensure_alive=AsyncMock(return_value=True),
            call=AsyncMock(side_effect=bridge_call),
            stop=AsyncMock(side_effect=AssertionError("No bridge process stop")),
        )
    else:
        retained = case.fallback
        retained.inner.process_id = OTHER_PID

        async def fallback_connect(pid):
            assert pid == TARGET_PID, "A reconnect must target the pinned current PID"
            retained.inner.process_id = pid

        retained.inner.connect = AsyncMock(side_effect=fallback_connect)

    register_ui_tools(
        capturing_mcp,
        case.session,
        check_session_access=lambda _ctx: None,
        backend_holder={"instance": retained},
    )
    response = await capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())

    assert "error" not in response, response
    assert_report(response["data"], activated=True, api_result=True)
    assert response["data"]["stealth_mode"] is False
    assert case.session.stealth_mode is False
    assert case.desktop.foreground_hwnd == TARGET_HWND
    assert case.desktop.user32.SetForegroundWindow.call_args_list, (
        "The current debuggee must receive the explicit activation request"
    )
    for native_call in (
        *case.desktop.user32.ShowWindow.call_args_list,
        *case.desktop.user32.BringWindowToTop.call_args_list,
        *case.desktop.user32.SetForegroundWindow.call_args_list,
    ):
        assert native_call.args[0] == TARGET_HWND, "No window effect may target the prior PID"


async def test_replacement_epoch_does_not_reuse_late_nonstealth_bridge_connection(
    foreground_case, capturing_mcp, monkeypatch
):
    case = foreground_case
    backend = FlaUIBackend("never-launched-fake-bridge.exe")
    backend._process_id = TARGET_PID
    backend._stealth_mode = True
    client = backend.client
    client._process = Mock(returncode=None, pid=99999)
    connect_written = asyncio.Event()
    release_connect_response = asyncio.Event()
    bridge = {"available": True, "stealth": True}
    applied_late_connect = []
    tree_read_modes = []

    async def ensure_fake_alive():
        return bridge["available"]

    async def stop_fake_bridge():
        bridge["available"] = False
        client._process = None
        client._mark_connection_invalid()

    async def exchange(request, *, request_is_current=None):
        # This is the wire-write boundary.  Once written, the old connect can
        # complete in the bridge even if the caller's epoch subsequently changes.
        assert bridge["available"], "No RPC may use a stopped fake bridge"
        assert request_is_current is None or request_is_current()
        method = request["method"]
        params = request["params"]
        if method == "connect":
            assert params["pid"] == TARGET_PID, "Every reconnect must keep the same target PID"
            requested_stealth = params.get("stealth", False)
            if not connect_written.is_set():
                assert requested_stealth is False
                connect_written.set()
                await release_connect_response.wait()
                applied_late_connect.append(True)
            bridge["stealth"] = requested_stealth
            result = {"connected": True}
        else:
            assert method == "get_tree", f"Unexpected bridge effect: {method}"
            tree_read_modes.append(bridge["stealth"])
            result = {
                "windows": [{"automationId": "ReplacementMain", "children": []}],
                "count": 1,
                "primary": "ReplacementMain",
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    monkeypatch.setattr(client, "ensure_alive", AsyncMock(side_effect=ensure_fake_alive))
    monkeypatch.setattr(client, "_send_and_receive", AsyncMock(side_effect=exchange))
    monkeypatch.setattr(client, "stop", AsyncMock(side_effect=stop_fake_bridge))
    monkeypatch.setattr(
        client, "start", AsyncMock(side_effect=AssertionError("No bridge process launch"))
    )
    # Eviction may safely refuse the follow-up; never discover/build/launch a
    # replacement backend in this fake-only regression.
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.backend.create_backend",
        Mock(side_effect=RuntimeError("Replacement bridge unavailable in fake-only test")),
    )
    register_ui_tools(
        capturing_mcp,
        case.session,
        check_session_access=lambda _ctx: None,
        backend_holder={"instance": backend},
    )
    operation = asyncio.create_task(
        capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())
    )
    try:
        await asyncio.wait_for(connect_written.wait(), timeout=2)
        assert not operation.done(), "The old connect response must still be pending"
        assert case.session.state.process_id == TARGET_PID
        case.session.state.activity_epoch = object()
        case.session.stealth_mode = True
    finally:
        release_connect_response.set()
        old_response = await asyncio.wait_for(operation, timeout=2)

    assert applied_late_connect, "The in-flight old connect must complete after epoch replacement"
    assert old_response.get("data", {}).get("activated") is not True
    assert case.session.state.process_id == TARGET_PID
    assert case.session.stealth_mode is True
    # Reuse the same registered dispatch and holder: registering fresh tools here
    # would hide precisely the stale-connection reuse this regression exercises.
    followup = await asyncio.wait_for(
        capturing_mcp.tools["ui_get_window_tree"](max_depth=1, max_children=1), timeout=2
    )

    assert all(mode is True for mode in tree_read_modes), (
        "The replacement session dispatched a UI operation through the old non-stealth bridge"
    )
    if "error" not in followup and followup.get("data", {}).get("status") != "BLOCKED":
        assert tree_read_modes, "Success must come from a real fake-bridge read, not empty output"
        assert followup["data"]["windows"][0]["automationId"] == "ReplacementMain"
    else:
        assert followup.get("error") or followup.get("data", {}).get("status") == "BLOCKED"
    assert case.session.stealth_mode is True


async def test_public_activation_survives_its_own_dead_bridge_cleanup_invalidation(
    foreground_case, capturing_mcp, monkeypatch
):
    from netcoredbg_mcp.process_registry import CleanupOutcome
    from netcoredbg_mcp.ui.flaui_client import WindowsOwnedProcess

    case = foreground_case
    backend = FlaUIBackend("never-launched-fake-bridge.exe")
    backend._process_id = TARGET_PID
    backend._stealth_mode = True
    client = backend.client
    old_process = Mock(returncode=0, pid=99998)
    new_process = Mock(returncode=None, pid=99999)
    client._process = old_process
    old_connection_epoch = backend._connection_epoch
    session_epoch = case.session.state.activity_epoch
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    connected_bridge_modes = []

    async def stop_fake_process(process):
        assert process is old_process, "Cleanup must stay bound to the exited bridge"
        # Real _join_stop has already invoked this client's real invalidation
        # callback before starting the fake OS cleanup below.
        cleanup_entered.set()
        await release_cleanup.wait()
        if client._process is process:
            client._process = None
        return CleanupOutcome(complete=True)

    async def launch_fake_process(**kwargs):
        assert kwargs["argv"] == ("never-launched-fake-bridge.exe",)
        assert cleanup_entered.is_set()
        return new_process

    async def exchange(request, *, request_is_current=None):
        assert client._process is new_process
        assert request_is_current is None or request_is_current()
        assert request["method"] == "connect"
        assert request["params"] == {"pid": TARGET_PID}
        connected_bridge_modes.append(False)
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"connected": True}}

    launch_mock = AsyncMock(side_effect=launch_fake_process)
    stop_process_mock = AsyncMock(side_effect=stop_fake_process)
    monkeypatch.setattr(WindowsOwnedProcess, "launch", launch_mock)
    monkeypatch.setattr(client, "_stop_process", stop_process_mock)
    monkeypatch.setattr(client, "_drain_stderr", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "_send_and_receive", AsyncMock(side_effect=exchange))
    # ensure_alive, start, _join_stop, the invalidation callback, and call all
    # remain real.  Only OS launch/drain and wire exchange are replaced.
    register_ui_tools(
        capturing_mcp,
        case.session,
        check_session_access=lambda _ctx: None,
        backend_holder={"instance": backend},
    )
    operation = asyncio.create_task(
        capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())
    )
    try:
        await asyncio.wait_for(cleanup_entered.wait(), timeout=2)
        assert not operation.done(), "The request must still be awaiting its own bridge cleanup"
        assert backend._connection_epoch is not old_connection_epoch
        assert case.session.state.activity_epoch is session_epoch
        assert case.session.state.process_id == TARGET_PID
        assert connected_bridge_modes == []
    finally:
        release_cleanup.set()
        response = await asyncio.wait_for(operation, timeout=2)
        await asyncio.gather(*client._stderr_tasks.values())

    assert "error" not in response, response
    assert_report(response["data"], activated=True, api_result=True)
    assert connected_bridge_modes == [False], "The new bridge needs exactly one non-stealth connect"
    assert response["data"]["stealth_mode"] is False
    assert case.session.stealth_mode is False
    assert case.session.state.activity_epoch is session_epoch
    launch_mock.assert_awaited_once()
    stop_process_mock.assert_awaited_once_with(old_process)
    case.desktop.user32.SetForegroundWindow.assert_called_once_with(TARGET_HWND)


@pytest.mark.parametrize("lifecycle", ["stopping", "terminal"])
@pytest.mark.parametrize("handoff", ["before-call", "during-restore-join"])
async def test_public_activation_refuses_stopping_or_terminal_dap_generation_while_running(
    foreground_case, capturing_mcp, lifecycle, handoff
):
    from netcoredbg_mcp.session.state import TransportTerminalSummary

    case = foreground_case
    generation = object()
    case.session._active_dap_run = generation
    case.session._stopping_dap_run = None
    case.session._client = SimpleNamespace(
        _run=SimpleNamespace(generation=generation),
        is_running=True,
    )
    case.session.client = case.session._client
    case.session.state.transport_terminal = None

    def publish_lifecycle_fact():
        if lifecycle == "stopping":
            case.session._stopping_dap_run = generation
        else:
            case.session.state.transport_terminal = TransportTerminalSummary(
                first_observed_signal="stdout_eof",
                adapter_pid=99998,
                process_exited=True,
                adapter_return_code=0,
                protocol_terminated=False,
                debuggee_exit_code=None,
                stdout_eof=True,
                last_dap_event_seq=None,
                last_dap_event_name=None,
                last_dap_event_body_preview=None,
                stderr_tail="",
                stderr_truncated=False,
                stderr_drained=True,
                reader_error=None,
                cleanup_outcome="complete",
            )
            case.session.client.is_running = False

    async def join_restore():
        publish_lifecycle_fact()
        return False

    if handoff == "before-call":
        publish_lifecycle_fact()
    else:
        case.session.cancel_pending_stealth_foreground_restore.side_effect = join_restore
    register_ui_tools(
        capturing_mcp,
        case.session,
        check_session_access=lambda _ctx: None,
        backend_holder={"instance": case.backend},
    )

    response = await capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())

    case.desktop.user32.ShowWindow.assert_not_called()
    case.desktop.user32.SetForegroundWindow.assert_not_called()
    case.backend.client.call.assert_not_awaited()
    assert response.get("data", {}).get("activated") is not True
    assert case.session.state.state == DebugState.RUNNING
    assert case.session.state.process_id == TARGET_PID
    assert case.session._active_dap_run is generation
    assert case.session.client._run.generation is generation
    assert case.session.stealth_mode is True
    if lifecycle == "stopping":
        assert case.session._stopping_dap_run is generation
    else:
        assert case.session.state.transport_terminal is not None

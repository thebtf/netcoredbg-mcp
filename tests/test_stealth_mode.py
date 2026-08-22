"""Tests for stealth-mode bridge contracts."""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import threading
from itertools import chain, repeat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ToolRegistry:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, annotations=None):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def _capture_metadata(method: str, width: int, height: int) -> dict[str, Any]:
    return {
        "method": method,
        "hwnd": 123,
        "client_rect": {"left": 0, "top": 0, "right": width, "bottom": height},
        "dpi": 96,
        "dpi_scale": 1.0,
        "physical_width": width,
        "physical_height": height,
        "logical_width": float(width),
        "logical_height": float(height),
    }


def _save_evidence_bundle(tmp_path, calls: list[tuple[str, str, str | None, int]]) -> Any:
    def save_screenshot_bundle(
        session_id: str,
        raw_data: bytes,
        raw_name: str,
        crop_data: bytes | None = None,
        crop_name: str | None = None,
    ):
        calls.append((session_id, raw_name, crop_name, threading.get_ident()))
        raw_path = tmp_path / raw_name
        raw_path.write_bytes(raw_data)
        if crop_data is None:
            assert crop_name is None
            return raw_path, None
        assert crop_name is not None
        crop_path = tmp_path / crop_name
        crop_path.write_bytes(crop_data)
        return raw_path, crop_path

    return save_screenshot_bundle


def test_bridge_registers_save_restore_foreground_commands() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")

    assert '["save_foreground"] = StealthCommands.SaveForeground' in router
    assert '["restore_foreground"] = StealthCommands.RestoreForeground' in router


def test_bridge_registers_flash_focus_send_keys_command() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")

    assert '["flash_focus_send_keys"] = StealthCommands.FlashFocusSendKeys' in router


def test_bridge_stealth_foreground_round_trip_contract() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "StealthCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "GetForegroundWindow()" in command
    assert '["hwnd"] = hwnd.ToInt64()' in command
    assert '@params?["hwnd"]?.GetValue<long>()' in command
    assert "SetForegroundWindow(hwnd)" in command
    assert '["restored"] = restored' in command


def test_bridge_flash_focus_send_keys_contract() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "StealthCommands.cs").read_text(
        encoding="utf-8"
    )
    input_commands = (PROJECT_ROOT / "bridge" / "Commands" / "InputCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "public static JsonNode FlashFocusSendKeys" in command
    assert "var savedForeground = GetForegroundWindow();" in command
    assert "SetForegroundWindow(targetHwnd)" in command
    assert "InputCommands.SendKeysWithoutForeground" in command
    assert "SetForegroundWindow(savedForeground)" in command
    assert '["sent"] = true' in command
    assert '["flash_ms"]' in command
    assert "internal static JsonObject SendKeysWithoutForeground" in input_commands


def test_bridge_send_keys_routes_to_flash_focus_only_in_stealth_mode() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "InputCommands.cs").read_text(
        encoding="utf-8"
    )

    send_keys_start = command.index("public static JsonNode SendKeys(")
    batch_start = command.index("public static JsonNode SendKeysBatch(")
    send_keys_body = command[send_keys_start:batch_start]
    batch_body = command[batch_start : command.index("public static JsonNode SetValue(")]

    assert "if (JsonRpcHandler.Stealth)" in send_keys_body
    assert "return StealthCommands.FlashFocusSendKeys(@params, automation, mainWindow);" in (
        send_keys_body
    )
    assert "EnsureForeground(mainWindow);" in send_keys_body
    assert send_keys_body.index("if (JsonRpcHandler.Stealth)") < send_keys_body.index(
        "EnsureForeground(mainWindow);"
    )
    assert "if (JsonRpcHandler.Stealth)" in batch_body
    assert "return StealthCommands.FlashFocusSendKeysBatch(@params, automation, mainWindow);" in (
        batch_body
    )
    assert "ensureForegroundBeforeEach: false" in batch_body
    assert "internal static JsonObject SendKeysBatchWithoutForeground" in command


def test_bridge_click_routes_stealth_to_invoke_or_flash_focus_click() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
        encoding="utf-8"
    )

    click_start = command.index("public static JsonNode Click(")
    right_click_start = command.index("public static JsonNode RightClick(")
    click_body = command[click_start:right_click_start]
    automation_start = command.index("private static JsonNode ClickByAutomationId(")
    coordinates_start = command.index("private static (int x, int y) GetCoordinates(")
    automation_body = command[automation_start:coordinates_start]

    assert "if (JsonRpcHandler.Stealth)" in click_body
    assert "return FlashFocusClick(x.Value, y.Value, mainWindow);" in click_body
    assert "EnsureForeground(mainWindow);" in click_body
    assert "invokePattern.Invoke();" in automation_body
    assert '["method"] = "InvokePattern"' in automation_body
    expected_saved_foreground = (
        "var savedForeground = JsonRpcHandler.Stealth ? GetForegroundWindow() : IntPtr.Zero;"
    )
    assert expected_saved_foreground in automation_body
    assert "SetForegroundWindow(savedForeground);" in automation_body
    assert "if (JsonRpcHandler.Stealth)" in automation_body
    assert (
        "return FlashFocusClick(center.X, center.Y, mainWindow, automationId);" in automation_body
    )
    assert "private static JsonObject FlashFocusClick" in command
    assert "GetForegroundWindow() != targetHwnd" in command
    assert "flash-focus click could not activate the debuggee window safely" in command
    assert "SetForegroundWindow(savedForeground)" in command


def test_bridge_blocks_unsupported_stealth_coordinate_mouse_commands() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
        encoding="utf-8"
    )

    assert 'RejectStealthMouseInput("right_click");' in command
    assert 'RejectStealthMouseInput("double_click");' in command
    assert 'RejectStealthMouseInput("drag");' in command
    assert "Use ui_click with automationId or ui_bring_to_front first." in command


def test_bridge_coordinate_mouse_input_verifies_foreground_activation() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
        encoding="utf-8"
    )

    ensure_start = command.index("internal static void EnsureForeground(")
    ensure_body = command[ensure_start:]

    assert "AttachThreadInput(currentThread, threadId, true)" in ensure_body
    assert "BringWindowToTop(hwnd);" in ensure_body
    assert "WaitForForeground(hwnd)" in ensure_body
    assert "if (!WaitForForeground(hwnd))" in ensure_body
    assert "coordinate mouse input could not activate the debuggee window safely" in ensure_body


def test_bridge_screenshot_uses_printwindow_in_stealth_mode() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "private const uint PW_RENDERFULLCONTENT = 0x00000002;" in command
    assert "if (JsonRpcHandler.Stealth)" in command
    assert "PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)" in command
    assert '["base64"] = base64' in command
    assert "Capture.Rectangle(rect)" in command
    assert command.index("if (JsonRpcHandler.Stealth)") < command.index("Capture.Rectangle(rect)")


def test_bridge_resize_window_returns_unit_labelled_post_resize_geometry() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "TransformCommands.cs").read_text(
        encoding="utf-8"
    )
    resize_start = command.index("public static JsonNode ResizeWindow")
    resize_body = command[resize_start:]

    required_fields = (
        '["request"]',
        '["geometry"]',
        '["target_comparability"]',
        '["uia_bounds"]',
        '["window_bounds"]',
        '["client_bounds"]',
        '["dpi"]',
        '["dpi_scale"]',
        '"MATCHED"',
        '"MISMATCH"',
        '"UNAVAILABLE"',
        '"physical_px"',
        '"dip"',
        '"uia_element_bounds"',
        '"UIA.TransformPattern.Resize"',
        '"UIA.BoundingRectangle"',
        '"GetWindowRect"',
        '"GetClientRect"',
        '"POST_RESIZE_GEOMETRY_UNAVAILABLE"',
    )
    assert all(field in resize_body for field in required_fields) and (
        resize_body.index("pattern.Resize(width, height);") < resize_body.index('["geometry"]')
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("comparability", ("MATCHED", "MISMATCH", "UNAVAILABLE"))
async def test_ui_resize_window_preserves_bridge_target_comparability(comparability: str) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value={"resized": True, "target_comparability": {"status": comparability}}
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda _ctx: None)
        response = await registry.tools["ui_resize_window"](
            SimpleNamespace(), width=800, height=600
        )

    backend._client.call.assert_awaited_once_with("resize_window", {"width": 800, "height": 600})
    assert response["data"] == {"resized": True, "target_comparability": {"status": comparability}}


@pytest.mark.asyncio
async def test_public_resize_uia_readback_then_evidence_screenshot_at_150_dpi(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1080), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        side_effect=[
            {
                "resized": True,
                "target_comparability": {"status": "MATCHED"},
                "geometry": {
                    "status": "available",
                    "uia_bounds": {
                        "physical_px": {"left": 0, "top": 0, "right": 1536, "bottom": 1080},
                        "dip": {"left": 0, "top": 0, "right": 1024, "bottom": 720},
                    },
                },
            },
            _bridge_lossless_screenshot(raw_png, width=1536, height=1080),
        ]
    )
    bundle_calls: list[tuple[str, str, str | None, int]] = []
    session = _stealth_evidence_session(tmp_path, bundle_calls)
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda _ctx: None)
        resize = await registry.tools["ui_resize_window"](
            SimpleNamespace(), width=1536, height=1080
        )
        screenshot = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            max_width=640,
            expected_hwnd=777,
            expected_physical_width=1536,
            expected_physical_height=1080,
        )

    metadata = json.loads(screenshot[1].text)
    assert resize["data"]["target_comparability"]["status"] == "MATCHED"
    assert resize["data"]["geometry"]["uia_bounds"]["dip"]["right"] == 1024
    assert (
        metadata["target_comparability"]["status"],
        metadata["raw_width"],
        metadata["width"],
    ) == (
        "MATCHED",
        1536,
        640,
    )
    assert metadata["capture_metadata"]["dpi_scale"] == 1.5
    assert backend._client.call.await_args_list == [
        call("resize_window", {"width": 1536, "height": 1080}),
        call("screenshot", {"evidence": True}),
    ]


def test_bridge_stealth_screenshot_limits_provenance_to_evidence() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    screenshot_start = command.index("public static JsonNode Screenshot(")
    resolve_start = command.index("private static IntPtr ResolveTargetHwnd")
    preview_start = command.index("private static JsonObject CaptureWithPrintWindow")
    evidence_start = command.index("private static JsonObject CaptureEvidenceWithPrintWindow")
    evidence_end = command.index(
        "private static (int width, int height) GetWindowSize", evidence_start
    )
    screenshot = command[screenshot_start:resolve_start]
    preview_capture = command[preview_start:evidence_start]
    evidence_capture = command[evidence_start:evidence_end]

    assert 'var evidence = @params?["evidence"]?.GetValue<bool>() ?? false;' in screenshot
    assert (
        "return evidence ? CaptureEvidenceWithPrintWindow(hwnd) : CaptureWithPrintWindow(hwnd);"
        in screenshot
    )
    assert "ReadCaptureSnapshot" not in preview_capture
    assert "GetDpiForWindow" not in preview_capture
    assert "CaptureWithFlashFocusBitBlt(hwnd, width, height)" in preview_capture
    assert "var printWindowBefore = ReadCaptureSnapshot(hwnd);" in evidence_capture
    assert "var printWindowAfter = ReadCaptureSnapshot(hwnd);" in evidence_capture
    assert "EnsureStableCaptureSnapshot(printWindowBefore, printWindowAfter);" in evidence_capture
    assert "CaptureEvidenceWithFlashFocusBitBlt" not in evidence_capture
    assert "BitBlt(" not in evidence_capture


def test_bridge_screenshot_falls_back_to_flash_focus_bitblt_when_blank() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "private const double BlankFrameVarianceThreshold = 0.01;" in command
    assert "private static bool IsBlankFrame(Bitmap bitmap)" in command
    assert "CaptureWithFlashFocusBitBlt(hwnd, width, height)" in command
    assert "GetForegroundWindow()" in command
    assert "SetForegroundWindow(hwnd)" in command
    assert "BitBlt(" in command
    assert 'result["fallback"] = "flash-focus";' in command
    assert "SetForegroundWindow(savedForeground)" in command


def test_bridge_evidence_requires_printwindow_while_preview_remains_best_effort() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    preview_start = command.index("private static Bitmap CaptureWithFlashFocusBitBlt")
    raster_start = command.index("private static Bitmap CaptureBitmapWithBitBlt")
    preview_capture = command[preview_start:raster_start]
    evidence_start = command.index("private static JsonObject CaptureEvidenceWithPrintWindow")
    evidence_end = command.index(
        "private static (int width, int height) GetWindowSize", evidence_start
    )
    evidence_capture = command[evidence_start:evidence_end]

    assert "SetForegroundWindow(hwnd);" in preview_capture
    assert "foregroundSet" not in preview_capture
    assert "GetForegroundWindow() != hwnd" not in preview_capture
    assert "CaptureEvidenceWithFlashFocusBitBlt" not in evidence_capture
    assert "Evidence capture requires a PrintWindow raster" in evidence_capture


def test_bridge_stable_printwindow_evidence_skips_blank_frame_fallback() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    capture_start = command.index("private static JsonObject CaptureEvidenceWithPrintWindow")
    capture_end = command.index(
        "private static (int width, int height) GetWindowSize", capture_start
    )
    evidence_capture = command[capture_start:capture_end]

    assert "var printWindowResult = EncodeBitmap(printWindowBitmap);" in evidence_capture
    assert "IsBlankFrame(printWindowBitmap)" not in evidence_capture
    assert "CaptureEvidenceWithFlashFocusBitBlt" not in evidence_capture


def test_bridge_printwindow_rejects_mismatched_capture_snapshots() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "var printWindowBefore = ReadCaptureSnapshot(hwnd);" in command
    assert "var printWindowAfter = ReadCaptureSnapshot(hwnd);" in command
    assert "EnsureStableCaptureSnapshot(printWindowBefore, printWindowAfter);" in command


def test_bridge_evidence_does_not_capture_a_bitblt_snapshot() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    evidence_start = command.index("private static JsonObject CaptureEvidenceWithPrintWindow")
    evidence_end = command.index(
        "private static (int width, int height) GetWindowSize", evidence_start
    )
    evidence_capture = command[evidence_start:evidence_end]

    assert "CaptureEvidenceWithFlashFocusBitBlt" not in evidence_capture
    assert "CaptureBitmapWithBitBlt" not in evidence_capture


def test_bridge_connect_stores_stealth_state_and_exposes_get_state() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")
    elements = (PROJECT_ROOT / "bridge" / "Commands" / "ElementCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "private static bool _stealth;" in router
    assert "internal static bool Stealth" in router
    assert '["get_state"] = GetState' in router
    assert '["stealth"] = Stealth' in router
    assert "_stealth = false;" in router
    assert 'var requestedStealth = @params?["stealth"]?.GetValue<bool>() ?? false;' in elements
    assert "JsonRpcHandler.Stealth = requestedStealth;" in elements
    assert elements.index("var window = SelectPrimaryWindow") < elements.index(
        "JsonRpcHandler.Stealth = requestedStealth;"
    )


def test_bridge_window_ensure_foreground_skips_only_in_stealth_mode() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "WindowCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "if (JsonRpcHandler.Stealth)" in command
    assert 'Program.Log("stealth: skipping foreground");' in command
    assert "SetForegroundWindow(hwnd)" in command
    assert "ShowWindow(hwnd, showCmd)" in command


def test_bridge_foreground_helpers_all_check_stealth_before_set_foreground() -> None:
    for relative_path in [
        "bridge/Commands/InputCommands.cs",
        "bridge/Commands/FocusCommands.cs",
        "bridge/Commands/ClickCommands.cs",
    ]:
        command = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        guard_index = command.index("if (JsonRpcHandler.Stealth)")
        foreground_index = command.index("SetForegroundWindow(hwnd)")

        assert guard_index < foreground_index, relative_path
        assert 'Program.Log("stealth: skipping foreground");' in command


def test_session_manager_defaults_stealth_mode_false(mock_netcoredbg_path) -> None:
    from netcoredbg_mcp.session.manager import SessionManager

    session = SessionManager("/fake/netcoredbg")

    assert session.stealth_mode is False


def test_session_manager_launch_stores_stealth_mode_source_contract() -> None:
    manager = (PROJECT_ROOT / "src" / "netcoredbg_mcp" / "session" / "manager.py").read_text(
        encoding="utf-8"
    )

    assert "self._stealth_mode = False" in manager
    assert "def stealth_mode(self) -> bool:" in manager
    assert "stealth_mode: bool = False" in manager
    assert "self._stealth_mode = stealth_mode" in manager
    assert "saved_foreground_hwnd = get_foreground_window() if stealth_mode else None" in manager
    assert "_restore_foreground_after_stealth_launch(saved_foreground_hwnd)" in manager
    assert "asyncio.create_task(" in manager
    assert "if debuggee_pid is None:" in manager


@pytest.mark.asyncio
async def test_session_manager_stealth_launch_restores_while_safe(
    monkeypatch,
) -> None:
    from netcoredbg_mcp.session.manager import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._state = SimpleNamespace(process_id=42)
    restored: list[int] = []

    foregrounds = iter([900, 900, 777])
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.get_foreground_window",
        lambda: next(foregrounds),
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.get_window_process_id",
        lambda hwnd: 42 if hwnd == 900 else 100,
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.restore_foreground_window",
        lambda hwnd: restored.append(hwnd) is None or True,
    )
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.session.manager.asyncio.sleep", sleep)

    await manager._restore_foreground_after_stealth_launch(123)

    assert restored == [123, 123]
    assert sleep.await_count == 2


def test_session_manager_stealth_restore_waits_for_process_id(monkeypatch) -> None:
    from netcoredbg_mcp.session.manager import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._state = SimpleNamespace(process_id=None)

    restore = MagicMock(return_value=True)
    monkeypatch.setattr("netcoredbg_mcp.session.manager.restore_foreground_window", restore)

    assert manager._restore_foreground_if_safe(123) is True
    restore.assert_not_called()


def test_session_manager_stealth_restore_waits_for_known_foreground_owner(monkeypatch) -> None:
    from netcoredbg_mcp.session.manager import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._state = SimpleNamespace(process_id=42)

    monkeypatch.setattr("netcoredbg_mcp.session.manager.get_foreground_window", lambda: 900)
    monkeypatch.setattr("netcoredbg_mcp.session.manager.get_window_process_id", lambda hwnd: None)
    restore = MagicMock(return_value=True)
    monkeypatch.setattr("netcoredbg_mcp.session.manager.restore_foreground_window", restore)

    assert manager._restore_foreground_if_safe(123) is True
    restore.assert_not_called()


@pytest.mark.asyncio
async def test_session_manager_stop_cancels_stealth_foreground_restore_task() -> None:
    from netcoredbg_mcp.session.manager import DebugState, SessionManager

    async def sleepy_restore() -> None:
        await asyncio.sleep(60)

    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    task = asyncio.create_task(sleepy_restore())
    manager._stealth_foreground_restore_task = task
    manager._client = SimpleNamespace(is_running=False)
    manager._process_registry = SimpleNamespace(cleanup_all=MagicMock())
    manager._session_id = None
    manager._state.state = DebugState.RUNNING
    manager._state_listeners = []
    manager._initialized_event = asyncio.Event()
    manager._execution_event = asyncio.Event()
    manager._runtime_smoke = SimpleNamespace(reset=MagicMock())
    manager._output_bytes = 1

    result = await manager.stop()

    assert result == {"success": True}
    assert task.cancelled()
    assert manager._stealth_foreground_restore_task is None
    manager._process_registry.cleanup_all.assert_called_once_with()
    manager._runtime_smoke.reset.assert_called_once_with()


@pytest.mark.asyncio
async def test_session_manager_stealth_launch_defers_foreground_restore_until_ui_ready(
    tmp_path,
    monkeypatch,
) -> None:
    """RED for #251: launch must not restore foreground before a UI readiness signal exists."""
    from netcoredbg_mcp.dap import DAPResponse
    from netcoredbg_mcp.session.manager import DebugState, SessionManager

    class FakeLaunchClient:
        is_running = True
        netcoredbg_path = str(tmp_path / "netcoredbg.exe")
        capabilities: dict[str, Any] = {}

        async def set_exception_breakpoints(
            self,
            filters: list[str] | None = None,
        ) -> DAPResponse:
            return DAPResponse(1, 1, True, "setExceptionBreakpoints")

        async def launch(self, **_kwargs: Any) -> DAPResponse:
            return DAPResponse(1, 1, True, "launch")

        async def configuration_done(self) -> DAPResponse:
            return DAPResponse(1, 1, True, "configurationDone")

    program = tmp_path / "WpfSmokeApp.dll"
    program.write_bytes(b"")

    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    manager._client = FakeLaunchClient()
    manager._state.state = DebugState.IDLE
    manager._initialized_event = asyncio.Event()
    manager._execution_event = asyncio.Event()
    manager._initialized_event.set()
    manager._breakpoints = SimpleNamespace(
        function_breakpoints=[],
        file_breakpoints={},
        hit_counts={},
    )
    manager._state_listeners = []
    manager._last_version_warning = None
    manager._stealth_mode = False
    manager._stealth_foreground_restore_task = None
    manager._last_launch_config = None
    manager._session_id = None
    manager._sync_all_breakpoints = AsyncMock()
    manager._enable_hot_reload_if_supported = AsyncMock()
    manager.check_dbgshim_compatibility = MagicMock(return_value=None)

    foregrounds = chain([111], repeat(222))
    restore_calls: list[int] = []
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.get_foreground_window",
        lambda: next(foregrounds),
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.get_window_process_id",
        lambda hwnd: None,
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.session.manager.restore_foreground_window",
        lambda hwnd: restore_calls.append(hwnd) is None or True,
    )

    result = None
    try:
        result = await manager.launch(
            program=str(program),
            pre_build=False,
            stealth_mode=True,
        )
    finally:
        task = getattr(manager, "_stealth_foreground_restore_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert result == {"success": True, "program": str(program)}
    assert restore_calls == []


def test_session_manager_stealth_launch_stops_after_user_moves_foreground(monkeypatch) -> None:
    from netcoredbg_mcp.session.manager import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._state = SimpleNamespace(process_id=42)

    monkeypatch.setattr("netcoredbg_mcp.session.manager.get_foreground_window", lambda: 777)
    monkeypatch.setattr("netcoredbg_mcp.session.manager.get_window_process_id", lambda hwnd: 100)

    restore = MagicMock(return_value=True)
    monkeypatch.setattr("netcoredbg_mcp.session.manager.restore_foreground_window", restore)

    assert manager._restore_foreground_if_safe(123) is False
    restore.assert_not_called()


def test_session_manager_allows_explicit_stealth_exit_source_contract() -> None:
    manager = (PROJECT_ROOT / "src" / "netcoredbg_mcp" / "session" / "manager.py").read_text(
        encoding="utf-8"
    )

    assert "@stealth_mode.setter" in manager
    assert "self._stealth_mode = value" in manager


def test_flaui_backend_bring_to_front_reconnects_bridge_without_stealth() -> None:
    backend = (PROJECT_ROOT / "src" / "netcoredbg_mcp" / "ui" / "flaui_client.py").read_text(
        encoding="utf-8"
    )

    assert "async def bring_to_front(self) -> dict[str, Any]:" in backend
    assert "get_hwnd_for_pid(self._process_id)" in backend
    assert "SetForegroundWindow(hwnd)" in backend
    assert "await self.connect(self._process_id, stealth=False)" in backend
    assert '["activated"] = activated' not in backend
    assert '"activated": activated' in backend


@pytest.mark.asyncio
async def test_flaui_backend_bring_to_front_blocks_when_no_visible_window(monkeypatch) -> None:
    """#266 overlap guard: explicit stealth exit must be blocked without a real UI stand."""
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda pid: None)

    with pytest.raises(RuntimeError, match="No visible window for process 42"):
        await backend.bring_to_front()


@pytest.mark.asyncio
async def test_ui_get_window_tree_reconnects_same_pid_after_bridge_disconnect() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    class DisconnectingBackend(FlaUIBackend):
        process_id = 42

        def __init__(self) -> None:
            self.connected = False
            self.connect = AsyncMock(side_effect=self._connect)
            self.bring_to_front = AsyncMock(return_value={"activated": True})

        async def _connect(self, pid: int, stealth: bool = False) -> None:
            self.connected = True

        async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> dict:
            if not self.connected:
                raise RuntimeError(
                    "FlaUI bridge error: Internal error: Not connected. Call 'connect' first."
                )
            return {"windows": [{"automationId": "MainWindow"}], "count": 1}

    backend = DisconnectingBackend()
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        bring_response = await registry.tools["ui_bring_to_front"](SimpleNamespace())
        tree_response = await registry.tools["ui_get_window_tree"]()

    assert bring_response["data"]["activated"] is True
    assert "error" not in tree_response
    assert tree_response["data"]["count"] == 1
    assert call(42, stealth=False) in backend.connect.await_args_list


@pytest.mark.asyncio
async def test_ui_get_window_tree_reconnects_same_pid_without_foreground_activation() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    class PassiveDisconnectingBackend(FlaUIBackend):
        process_id = 42

        def __init__(self) -> None:
            self.connected = False
            self.connect = AsyncMock(side_effect=self._connect)
            self.bring_to_front = AsyncMock(return_value={"activated": True})

        async def _connect(self, pid: int, stealth: bool = False) -> None:
            self.connected = True

        async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> dict:
            if not self.connected:
                raise RuntimeError(
                    "FlaUI bridge error: Internal error: Not connected. Call 'connect' first."
                )
            return {"windows": [{"automationId": "MainWindow"}], "count": 1}

    backend = PassiveDisconnectingBackend()
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        tree_response = await registry.tools["ui_get_window_tree"]()

    backend.connect.assert_awaited_once_with(42, stealth=True)
    backend.bring_to_front.assert_not_awaited()
    assert session.stealth_mode is True
    assert "error" not in tree_response
    assert tree_response["data"]["count"] == 1


@pytest.mark.asyncio
async def test_ui_find_element_reconnects_same_pid_after_bridge_stop() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = SimpleNamespace(is_running=False)
    backend._process_id = 42
    backend._element_cache = {"stale": {"runtimeId": "old-bridge"}}

    async def connect(pid: int, stealth: bool = False) -> None:
        backend._client.is_running = True
        backend._process_id = pid

    async def find_element(**_: Any) -> dict[str, Any]:
        if not backend._client.is_running:
            raise RuntimeError(
                "FlaUI bridge error: Internal error: Not connected. Call 'connect' first."
            )
        return {"automationId": "btnInvoke", "name": "Invoke", "controlType": "Button"}

    backend.connect = AsyncMock(side_effect=connect)
    backend.find_element = AsyncMock(side_effect=find_element)

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_find_element"](automation_id="btnInvoke")

    backend.connect.assert_awaited_once_with(42, stealth=True)
    assert "error" not in response
    assert response["data"]["automationId"] == "btnInvoke"


@pytest.mark.asyncio
async def test_start_debug_passes_stealth_mode_to_session_launch(tmp_path) -> None:
    from netcoredbg_mcp.tools.debug import register_debug_tools

    registry = ToolRegistry()
    session = SimpleNamespace(
        project_path=str(tmp_path),
        state=SimpleNamespace(state="idle"),
        validate_program=MagicMock(side_effect=lambda program, must_exist=True: program),
        validate_path=MagicMock(side_effect=lambda path, must_exist=True: path),
        launch=AsyncMock(return_value={"success": True, "program": "app.dll"}),
    )

    async def notify_state_changed(ctx):
        return None

    async def resolve_project_root(ctx, session):
        session.project_path = str(tmp_path)

    register_debug_tools(
        registry,
        session,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=notify_state_changed,
        check_session_access=lambda ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=resolve_project_root,
    )

    ctx = SimpleNamespace(
        report_progress=AsyncMock(),
        warning=AsyncMock(),
        info=AsyncMock(),
    )

    await registry.tools["start_debug"](
        ctx,
        program="app.dll",
        pre_build=False,
        stealth_mode=True,
    )
    assert session.launch.await_args.kwargs["stealth_mode"] is True

    session.launch.reset_mock()

    await registry.tools["start_debug"](
        ctx,
        program="app.dll",
        pre_build=False,
    )
    assert session.launch.await_args.kwargs["stealth_mode"] is False


@pytest.mark.asyncio
async def test_flaui_backend_connect_sends_stealth_flag_to_bridge() -> None:
    from netcoredbg_mcp.ui.flaui_client import CONNECT_CALL_TIMEOUT_SECONDS, FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._client = MagicMock()
    backend._client.ensure_alive = AsyncMock(return_value=True)
    backend._client.call = AsyncMock(return_value={"connected": True, "title": "WPF Smoke"})
    backend._element_cache = {}
    backend._process_id = None

    await backend.connect(42, stealth=True)

    backend._client.call.assert_awaited_once_with(
        "connect",
        {"pid": 42, "stealth": True},
        timeout=CONNECT_CALL_TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_ui_tools_connect_flaui_backend_with_session_stealth_mode() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = None
    backend.connect = AsyncMock()
    backend.get_window_tree = AsyncMock(return_value={"windows": [], "count": 0})

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        await registry.tools["ui_get_window_tree"]()

    backend.connect.assert_awaited_once_with(42, stealth=True)


@pytest.mark.asyncio
async def test_ui_get_window_tree_timeout_returns_structured_blocked() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    async def slow_window_tree(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"windows": [], "count": 0}

    backend = SimpleNamespace(
        process_id=42,
        get_window_tree=AsyncMock(side_effect=slow_window_tree),
        bring_to_front=AsyncMock(return_value={"activated": True}),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with (
        patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend),
        patch("netcoredbg_mcp.tools.ui.UI_TREE_DISCOVERY_TIMEOUT_SECONDS", 0.001),
    ):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_get_window_tree"](max_depth=1)
        followup = await registry.tools["ui_bring_to_front"](SimpleNamespace())

    assert "error" not in response
    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "ui tree discovery timed out"
    assert response["data"]["timeout_seconds"] == 0.001
    assert response["data"]["debuggee_process_id"] == 42
    assert response["data"]["ui_backend"] == "SimpleNamespace"
    assert response["data"]["error"] == "TimeoutError"
    assert "ui_wait_for" in response["data"]["next_step"]
    assert followup["data"]["activated"] is True


@pytest.mark.asyncio
async def test_ui_get_window_tree_connect_timeout_is_not_discovery_timeout() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(
        process_id=None,
        get_window_tree=AsyncMock(return_value={"windows": [], "count": 0}),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with (
        patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend),
        patch(
            "netcoredbg_mcp.ui.backend.connect_backend",
            AsyncMock(side_effect=asyncio.TimeoutError("connect timeout")),
        ) as connect_backend,
    ):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_get_window_tree"](max_depth=1)

    connect_backend.assert_awaited_once_with(backend, 42, stealth_mode=True)
    backend.get_window_tree.assert_not_awaited()
    assert response["error"] == "connect timeout"
    assert "data" not in response


@pytest.mark.asyncio
async def test_ui_bring_to_front_disables_session_stealth_mode() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    backend = SimpleNamespace(
        process_id=42,
        bring_to_front=AsyncMock(return_value={"activated": True, "hwnd": 123}),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()
    ctx = SimpleNamespace()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_bring_to_front"](ctx)

    backend.bring_to_front.assert_awaited_once_with()
    assert session.stealth_mode is False
    assert response["data"]["activated"] is True
    assert response["data"]["stealth_mode"] is False


@pytest.mark.asyncio
async def test_ui_click_stealth_response_includes_mode() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(return_value={"clicked": True, "method": "InvokePattern"})
    backend._element_cache = {}
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_click"](SimpleNamespace(), automation_id="btnInvoke")

    backend.client.call.assert_awaited_once_with("click", {"automationId": "btnInvoke"})
    assert response["data"]["mode"] == "stealth"
    assert response["data"]["method"] == "InvokePattern"


@pytest.mark.asyncio
async def test_ui_send_keys_stealth_response_includes_flash_focus_mode() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(return_value={"sent": True, "flash_ms": 12})
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_send_keys"](SimpleNamespace(), keys="abc")

    backend.client.call.assert_awaited_once_with("send_keys", {"keys": "abc"})
    assert response["data"]["mode"] == "flash-focus"
    assert response["data"]["flash_ms"] == 12


@pytest.mark.asyncio
async def test_ui_take_screenshot_stealth_uses_bridge_screenshot_metadata() -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    png = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(png, format="PNG")
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value={
            "base64": base64.b64encode(png.getvalue()).decode("ascii"),
            "width": 8,
            "height": 8,
            "method": "PrintWindow",
        }
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id=None,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        content = await registry.tools["ui_take_screenshot"](SimpleNamespace(), format="png")

    backend.client.call.assert_awaited_once_with("screenshot", {})
    metadata = json.loads(content[1].text)
    assert metadata["mode"] == "stealth"
    assert metadata["method"] == "PrintWindow"
    assert metadata["evidence_grade"] == "preview_only"


@pytest.mark.asyncio
async def test_ui_take_screenshot_evidence_persists_raw_png_with_hash(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    capture_metadata = {
        "method": "PrintWindow",
        "hwnd": 123,
        "client_rect": {"left": 0, "top": 0, "right": 8, "bottom": 6},
        "dpi": 96,
        "dpi_scale": 1.0,
        "physical_width": 8,
        "physical_height": 6,
        "logical_width": 8.0,
        "logical_height": 6.0,
    }

    bundle_calls: list[tuple[str, str, str | None, int]] = []
    save_screenshot_bundle = _save_evidence_bundle(tmp_path, bundle_calls)

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="evidence-session",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=save_screenshot_bundle,
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )
    registry = ToolRegistry()
    with (
        patch("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", return_value=123),
        patch(
            "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
            return_value=(raw_png, 8, 6, capture_metadata),
        ),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        content = await registry.tools["ui_take_screenshot"](SimpleNamespace(), evidence=True)
    assert isinstance(content, list), content
    metadata = json.loads(content[1].text)
    assert metadata["evidence_grade"] == "lossless_raster"
    assert metadata["target_comparability"] == {"status": "UNASSERTED"}
    assert metadata["retention"] == "stop_cleanup_or_stale_gc_after_4h"
    assert metadata["capture_metadata"] == capture_metadata
    assert metadata["raw_mime"] == "image/png"
    assert (metadata["raw_width"], metadata["raw_height"]) == (8, 6)
    assert Path(metadata["raw_path"]).read_bytes() == raw_png
    assert metadata["raw_sha256"] == hashlib.sha256(raw_png).hexdigest()
    assert Path(metadata["hd_path"]).exists()
    assert len(bundle_calls) == 1
    assert bundle_calls[0][0] == "evidence-session"
    assert bundle_calls[0][1].startswith("evidence_")
    assert bundle_calls[0][2] is None


@pytest.mark.asyncio
async def test_ui_take_screenshot_evidence_persists_raw_derived_crop(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    image = Image.new("RGB", (5, 4))
    image.putpixel((1, 1), (12, 34, 56))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw_png = buffer.getvalue()

    bundle_calls: list[tuple[str, str, str | None, int]] = []
    save_screenshot_bundle = _save_evidence_bundle(tmp_path, bundle_calls)

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="crop-session",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=save_screenshot_bundle,
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )
    registry = ToolRegistry()
    with (
        patch("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", return_value=123),
        patch(
            "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
            return_value=(raw_png, 5, 4, _capture_metadata("PrintWindow", 5, 4)),
        ),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        content = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            crop_x=1,
            crop_y=1,
            crop_width=3,
            crop_height=2,
        )
    assert isinstance(content, list), content
    metadata = json.loads(content[1].text)
    crop_path = Path(metadata["crop_path"])
    assert metadata["crop_rect"] == {"x": 1, "y": 1, "width": 3, "height": 2}
    assert (metadata["crop_width"], metadata["crop_height"]) == (3, 2)
    assert metadata["crop_sha256"] == hashlib.sha256(crop_path.read_bytes()).hexdigest()
    with Image.open(crop_path) as crop:
        assert crop.size == (3, 2)
        assert crop.getpixel((0, 0)) == (12, 34, 56)
    assert len(bundle_calls) == 1
    assert bundle_calls[0][0] == "crop-session"
    assert bundle_calls[0][1].startswith("evidence_")
    assert bundle_calls[0][2] is not None
    assert bundle_calls[0][2].startswith("evidence_crop_")


@pytest.mark.asyncio
async def test_ui_take_screenshot_rejects_incomplete_evidence_crop() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="crop-session",
        temp_manager=SimpleNamespace(),
    )
    registry = ToolRegistry()
    register_ui_tools(registry, session, check_session_access=lambda ctx: None)

    response = await registry.tools["ui_take_screenshot"](
        SimpleNamespace(), evidence=True, crop_x=0, crop_y=0, crop_width=1
    )

    assert (
        response["error"] == "crop_x, crop_y, crop_width, and crop_height must be supplied together"
    )


@pytest.mark.asyncio
async def test_ui_take_screenshot_rejects_out_of_bounds_evidence_crop(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 255, 255)).save(buffer, format="PNG")
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="bounds-session",
        temp_manager=SimpleNamespace(
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name
        ),
    )
    registry = ToolRegistry()
    with (
        patch("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", return_value=123),
        patch(
            "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
            return_value=(buffer.getvalue(), 2, 2, _capture_metadata("PrintWindow", 2, 2)),
        ),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        response = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            crop_x=1,
            crop_y=0,
            crop_width=2,
            crop_height=1,
        )

    assert response["error"] == "Crop rectangle must be positive and within image bounds"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_ui_take_screenshot_stealth_evidence_uses_bridge_capture_provenance_not_pid_window(
    tmp_path,
) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value={
            "base64": base64.b64encode(raw_png).decode("ascii"),
            "width": 8,
            "height": 8,
            "method": "PrintWindow",
            "hwnd": 777,
            "client_rect": {
                "left": 0,
                "top": 0,
                "right": 6,
                "bottom": 4,
                "unit": "physical_px",
                "coordinate_space": "client",
                "source_api": "GetClientRect",
            },
            "dpi": 144,
        }
    )

    bundle_calls: list[tuple[str, str, str | None, int]] = []
    save_screenshot_bundle = _save_evidence_bundle(tmp_path, bundle_calls)

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id="stealth-evidence-session",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=save_screenshot_bundle,
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )
    registry = ToolRegistry()

    with (
        patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend),
        patch(
            "netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid",
            side_effect=AssertionError("bridge evidence must not rediscover a PID window"),
        ),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        content = await registry.tools["ui_take_screenshot"](SimpleNamespace(), evidence=True)

    assert isinstance(content, list), content
    metadata = json.loads(content[1].text)
    backend._client.call.assert_awaited_once_with("screenshot", {"evidence": True})
    assert metadata["capture_metadata"] == {
        "method": "PrintWindow",
        "hwnd": 777,
        "client_rect": {
            "left": 0,
            "top": 0,
            "right": 6,
            "bottom": 4,
            "unit": "physical_px",
            "coordinate_space": "client",
            "source_api": "GetClientRect",
        },
        "dpi": 144,
        "dpi_scale": 1.5,
        "physical_width": 8,
        "physical_height": 8,
        "logical_width": 8 / 1.5,
        "logical_height": 8 / 1.5,
    }
    assert Path(metadata["raw_path"]).read_bytes() == raw_png
    assert len(bundle_calls) == 1
    assert bundle_calls[0][0] == "stealth-evidence-session"
    assert bundle_calls[0][2] is None


@pytest.mark.parametrize(
    ("missing_key", "provenance"),
    [
        pytest.param("hwnd", {}, id="missing-hwnd"),
        pytest.param(None, {"hwnd": True}, id="non-integer-hwnd"),
        pytest.param(
            None,
            {
                "client_rect": {
                    "left": 0,
                    "top": 0,
                    "right": 0,
                    "bottom": 4,
                    "unit": "physical_px",
                    "coordinate_space": "client",
                    "source_api": "GetClientRect",
                }
            },
            id="empty-client-rect",
        ),
        pytest.param(
            None,
            {
                "client_rect": {
                    "left": 0,
                    "top": 0,
                    "right": 6,
                    "bottom": 4,
                    "unit": "dip",
                    "coordinate_space": "client",
                    "source_api": "GetClientRect",
                }
            },
            id="client-rect-wrong-unit",
        ),
        pytest.param(
            None,
            {
                "client_rect": {
                    "left": 0,
                    "top": 0,
                    "right": 6,
                    "bottom": 4,
                    "unit": "physical_px",
                    "coordinate_space": "screen",
                    "source_api": "GetClientRect",
                }
            },
            id="client-rect-wrong-space",
        ),
        pytest.param(
            None,
            {
                "client_rect": {
                    "left": 0,
                    "top": 0,
                    "right": 6,
                    "bottom": 4,
                    "unit": "physical_px",
                    "coordinate_space": "client",
                    "source_api": "UIA.BoundingRectangle",
                }
            },
            id="client-rect-wrong-source",
        ),
        pytest.param(None, {"dpi": 0}, id="zero-dpi"),
    ],
)
@pytest.mark.asyncio
async def test_ui_take_screenshot_stealth_evidence_rejects_missing_or_invalid_bridge_provenance(
    tmp_path,
    missing_key: str | None,
    provenance: dict[str, object],
) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buffer, format="PNG")
    bridge_result: dict[str, object] = {
        "base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "width": 8,
        "height": 8,
        "method": "PrintWindow",
        "hwnd": 777,
        "client_rect": {
            "left": 0,
            "top": 0,
            "right": 6,
            "bottom": 4,
            "unit": "physical_px",
            "coordinate_space": "client",
            "source_api": "GetClientRect",
        },
        "dpi": 144,
    }
    if missing_key is not None:
        del bridge_result[missing_key]
    bridge_result.update(provenance)
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(return_value=bridge_result)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id="stealth-evidence-session",
        temp_manager=SimpleNamespace(save_screenshot=lambda *_args: tmp_path / "unexpected.png"),
    )
    registry = ToolRegistry()

    with (
        patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend),
        patch(
            "netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid",
            side_effect=AssertionError("bridge evidence must not rediscover a PID window"),
        ),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        response = await registry.tools["ui_take_screenshot"](SimpleNamespace(), evidence=True)

    assert response["error"] == "Evidence capture requires valid bridge screenshot provenance"
    assert not (tmp_path / "unexpected.png").exists()


def _bridge_lossless_screenshot(
    png: bytes,
    *,
    width: int,
    height: int,
    hwnd: int = 777,
    window_bounds: bool = True,
) -> dict[str, object]:
    result: dict[str, object] = {
        "base64": base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "method": "PrintWindow",
        "hwnd": hwnd,
        "client_rect": {
            "left": 0,
            "top": 0,
            "right": width,
            "bottom": height,
            "unit": "physical_px",
            "coordinate_space": "client",
            "source_api": "GetClientRect",
        },
        "dpi": 144,
    }
    if window_bounds:
        result["window_bounds"] = {
            "left": 100,
            "top": 50,
            "right": 100 + width,
            "bottom": 50 + height,
            "unit": "physical_px",
            "coordinate_space": "screen",
            "source_api": "GetWindowRect",
        }
    return result


def _stealth_evidence_session(tmp_path, bundle_calls: list[tuple[str, str, str | None, int]]):
    from netcoredbg_mcp.session.manager import DebugState

    return SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id="strict-physical-target",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=_save_evidence_bundle(tmp_path, bundle_calls),
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )


@pytest.mark.asyncio
async def test_ui_take_screenshot_strict_physical_target_persists_exact_raw_bridge_raster(
    tmp_path,
) -> None:
    from PIL import Image

    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1080), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value=_bridge_lossless_screenshot(raw_png, width=1536, height=1080)
    )
    bundle_calls: list[tuple[str, str, str | None, int]] = []
    session = _stealth_evidence_session(tmp_path, bundle_calls)
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda _ctx: None)
        content = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            max_width=640,
            expected_hwnd=777,
            expected_physical_width=1536,
            expected_physical_height=1080,
        )

    metadata = json.loads(content[1].text)
    assert (
        metadata["evidence_grade"],
        metadata["raw_width"],
        metadata["raw_height"],
        metadata["width"],
        metadata["preview_width"],
        metadata["target_comparability"]["status"],
        metadata["physical_target"],
        metadata["capture_metadata"]["raw_raster"]["raster_source"],
        Path(metadata["raw_path"]).read_bytes(),
        len(bundle_calls),
    ) == (
        "lossless_raster",
        1536,
        1080,
        640,
        640,
        "MATCHED",
        {
            "status": "matched",
            "expected": {
                "hwnd": 777,
                "width": 1536,
                "height": 1080,
                "unit": "physical_px",
                "coordinate_space": "raw_raster",
            },
            "actual": {
                "hwnd": 777,
                "width": 1536,
                "height": 1080,
                "unit": "physical_px",
                "coordinate_space": "window",
                "bounds_source": "GetWindowRect",
            },
            "mismatch_fields": [],
        },
        "PrintWindow",
        raw_png,
        1,
    )


@pytest.mark.asyncio
async def test_ui_take_screenshot_strict_target_mismatch_returns_preview_without_evidence(
    tmp_path,
) -> None:
    from PIL import Image

    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (1535, 1079), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value=_bridge_lossless_screenshot(raw_png, width=1535, height=1079)
    )
    bundle_calls: list[tuple[str, str, str | None, int]] = []
    session = _stealth_evidence_session(tmp_path, bundle_calls)
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda _ctx: None)
        content = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            expected_hwnd=777,
            expected_physical_width=1536,
            expected_physical_height=1080,
        )

    metadata = json.loads(content[1].text)
    assert (
        metadata["evidence_grade"],
        metadata["target_comparability"]["status"],
        metadata["physical_target"],
        {key for key in ("retention", "raw_path", "raw_sha256", "crop_path") if key in metadata},
        bundle_calls,
    ) == (
        "preview_only",
        "MISMATCH",
        {
            "status": "mismatch",
            "code": "PHYSICAL_CAPTURE_MISMATCH",
            "expected": {
                "hwnd": 777,
                "width": 1536,
                "height": 1080,
                "unit": "physical_px",
                "coordinate_space": "raw_raster",
            },
            "actual": {
                "hwnd": 777,
                "width": 1535,
                "height": 1079,
                "unit": "physical_px",
                "coordinate_space": "window",
                "bounds_source": "GetWindowRect",
            },
            "mismatch_fields": ["width", "height"],
        },
        set(),
        [],
    )


@pytest.mark.asyncio
async def test_ui_take_screenshot_strict_physical_target_rejects_inconsistent_raw_bridge_provenance(
    tmp_path,
) -> None:
    from PIL import Image

    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    buffer = io.BytesIO()
    Image.new("RGB", (1535, 1079), (255, 255, 255)).save(buffer, format="PNG")
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value=_bridge_lossless_screenshot(buffer.getvalue(), width=1536, height=1080)
    )
    bundle_calls: list[tuple[str, str, str | None, int]] = []
    session = _stealth_evidence_session(tmp_path, bundle_calls)
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda _ctx: None)
        response = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            expected_hwnd=777,
            expected_physical_width=1536,
            expected_physical_height=1080,
        )

    assert (
        response["error"],
        response["code"],
        "data" in response,
        isinstance(response, list),
        bundle_calls,
    ) == (
        "Bridge screenshot dimensions do not match decoded PNG dimensions",
        "PHYSICAL_CAPTURE_PROVENANCE_UNAVAILABLE",
        False,
        False,
        [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict_arguments", "expected_error"),
    [
        pytest.param(
            {"expected_hwnd": 777},
            (
                "expected_hwnd, expected_physical_width, and "
                "expected_physical_height must be supplied together"
            ),
            id="partial",
        ),
        pytest.param(
            {
                "expected_hwnd": 0,
                "expected_physical_width": 1536,
                "expected_physical_height": 1080,
            },
            "expected_hwnd must be non-zero",
            id="zero-hwnd",
        ),
        pytest.param(
            {
                "expected_hwnd": 777,
                "expected_physical_width": 0,
                "expected_physical_height": 1080,
            },
            "expected_physical_width must be positive, got 0",
            id="zero-width",
        ),
        pytest.param(
            {
                "expected_hwnd": 777,
                "expected_physical_width": 1536,
                "expected_physical_height": 0,
            },
            "expected_physical_height must be positive, got 0",
            id="zero-height",
        ),
    ],
)
async def test_ui_take_screenshot_strict_physical_target_rejects_malformed_assertions(
    tmp_path,
    strict_arguments: dict[str, int],
    expected_error: str,
) -> None:
    from netcoredbg_mcp.tools.ui import register_ui_tools

    bundle_calls: list[tuple[str, str, str | None, int]] = []
    session = _stealth_evidence_session(tmp_path, bundle_calls)
    registry = ToolRegistry()
    register_ui_tools(registry, session, check_session_access=lambda _ctx: None)

    response = await registry.tools["ui_take_screenshot"](
        SimpleNamespace(), evidence=True, **strict_arguments
    )

    assert (
        response["error"],
        "code" in response,
        "data" in response,
        bundle_calls,
    ) == (expected_error, False, False, [])


@pytest.mark.asyncio
async def test_ui_take_screenshot_evidence_uses_unique_filenames_and_executor(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    saved: list[tuple[str, str, str | None, int]] = []
    main_thread = threading.get_ident()
    save_screenshot_bundle = _save_evidence_bundle(tmp_path, saved)

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="collision-session",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=save_screenshot_bundle,
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )
    registry = ToolRegistry()
    with (
        patch("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", return_value=123),
        patch(
            "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
            return_value=(raw_png, 8, 8, _capture_metadata("PrintWindow", 8, 8)),
        ),
        patch("time.time", return_value=1_000.0),
    ):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        first = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            crop_x=0,
            crop_y=0,
            crop_width=2,
            crop_height=2,
        )
        second = await registry.tools["ui_take_screenshot"](
            SimpleNamespace(),
            evidence=True,
            crop_x=0,
            crop_y=0,
            crop_width=2,
            crop_height=2,
        )

    first_metadata = json.loads(first[1].text)
    second_metadata = json.loads(second[1].text)
    assert first_metadata["raw_path"] != second_metadata["raw_path"]
    assert first_metadata["crop_path"] != second_metadata["crop_path"]
    assert len(saved) == 2
    assert len({raw_name for _sid, raw_name, _crop_name, _thread_id in saved}) == 2
    assert len({crop_name for _sid, _raw_name, crop_name, _thread_id in saved}) == 2
    assert all(crop_name is not None for _sid, _raw_name, crop_name, _thread_id in saved)
    assert all(thread_id != main_thread for _sid, _raw_name, _crop_name, thread_id in saved)


@pytest.mark.asyncio
async def test_wpf_fixture_stealth_foundation_read_only_ui_path() -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    fixture_root = PROJECT_ROOT / "tests" / "fixtures" / "WpfSmokeApp"
    xaml = (fixture_root / "MainWindow.xaml").read_text(encoding="utf-8")

    assert (fixture_root / "WpfSmokeApp.csproj").exists()
    assert 'AutomationProperties.AutomationId="mainWindow"' in xaml
    assert 'AutomationProperties.AutomationId="btnInvoke"' in xaml
    assert 'AutomationProperties.AutomationId="txtOutput"' in xaml

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = None
    backend.connect = AsyncMock()
    backend.get_window_tree = AsyncMock(
        return_value={
            "windows": [
                {
                    "automationId": "mainWindow",
                    "name": "WPF Smoke Test",
                    "children": [
                        {"automationId": "btnInvoke"},
                        {"automationId": "txtOutput"},
                    ],
                }
            ],
            "count": 1,
        }
    )

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=4242),
        stealth_mode=True,
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(
            registry,
            session,
            check_session_access=lambda ctx: None,
        )

        response = await registry.tools["ui_get_window_tree"]()

    backend.connect.assert_awaited_once_with(4242, stealth=True)
    backend.get_window_tree.assert_awaited_once_with(3, 50)
    assert response["data"]["windows"][0]["automationId"] == "mainWindow"


@pytest.mark.asyncio
async def test_ui_take_screenshot_evidence_requests_strict_bridge_provenance(tmp_path) -> None:
    from PIL import Image

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    png = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(png, format="PNG")
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = MagicMock()
    backend._client.call = AsyncMock(
        return_value={
            "base64": base64.b64encode(png.getvalue()).decode("ascii"),
            "width": 8,
            "height": 8,
            "method": "PrintWindow",
            "hwnd": 123,
            "client_rect": {"left": 0, "top": 0, "right": 8, "bottom": 8},
        }
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id="evidence-session",
        temp_manager=SimpleNamespace(
            save_screenshot=lambda _sid, _data, name: tmp_path / name,
        ),
    )
    registry = ToolRegistry()

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(registry, session, check_session_access=lambda ctx: None)
        response = await registry.tools["ui_take_screenshot"](SimpleNamespace(), evidence=True)

    backend._client.call.assert_awaited_once_with("screenshot", {"evidence": True})
    assert response["error"] == "Evidence capture requires valid bridge screenshot provenance"

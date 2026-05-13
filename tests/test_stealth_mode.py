"""Tests for stealth-mode bridge contracts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    batch_body = command[batch_start: command.index("public static JsonNode SetValue(")]

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
    assert "ensureForegroundBeforeEach: true" in batch_body
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
    assert "if (JsonRpcHandler.Stealth)" in automation_body
    assert (
        "return FlashFocusClick(center.X, center.Y, mainWindow, automationId);"
        in automation_body
    )
    assert "private static JsonObject FlashFocusClick" in command
    assert "SetForegroundWindow(savedForeground)" in command


def test_bridge_screenshot_uses_printwindow_in_stealth_mode() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "private const uint PW_RENDERFULLCONTENT = 0x00000002;" in command
    assert "if (JsonRpcHandler.Stealth)" in command
    assert "PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)" in command
    assert '["base64"] = base64' in command
    assert "Capture.Rectangle(rect)" in command
    assert command.index("if (JsonRpcHandler.Stealth)") < command.index(
        "Capture.Rectangle(rect)"
    )


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
    assert 'JsonRpcHandler.Stealth = @params?["stealth"]?.GetValue<bool>() ?? false;' in elements


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

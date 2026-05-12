"""Tests for stealth-mode bridge contracts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bridge_registers_save_restore_foreground_commands() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")

    assert '["save_foreground"] = StealthCommands.SaveForeground' in router
    assert '["restore_foreground"] = StealthCommands.RestoreForeground' in router


def test_bridge_stealth_foreground_round_trip_contract() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "StealthCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "GetForegroundWindow()" in command
    assert '["hwnd"] = hwnd.ToInt64()' in command
    assert '@params?["hwnd"]?.GetValue<long>()' in command
    assert "SetForegroundWindow(hwnd)" in command
    assert '["restored"] = restored' in command


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

    class ToolRegistry:
        def __init__(self) -> None:
            self.tools = {}

        def tool(self, annotations=None):
            def decorator(func):
                self.tools[func.__name__] = func
                return func

            return decorator

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

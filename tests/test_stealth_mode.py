"""Tests for stealth-mode bridge contracts."""

from pathlib import Path


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

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

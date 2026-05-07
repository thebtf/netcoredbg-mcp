"""Scoped key sequence evidence tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
from netcoredbg_mcp.ui.key_sequence import run_scoped_key_sequence
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

TEST_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_ROOT.parent


class FakeKeySequenceBackend:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def scoped_key_sequence(
        self,
        selector: dict[str, Any],
        modifiers: list[str],
        keys: list[str],
    ) -> dict[str, Any]:
        self.calls.append({
            "selector": dict(selector),
            "modifiers": list(modifiers),
            "keys": list(keys),
        })
        return dict(self.response)


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


@pytest.mark.asyncio
async def test_scoped_key_sequence_reports_shift_held_for_two_down_keys() -> None:
    backend = FakeKeySequenceBackend({
        "status": "PASS",
        "focused": {"focused": True, "automationId": "CueGrid"},
        "sent_count": 2,
        "held_modifiers_during_sequence": ["shift"],
        "release_result": {"released": True, "modifiers": []},
        "final_held_modifiers": [],
    })

    result = await run_scoped_key_sequence(
        backend,
        {"automation_id": "CueGrid"},
        modifiers=["shift"],
        keys=["Down", "Down"],
    )

    assert result["status"] == "PASS"
    assert result["sent_count"] == 2
    assert result["held_modifiers_during_sequence"] == ["shift"]
    assert result["final_held_modifiers"] == []
    assert backend.calls == [{
        "selector": {"automation_id": "CueGrid"},
        "modifiers": ["shift"],
        "keys": ["DOWN", "DOWN"],
    }]


@pytest.mark.asyncio
async def test_scoped_key_sequence_fails_when_cleanup_leaves_shift_held() -> None:
    backend = FakeKeySequenceBackend({
        "status": "PASS",
        "focused": {"focused": True},
        "sent_count": 1,
        "held_modifiers_during_sequence": ["shift"],
        "release_result": {"released": False, "modifiers": ["shift"]},
        "final_held_modifiers": ["shift"],
    })

    result = await run_scoped_key_sequence(
        backend,
        {},
        modifiers=["shift"],
        keys=["Down"],
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "modifier cleanup left held modifiers"
    assert result["final_held_modifiers"] == ["shift"]


@pytest.mark.asyncio
async def test_scoped_key_sequence_blocks_unsupported_backend_without_fake_success() -> None:
    backend = FakeKeySequenceBackend({
        "status": "UNSUPPORTED",
        "unsupported": True,
        "reason": "FlaUI bridge required for scoped key sequence",
    })

    result = await run_scoped_key_sequence(
        backend,
        {},
        modifiers=["shift"],
        keys=["Down"],
    )

    assert result["status"] == "BLOCKED"
    assert "FlaUI bridge" in result["reason"]


@pytest.mark.asyncio
async def test_unknown_key_fails_before_backend_call() -> None:
    backend = FakeKeySequenceBackend({"status": "PASS"})

    result = await run_scoped_key_sequence(
        backend,
        {},
        modifiers=["shift"],
        keys=["NotAKey"],
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "unknown key"
    assert result["invalid_key"] == "NotAKey"
    assert backend.calls == []


@pytest.mark.asyncio
async def test_unknown_modifier_fails_before_backend_call() -> None:
    backend = FakeKeySequenceBackend({"status": "PASS"})

    result = await run_scoped_key_sequence(
        backend,
        {},
        modifiers=["meta"],
        keys=["Down"],
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "unknown modifier"
    assert result["invalid_modifier"] == "meta"
    assert backend.calls == []


@pytest.mark.asyncio
async def test_flaui_backend_forwards_scoped_key_sequence_to_bridge() -> None:
    backend = _make_flaui()
    backend._client.call.return_value = {
        "status": "PASS",
        "sent_count": 2,
        "final_held_modifiers": [],
    }

    result = await backend.scoped_key_sequence(
        {"automation_id": "CueGrid"},
        ["shift"],
        ["Down", "Down"],
    )

    assert result["status"] == "PASS"
    backend._client.call.assert_awaited_once_with(
        "scoped_key_sequence",
        {
            "selector": {"automationId": "CueGrid"},
            "modifiers": ["shift"],
            "keys": ["Down", "Down"],
        },
    )


@pytest.mark.asyncio
async def test_pywinauto_backend_returns_unsupported_for_scoped_key_sequence() -> None:
    backend = PywinautoBackend()

    result = await backend.scoped_key_sequence({}, ["shift"], ["Down"])

    assert result["status"] == "UNSUPPORTED"
    assert result["unsupported"] is True
    assert "FlaUI bridge" in result["reason"]


@pytest.mark.asyncio
async def test_ui_key_sequence_tool_is_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tool_names = {tool.name for tool in await server.list_tools()}

    assert "ui_key_sequence" in tool_names


def test_bridge_router_registers_scoped_key_sequence_and_dispose_cleanup() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")
    command = (
        PROJECT_ROOT / "bridge" / "Commands" / "KeySequenceCommands.cs"
    ).read_text(encoding="utf-8")

    assert '["scoped_key_sequence"]' in router
    assert "KeySequenceCommands.ScopedKeySequence" in router
    assert "ModifierCommands.ReleaseAllHeldModifiers();" in router
    assert "SendInput(1, [input], Marshal.SizeOf<INPUT>())" in command
    assert "TryAcquireScopedModifier" in command
    assert "ReleaseScopedModifier" in command
    assert "releaseFailureNames" in command


def test_wpf_and_avalonia_fixtures_expose_shift_datagrid_routes() -> None:
    fixture_root = TEST_ROOT / "fixtures"
    wpf_xaml = (fixture_root / "WpfSmokeApp" / "MainWindow.xaml").read_text(
        encoding="utf-8",
    )
    wpf_code = (fixture_root / "WpfSmokeApp" / "MainWindow.xaml.cs").read_text(
        encoding="utf-8",
    )
    avalonia_xaml = (fixture_root / "AvaloniaSmokeApp" / "MainWindow.axaml").read_text(
        encoding="utf-8",
    )
    avalonia_code = (
        fixture_root / "AvaloniaSmokeApp" / "MainWindow.axaml.cs"
    ).read_text(
        encoding="utf-8",
    )

    assert 'AutomationProperties.AutomationId="dataGrid"' in wpf_xaml
    assert 'PreviewKeyDown="DataGrid_PreviewKeyDown"' in wpf_xaml
    assert 'SelectionChanged="DataGrid_SelectionChanged"' in wpf_xaml
    assert "DataGridArrow key=" in wpf_code
    assert "_suppressSelectionSync" in wpf_code
    assert 'AutomationProperties.AutomationId="dataGrid"' in avalonia_xaml
    assert 'KeyDown="DataGrid_KeyDown"' in avalonia_xaml
    assert "AvaloniaDataGridArrow key=" in avalonia_code

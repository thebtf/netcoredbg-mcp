"""Focus assertion helper tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.ui.focus import assert_focus

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeFocusBackend:
    def __init__(self, focused: bool) -> None:
        self.focused = focused
        self.calls: list[dict[str, Any]] = []

    async def assert_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(selector))
        return {
            "status": "PASS" if self.focused else "FAIL",
            "focused": self.focused,
            "selector": dict(selector),
            "reason": "focus matched" if self.focused else "focus outside selector",
        }


@pytest.mark.asyncio
async def test_focus_assertion_passes_when_backend_reports_matching_focus() -> None:
    backend = FakeFocusBackend(focused=True)

    result = await assert_focus(backend, {"automation_id": "CueDataGrid"})

    assert result["status"] == "PASS"
    assert result["focused"] is True
    assert backend.calls == [{"automation_id": "CueDataGrid"}]


@pytest.mark.asyncio
async def test_focus_assertion_fails_when_focus_is_outside_selector() -> None:
    result = await assert_focus(
        FakeFocusBackend(focused=False),
        {"automation_id": "CueDataGrid"},
    )

    assert result["status"] == "FAIL"
    assert result["focused"] is False
    assert result["reason"] == "focus outside selector"


def test_bridge_focus_assertion_accepts_descendant_focus() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "FocusCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "IsSameOrDescendant(expected, focused)" in command
    assert "SameRuntimeId(expected, current)" in command
    assert "current = current.Parent" in command


def test_bridge_registers_focused_element_query() -> None:
    router = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(encoding="utf-8")

    assert '["get_focused_element"] = FocusCommands.GetFocusedElement' in router


def test_bridge_focused_element_query_returns_bounded_element_info() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "FocusCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "public static JsonNode GetFocusedElement" in command
    assert "automation.FocusedElement()" in command
    assert "ElementCommands.BuildElementInfo(focused, includePatterns: false)" in command
    assert 'result["focused"] = true' in command
    assert '["focused"] = false' in command
    assert 'result["value"] = FocusedValue(focused)' in command


def test_bridge_focused_element_query_handles_transient_focused_element_errors() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "FocusCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "AutomationElement? focused = null;" in command
    assert "focused = automation.FocusedElement();" in command
    assert "catch (COMException)" in command
    assert "catch (InvalidOperationException)" in command
    assert "if (focused is null)" in command
    assert "return EmptyFocusedElementInfo();" in command


def test_bridge_focused_element_query_rejects_focus_outside_connected_process() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "FocusCommands.cs").read_text(
        encoding="utf-8"
    )

    assert "FocusedElementBelongsToConnectedProcess(focused, mainWindow)" in command
    assert "JsonRpcHandler.ProcessId" in command
    assert "mainWindow.Properties.ProcessId.ValueOrDefault" in command
    assert "focused.Properties.ProcessId.ValueOrDefault" in command
    assert "return EmptyFocusedElementInfo();" in command

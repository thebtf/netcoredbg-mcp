"""Focused behavior tests for the installed WPF submenu consumer helper."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.types import TextContent

from tests.wpf_submenu_consumer import (
    _poll_discovery,
    _server_environment,
    _tree_contains_automation_id,
)


class _ToolResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.isError = False
        self.content = [TextContent(type="text", text=json.dumps(payload))]


class _ConsumerSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _ToolResult:
        self.calls.append((name, arguments))
        return _ToolResult(self._payload)


def test_tree_contains_automation_id_distinguishes_closed_and_open_submenus() -> None:
    closed_tree = {
        "windows": [
            {
                "automationId": "mainWindow",
                "children": [{"automationId": "submenuParent"}],
            }
        ]
    }
    open_tree = {
        "windows": [
            {
                "automationId": "mainWindow",
                "children": [
                    {"automationId": "submenuParent"},
                    {"automationId": "submenuChild"},
                ],
            }
        ]
    }

    assert not _tree_contains_automation_id(closed_tree, "submenuChild")
    assert _tree_contains_automation_id(open_tree, "submenuChild")


def test_server_environment_preserves_required_tool_paths(monkeypatch) -> None:
    monkeypatch.setenv("FLAUI_BRIDGE_PATH", "C:/bridge/FlaUIBridge.exe")
    monkeypatch.setenv("NETCOREDBG_PATH", "C:/debug/netcoredbg.exe")

    assert _server_environment() == {
        "FLAUI_BRIDGE_PATH": "C:/bridge/FlaUIBridge.exe",
        "NETCOREDBG_PATH": "C:/debug/netcoredbg.exe",
    }


@pytest.mark.asyncio
async def test_poll_discovery_returns_matching_public_response_with_attempt_evidence() -> None:
    arguments = {"max_depth": 6, "max_children": 100}
    session = _ConsumerSession({"data": {"automationId": "submenuParent"}})

    data, evidence = await _poll_discovery(
        session,  # type: ignore[arg-type]
        name="ui_find_element",
        arguments=arguments,
        matches=lambda candidate: candidate.get("automationId") == "submenuParent",
    )

    assert data == {"automationId": "submenuParent"}
    assert evidence == {
        "operation": "ui_find_element",
        "attempts": 1,
        "deadline_seconds": 10.0,
    }
    assert session.calls == [("ui_find_element", arguments)]

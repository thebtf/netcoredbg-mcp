"""Focused behavior tests for the installed WPF submenu consumer helper."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import TextContent

from tests import wpf_submenu_consumer
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
        "deadline_seconds": wpf_submenu_consumer.POLL_DEADLINE_SECONDS,
    }
    assert session.calls == [("ui_find_element", arguments)]


@pytest.mark.asyncio
async def test_poll_discovery_preserves_timeout_event_when_sleep_reaches_deadline(
    monkeypatch,
) -> None:
    class _Clock:
        now = 0.0

        def time(self) -> float:
            return self.now

    clock = _Clock()

    async def timeout_after_partial_attempt(
        awaitable: Any,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        assert timeout == 1.0
        awaitable.close()
        clock.now += 0.75
        raise asyncio.TimeoutError

    async def sleep_to_deadline(delay: float) -> None:
        clock.now += delay

    monkeypatch.setattr(wpf_submenu_consumer, "POLL_DEADLINE_SECONDS", 1.0)
    monkeypatch.setattr(wpf_submenu_consumer, "POLL_INTERVAL_SECONDS", 0.25)
    monkeypatch.setattr(
        wpf_submenu_consumer,
        "asyncio",
        SimpleNamespace(
            TimeoutError=asyncio.TimeoutError,
            get_running_loop=lambda: clock,
            sleep=sleep_to_deadline,
            wait_for=timeout_after_partial_attempt,
        ),
    )

    session = _ConsumerSession({})  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="discovery deadline") as raised:
        await _poll_discovery(
            session,
            name="ui_find_element",
            arguments={},
            matches=lambda _: False,
        )

    evidence = json.loads(str(raised.value).split(": ", maxsplit=1)[1])
    assert evidence["attempts"] == 1
    assert evidence["last_received_response"] is None
    assert evidence["terminal_event"] == "attempt_timeout_no_response"

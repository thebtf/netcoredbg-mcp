"""Tests for DAP progress event tracking and get_progress."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent, Events
from netcoredbg_mcp.session import SessionManager


def make_manager() -> SessionManager:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        return SessionManager()


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


def test_progress_lifecycle_start_update_end():
    manager = make_manager()

    manager._on_progress_start(DAPEvent(
        seq=1,
        event=Events.PROGRESS_START,
        body={
            "progressId": "p1",
            "title": "Loading symbols",
            "message": "starting",
            "percentage": 10,
            "cancellable": True,
        },
    ))
    assert manager.state.active_progress["p1"].title == "Loading symbols"
    assert manager.state.active_progress["p1"].percentage == 10.0

    manager._on_progress_update(DAPEvent(
        seq=2,
        event=Events.PROGRESS_UPDATE,
        body={"progressId": "p1", "message": "halfway", "percentage": 50},
    ))
    assert manager.state.active_progress["p1"].message == "halfway"
    assert manager.state.active_progress["p1"].percentage == 50.0

    manager._on_progress_end(DAPEvent(
        seq=3,
        event=Events.PROGRESS_END,
        body={"progressId": "p1"},
    ))
    assert "p1" not in manager.state.active_progress


def test_progress_start_replaces_existing_entry():
    manager = make_manager()

    manager._on_progress_start(DAPEvent(
        seq=1,
        event=Events.PROGRESS_START,
        body={"progressId": "p1", "title": "First", "percentage": 10},
    ))
    first_started_at = manager.state.active_progress["p1"].started_at

    manager._on_progress_start(DAPEvent(
        seq=2,
        event=Events.PROGRESS_START,
        body={"progressId": "p1", "title": "Second", "percentage": 20},
    ))

    entry = manager.state.active_progress["p1"]
    assert entry.title == "Second"
    assert entry.percentage == 20.0
    assert entry.started_at >= first_started_at


def test_progress_update_unknown_id_warns(caplog):
    manager = make_manager()

    caplog.set_level("WARNING", logger="netcoredbg_mcp.session.manager")
    manager._on_progress_update(DAPEvent(
        seq=1,
        event=Events.PROGRESS_UPDATE,
        body={"progressId": "missing", "percentage": 5},
    ))

    assert "unknown progressId" in caplog.text


def test_progress_end_unknown_id_warns(caplog):
    manager = make_manager()

    caplog.set_level("WARNING", logger="netcoredbg_mcp.session.manager")
    manager._on_progress_end(DAPEvent(
        seq=1,
        event=Events.PROGRESS_END,
        body={"progressId": "missing"},
    ))

    assert "unknown progressId" in caplog.text


def test_get_progress_returns_response_shape():
    manager = make_manager()
    manager._on_progress_start(DAPEvent(
        seq=1,
        event=Events.PROGRESS_START,
        body={"progressId": "p1", "title": "Loading", "percentage": 25},
    ))

    progress = manager.get_progress()

    assert progress == [{
        "progressId": "p1",
        "progress_id": "p1",
        "title": "Loading",
        "message": None,
        "percentage": 25.0,
        "cancellable": False,
        "startedAt": progress[0]["startedAt"],
        "started_at": progress[0]["started_at"],
    }]


@pytest.mark.asyncio
async def test_get_progress_tool_response():
    from netcoredbg_mcp.tools.inspection import register_inspection_tools

    manager = make_manager()
    manager._on_progress_start(DAPEvent(
        seq=1,
        event=Events.PROGRESS_START,
        body={"progressId": "p1", "title": "Loading", "percentage": 25},
    ))
    registry = ToolRegistry()
    register_inspection_tools(registry, manager, lambda ctx: None)

    response = await registry.tools["get_progress"]()

    assert response["data"]["count"] == 1
    assert response["data"]["progress"][0]["progressId"] == "p1"
    assert response["data"]["progress"][0]["progress_id"] == "p1"
    assert "started_at" in response["data"]["progress"][0]

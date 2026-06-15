"""Registration and compatibility baseline for runtime smoke work."""

from __future__ import annotations

import os

import pytest

from netcoredbg_mcp.response import build_error_response, build_response
from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.session.state import DebugState


@pytest.mark.asyncio
async def test_existing_primitive_tool_names_remain_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert {
        "start_debug",
        "stop_debug",
        "add_breakpoint",
        "clear_breakpoints",
        "list_breakpoints",
        "get_output",
        "get_output_tail",
        "search_output",
        "ui_send_keys",
        "ui_set_focus",
    }.issubset(tool_names)


@pytest.mark.asyncio
async def test_runtime_smoke_agent_lifecycle_tools_are_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert {
        "runtime_smoke_start",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
        "runtime_smoke_stop",
        "run_runtime_smoke",
    }.issubset(tool_names)


@pytest.mark.asyncio
async def test_ui_monitor_tools_are_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert {
        "ui_monitor_start",
        "ui_monitor_poll",
        "ui_monitor_wait",
        "ui_monitor_events",
    }.issubset(tool_names)


def test_success_response_keeps_existing_envelope_meaning() -> None:
    response = build_response(
        data={"value": 42},
        state=DebugState.STOPPED,
        message="custom message",
    )

    assert response["state"] == "stopped"
    assert response["message"] == "custom message"
    assert response["data"] == {"value": 42}
    assert "get_call_stack" in response["next_actions"]
    assert "error" not in response


def test_error_response_keeps_existing_envelope_meaning() -> None:
    response = build_error_response("boom", state=DebugState.IDLE)

    assert response["error"] == "boom"
    assert response["state"] == "idle"
    assert "start_debug" in response["next_actions"]
    assert response["message"].startswith("Error: boom.")


def test_fake_smoke_session_fixture_exposes_required_state(fake_smoke_session) -> None:
    assert fake_smoke_session.breakpoints == {}
    assert fake_smoke_session.tracepoints == {}
    assert list(fake_smoke_session.output_buffer) == []
    assert fake_smoke_session.modules == []
    assert fake_smoke_session.loaded_sources == {}
    assert fake_smoke_session.process_id is None

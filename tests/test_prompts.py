"""Tests for MCP prompt registration and DAP escape hatch discoverability."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

UNWRAPPED_DAP_COMMANDS = [
    "cancel",
    "restart",
    "restartFrame",
    "goto",
    "gotoTargets",
    "stepBack",
    "reverseContinue",
    "terminateThreads",
    "setInstructionBreakpoints",
    "source",
    "completions",
    "setExpression",
]


DAP_WRAPPING_TOOLS = [
    "start_debug",
    "attach_debug",
    "stop_debug",
    "restart_debug",
    "continue_execution",
    "pause_execution",
    "step_over",
    "get_step_in_targets",
    "step_into",
    "step_out",
    "get_debug_state",
    "terminate_debug",
    "add_breakpoint",
    "remove_breakpoint",
    "list_breakpoints",
    "clear_breakpoints",
    "add_function_breakpoint",
    "configure_exceptions",
    "get_threads",
    "get_call_stack",
    "get_scopes",
    "get_variables",
    "evaluate_expression",
    "set_variable",
    "get_exception_info",
    "get_modules",
    "get_progress",
    "get_loaded_sources",
    "disassemble",
    "get_locations",
    "quick_evaluate",
    "get_exception_context",
    "get_stop_context",
    "read_memory",
    "write_memory",
]


@pytest.fixture
def mcp_server():
    with patch.dict(os.environ, {"NETCOREDBG_PATH": "/fake/netcoredbg"}):
        with patch(
            "netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg",
            return_value="/fake/netcoredbg",
        ):
            from netcoredbg_mcp.server import create_server

            yield create_server()


@pytest.mark.asyncio
async def test_escape_hatch_lists_all_unwrapped(mcp_server):
    prompts = await mcp_server.list_prompts()
    prompt_names = {prompt.name for prompt in prompts}
    assert "dap-escape-hatch" in prompt_names

    result = await mcp_server.get_prompt("dap-escape-hatch")
    content = "\n".join(message.content.text for message in result.messages)

    for command in UNWRAPPED_DAP_COMMANDS:
        assert f"`{command}`" in content
        assert f'send_request("{command}"' in content


@pytest.mark.asyncio
async def test_every_wrapping_tool_mentions_escape_hatch(mcp_server):
    tools = await mcp_server.list_tools()
    descriptions = {tool.name: tool.description or "" for tool in tools}

    missing_tools = [name for name in DAP_WRAPPING_TOOLS if name not in descriptions]
    assert missing_tools == []

    missing_pointer = [
        name for name in DAP_WRAPPING_TOOLS
        if "dap-escape-hatch" not in descriptions[name]
    ]
    assert missing_pointer == []

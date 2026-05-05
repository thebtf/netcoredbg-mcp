"""Smoke tests — verify the MCP server actually starts and registers tools.

These tests catch import errors, annotation resolution failures, and
registration crashes that unit tests miss because they test components
in isolation.

If this file fails, the server CANNOT start. Fix before merging.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_netcoredbg():
    """Mock netcoredbg path so server can initialize without the binary."""
    with patch.dict(os.environ, {"NETCOREDBG_PATH": "/fake/netcoredbg"}):
        with patch(
            "netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg",
            return_value="/fake/netcoredbg",
        ):
            yield


class TestServerSmoke:
    """Verify the server starts and all tools/prompts/resources register."""

    def test_server_creates_without_crash(self):
        """The most basic test: can we create the server at all?"""
        from netcoredbg_mcp.server import create_server

        mcp = create_server()
        assert mcp is not None

    @pytest.mark.asyncio
    async def test_tools_register(self):
        """All tools must register without annotation resolution errors."""
        from netcoredbg_mcp.server import create_server

        mcp = create_server()
        tools = await mcp.list_tools()

        # We expect 40+ tools — fail loudly if tools are missing
        tool_names = [t.name for t in tools]
        assert len(tool_names) >= 40, f"Expected 40+ tools, got {len(tool_names)}: {tool_names}"

        # Critical tools must be present
        critical = [
            "start_debug", "stop_debug", "continue_execution",
            "step_over", "step_into", "step_out",
            "add_breakpoint", "get_call_stack", "get_variables",
            "ui_take_screenshot", "ui_take_annotated_screenshot",
            "cleanup_processes", "restart_debug",
            "get_progress", "read_memory", "write_memory",
        ]
        for name in critical:
            assert name in tool_names, f"Critical tool '{name}' missing from server"

    @pytest.mark.asyncio
    async def test_prompts_register(self):
        """All prompts must register."""
        from netcoredbg_mcp.server import create_server

        mcp = create_server()
        prompts = await mcp.list_prompts()

        prompt_names = [p.name for p in prompts]
        assert len(prompt_names) >= 6, f"Expected 6+ prompts, got {len(prompt_names)}"

        expected = [
            "debug", "debug-gui", "debug-exception", "investigate",
            "debug-scenario", "dap-escape-hatch",
        ]
        for name in expected:
            assert name in prompt_names, f"Prompt '{name}' missing from server"

    @pytest.mark.asyncio
    async def test_parameterized_prompt_renders(self):
        """Parameterized prompts must render without errors."""
        from netcoredbg_mcp.server import create_server

        mcp = create_server()

        result = await mcp.get_prompt(
            "investigate", arguments={"symptom": "NullReferenceException"}
        )
        assert result.messages
        content = result.messages[0].content
        assert "NullReferenceException" in content.text

    @pytest.mark.asyncio
    async def test_resources_register(self):
        """Resources must be listed."""
        from netcoredbg_mcp.server import create_server

        mcp = create_server()
        resources = await mcp.list_resources()

        resource_uris = [str(r.uri) for r in resources]
        assert len(resource_uris) >= 3, f"Expected 3+ resources, got {len(resource_uris)}"

    def test_tool_annotations_present(self):
        """Tools must have ToolAnnotations set."""
        import asyncio

        from netcoredbg_mcp.server import create_server

        mcp = create_server()
        tools = asyncio.run(mcp.list_tools())

        # At least some tools should have annotations
        annotated = [t for t in tools if t.annotations is not None]
        assert len(annotated) > 0, "No tools have annotations"

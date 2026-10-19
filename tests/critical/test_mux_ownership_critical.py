"""Critical release-gate test for FD-007 mux session ownership parity.

@critical
category: behavioral
features: x-mux-capability-projection, session-ownership-parity
dev_stand: optional
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent

from tests.test_host_proxy import (
    HOST_DLL,
    HOST_PROJECT,
    _build_dotnet_test_project,
    _test_child_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)


def _payload(result: CallToolResult) -> dict:
    assert not result.isError, f"tool call reported isError: {result}"
    first = result.content[0]
    assert isinstance(first, TextContent)
    return json.loads(first.text)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_mux_session_ownership_survives_the_installed_host_process(tmp_path: Path) -> None:
    """@critical category: behavioral - through the real installed .NET host process
    (not direct Python registration), Python's SessionOwnership still arbitrates:
    the same agent may mutate repeatedly, a competing agent is denied by name,
    and a read-only observation is always permitted regardless of ownership."""
    host_dll = _build_dotnet_test_project(HOST_PROJECT, "Release", HOST_DLL.name)

    env = _test_child_env(get_default_environment())
    env["PYTHONPATH"] = str(SRC_DIR)
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable

    params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project-from-cwd"],
        env=env,
        cwd=str(tmp_path),
    )

    errlog_path = tmp_path / "host-stderr.log"
    with open(errlog_path, "w+", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                claimed = _payload(
                    await session.call_tool(
                        "cleanup_processes",
                        {"force": True},
                        meta={"muxSessionId": "agent-A"},
                    )
                )
                assert "error" not in claimed, claimed

                repeated = _payload(
                    await session.call_tool(
                        "cleanup_processes",
                        {"force": True},
                        meta={"muxSessionId": "agent-A"},
                    )
                )
                assert "error" not in repeated, repeated

                denied = _payload(
                    await session.call_tool(
                        "cleanup_processes",
                        {"force": True},
                        meta={"muxSessionId": "agent-B"},
                    )
                )
                assert "error" in denied, denied
                assert "agent-A" in denied["error"]
                assert "owned by another agent" in denied["error"]

                observed = _payload(
                    await session.call_tool(
                        "cleanup_processes",
                        {"force": False},
                        meta={"muxSessionId": "agent-B"},
                    )
                )
                assert "error" not in observed, observed
                assert observed["data"]["action"] == "status"

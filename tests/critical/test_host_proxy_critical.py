"""Critical release-gate coverage for the real .NET MCP compatibility host (Q0).

Promotes the smallest real Release-host ``initialize`` -> ``tools/list`` ->
``tools/call`` exchange into the critical suite. This drives the actual
``dotnet``-hosted compatibility proxy and a real Python child over stdio
through the official ``mcp`` client SDK -- the same technique already proven
by ``tests/test_host_proxy.py`` -- not direct Python tool registration or
source-text assertions. Later front-door proxied surfaces (roots,
progress/logging, and further tool families) extend this same file rather
than inventing a second release-only smoke path. The ``host_dll`` fixture is
reused unchanged from ``tests/test_host_proxy.py`` via
``tests/critical/conftest.py``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.test_host_proxy import MINIMAL_PLAN, PARITY_TOOL_NAME, _backend_env, _tool_payload

pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_host_proxy_critical_initialize_list_call(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """@critical category: behavioral - real Release host completes a live MCP round trip."""
    env = _backend_env()
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable

    params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project-from-cwd"],
        env=env,
        cwd=str(tmp_path),
    )

    async def _run_exchange(errlog) -> None:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # Startup + real MCP capability negotiation against the actual child.
                init_result = await session.initialize()
                assert init_result.protocolVersion, "host must negotiate a protocol version"
                assert init_result.capabilities.tools is not None, (
                    "host must advertise a tools capability"
                )

                # Real server identity from the actual .NET host process,
                # not a stub -- and the mcp-mux session-isolation capability
                # it mirrors from the Python server (see Program.cs).
                assert init_result.serverInfo.name == "netcoredbg-mcp-host", (
                    f"unexpected serverInfo.name: {init_result.serverInfo.name!r}"
                )
                experimental = init_result.capabilities.experimental or {}
                assert experimental.get("x-mux") == {"sharing": "isolated"}, (
                    f"host must advertise x-mux.sharing=isolated: {experimental}"
                )

                # tools/list forwards at least one authoritative tool unchanged.
                tools_result = await session.list_tools()
                proxied_names = {tool.name for tool in tools_result.tools}
                assert PARITY_TOOL_NAME in proxied_names, (
                    f"host tools/list is missing {PARITY_TOOL_NAME!r}: {sorted(proxied_names)}"
                )

                # One real read-only call/result: the tool is annotated
                # readOnlyHint=True and never launches or touches a target app.
                call_result = await session.call_tool(PARITY_TOOL_NAME, {"plan": MINIMAL_PLAN})
                data = _tool_payload(call_result)["data"]
                assert data["can_run"] is True, data
                assert data["status"] == "PASS", data
        # stdio_client's async-context exit runs the MCP-mandated shutdown
        # sequence (close stdin, wait, then terminate/kill the child) before
        # returning control here; the outer asyncio.wait_for fails the test
        # instead of the suite ever hanging if that teardown does not finish.

    host_errlog_path = tmp_path / "host-stderr.log"
    with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
        await asyncio.wait_for(_run_exchange(errlog), timeout=60)

    # A clean multi-round-trip exchange over the client's line-oriented
    # JSON-RPC stdout reader is itself proof stdout carried protocol frames
    # only -- a stray non-JSON-RPC line would have broken parsing above. The
    # forwarded Python diagnostic on stderr proves the channels stay separate.
    stderr_text = host_errlog_path.read_text(encoding="utf-8")
    assert "[DIAGNOSTIC] Startup CWD:" in stderr_text, (
        f"expected forwarded Python diagnostics on stderr, got:\n{stderr_text}"
    )


@pytest.mark.critical
def test_host_proxy_critical_fails_when_python_backend_is_missing(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """@critical category: behavioral - host refuses to serve without its Python child."""
    env = _backend_env()
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = str(tmp_path / "missing-python-executable")

    result = subprocess.run(
        ["dotnet", str(host_dll)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert result.stdout == b"", "a failed backend launch must not expose a partial MCP server"
    assert b"Failed to start the Python backend" in result.stderr

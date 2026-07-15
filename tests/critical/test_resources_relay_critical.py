"""Direct-Python ground truth for the FD-005 resources contract (Engram #391).

@critical
category: behavioral, data-consistency
features: mcp-resources-contract, dap-threads-resource
dev_stand: optional

Protects the exact contract ``host/NetCoreDbg.Mcp.Host/ResourcesRelay.cs`` must forward
unchanged: the four ``debug://`` resource URIs/names/mime types, the zero-template list,
successful reads for state/breakpoints/output, an invalid-URI protocol error, and a
*successful* ``debug://threads`` read during a live debug session (the DAP-backed resource
that legitimately errors while idle - see ``ResourcesRealPythonTests.cs`` for that idle-state
ground truth). This file never starts the .NET host; the host-relay proof against both a fake
and this exact real Python contract lives in ``host/NetCoreDbg.Mcp.Host.Tests/ResourcesRelayTests.cs``
and ``ResourcesRealPythonTests.cs``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SMOKE_PROJECT = REPO_ROOT / "tests" / "fixtures" / "SmokeTestApp"
SMOKE_DLL = SMOKE_PROJECT / "bin" / "Debug" / "net8.0-windows" / "SmokeTestApp.dll"

# The exact four-resource contract this repository publishes today. Kept here as the single
# source of truth the .NET-side tests reference in their own doc comments.
EXPECTED_RESOURCES: dict[str, tuple[str, str]] = {
    "debug://state": ("debug_state_resource", "application/json"),
    "debug://breakpoints": ("debug_breakpoints_resource", "application/json"),
    "debug://output": ("debug_output_resource", "text/plain"),
    "debug://threads": ("debug_threads_resource", "application/json"),
}


def _backend_env() -> dict[str, str]:
    env = get_default_environment()
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _resolve_netcoredbg() -> str | None:
    """NETCOREDBG_PATH env var, then PATH - never triggers the auto-download fallback, so a
    missing debugger skips this test deterministically instead of hitting the network."""
    configured = os.environ.get("NETCOREDBG_PATH")
    if configured and Path(configured).is_file():
        return configured
    return shutil.which("netcoredbg")


async def _wait_until_stopped(session: ClientSession, timeout_seconds: float = 5.0) -> None:
    """Polls debug://state until stopReason is populated (the async entry-stop event
    following start_debug), rather than guessing a fixed sleep duration."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        state = await session.read_resource("debug://state")  # type: ignore[arg-type]
        payload = json.loads(state.contents[0].text)
        if payload.get("stopReason") is not None:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(f"debuggee did not reach a stopped state in time: {payload}")
        await asyncio.sleep(0.1)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_resources_exact_four_uri_contract_and_empty_templates(tmp_path: Path) -> None:
    """@critical category: data-consistency - direct-Python resources/list and
    resources/templates/list are the exact four-URI, zero-template contract ResourcesRelay
    must preserve, and state/breakpoints/output read successfully while idle."""

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=_backend_env(),
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()

            resources_capability = init_result.capabilities.resources
            assert resources_capability is not None, "Python must advertise a resources capability"
            assert resources_capability.subscribe is False
            assert resources_capability.listChanged is False

            resources_result = await session.list_resources()
            assert resources_result.nextCursor is None
            actual = {str(r.uri): (r.name, r.mimeType) for r in resources_result.resources}
            assert actual == EXPECTED_RESOURCES

            templates_result = await session.list_resource_templates()
            assert templates_result.resourceTemplates == []
            assert templates_result.nextCursor is None

            state = await session.read_resource("debug://state")  # type: ignore[arg-type]
            assert state.contents[0].mimeType == "application/json"
            assert '"execState"' in state.contents[0].text

            breakpoints = await session.read_resource("debug://breakpoints")  # type: ignore[arg-type]
            assert breakpoints.contents[0].text == "{}"

            output = await session.read_resource("debug://output")  # type: ignore[arg-type]
            assert output.contents[0].mimeType == "text/plain"
            assert output.contents[0].text == ""

            with pytest.raises(McpError):
                await session.read_resource("debug://not-a-real-resource")  # type: ignore[arg-type]


@pytest.mark.critical
@pytest.mark.asyncio
async def test_resources_threads_reads_successfully_during_a_live_debug_session() -> None:
    """@critical category: behavioral - direct-Python debug://threads succeeds against a
    real running debug session, completing the successful-read ground truth for all four
    resources (state/breakpoints/output above, threads here)."""

    netcoredbg = _resolve_netcoredbg()
    if netcoredbg is None:
        pytest.skip("netcoredbg is required for a live debug session (NETCOREDBG_PATH or PATH)")
    if shutil.which("dotnet") is None:
        pytest.skip("dotnet CLI is required to build the SmokeTestApp fixture")

    build = subprocess.run(
        ["dotnet", "build", str(SMOKE_PROJECT), "-c", "Debug"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, f"SmokeTestApp build failed:\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    assert SMOKE_DLL.exists(), f"dotnet build did not produce {SMOKE_DLL}"

    env = _backend_env()
    env["NETCOREDBG_PATH"] = netcoredbg
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            start_result = await session.call_tool(
                "start_debug",
                {"program": str(SMOKE_DLL), "pre_build": False, "stop_at_entry": True},
            )
            assert not start_result.isError, start_result
            await _wait_until_stopped(session)

            threads = await session.read_resource("debug://threads")  # type: ignore[arg-type]
            assert threads.contents[0].mimeType == "application/json"
            assert "Main Thread" in threads.contents[0].text

            stop_result = await session.call_tool("stop_debug", {})
            assert not stop_result.isError, stop_result

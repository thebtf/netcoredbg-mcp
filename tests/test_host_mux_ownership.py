"""FD-007: mux session ownership proved through the real .NET compatibility host.

Builds (if needed) and starts ``host/NetCoreDbg.Mcp.Host`` over real stdio, with
a real ``netcoredbg-mcp`` Python backend spawned underneath it exactly as in
production (``NetCoreDbg.Mcp.Host.PythonBackendProcess``). Drives a real MCP
client (the official ``mcp`` SDK) through ``tools/call`` with distinct
``_meta.muxSessionId`` values - simulating the mcp-mux multiplexed-agent model
this compatibility host is built for - to prove Python's own
``SessionOwnership`` (same-owner mutation, competing-owner rejection,
always-permitted read-only observation, idle release, and disconnect) survives
the host's raw request/response forwarding unchanged, with exact result/error
parity against a direct (no host) Python session.

Skips only when the ``dotnet`` CLI itself is unavailable, matching
``tests/test_host_proxy.py``'s existing compatibility-proof contract. This
file intentionally does not repeat that file's initialize/tools-list/schema
parity assertions; it owns only the mux-ownership-specific proof T-FD007-01
adds.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
HOST_PROJECT_DIR = REPO_ROOT / "host" / "NetCoreDbg.Mcp.Host"
HOST_PROJECT = HOST_PROJECT_DIR / "NetCoreDbg.Mcp.Host.csproj"
HOST_DLL = HOST_PROJECT_DIR / "bin" / "Release" / "net8.0" / "NetCoreDbg.Mcp.Host.dll"

pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)


@pytest.fixture(scope="session")
def host_dll() -> Path:
    """Build the host exactly once per pytest process via incremental ``dotnet
    build``, so a stale gitignored binary from an earlier run can never
    silently satisfy this contract against current source."""
    result = subprocess.run(
        ["dotnet", "build", str(HOST_PROJECT), "-c", "Release"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"dotnet build failed for {HOST_PROJECT}:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert HOST_DLL.exists(), f"dotnet build did not produce {HOST_DLL}"
    return HOST_DLL


def _backend_env() -> dict[str, str]:
    env = get_default_environment()
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _payload(result: CallToolResult) -> dict:
    assert not result.isError, f"tool call reported isError: {result}"
    first = result.content[0]
    assert isinstance(first, TextContent), f"expected a text content block, got {first!r}"
    return json.loads(first.text)


def _host_params(
    host_dll: Path, project_root: Path, *, extra_env: dict[str, str] | None = None
) -> StdioServerParameters:
    env = _backend_env()
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    if extra_env:
        env.update(extra_env)
    return StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project-from-cwd"],
        env=env,
        cwd=str(project_root),
    )


def _direct_python_params(
    project_root: Path, *, extra_env: dict[str, str] | None = None
) -> StdioServerParameters:
    env = _backend_env()
    if extra_env:
        env.update(extra_env)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=env,
        cwd=str(project_root),
    )


async def _cleanup_processes(
    session: ClientSession, *, force: bool, mux_session_id: str | None
) -> dict:
    meta = {"muxSessionId": mux_session_id} if mux_session_id is not None else None
    return _payload(await session.call_tool("cleanup_processes", {"force": force}, meta=meta))


@pytest.mark.asyncio
async def test_host_proves_same_owner_competing_owner_and_read_only_observation(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    params = _host_params(host_dll, tmp_path)
    host_errlog_path = tmp_path / "host-stderr.log"

    async def _run() -> None:
        with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    first = await _cleanup_processes(session, force=True, mux_session_id="agent-A")
                    assert "error" not in first, first

                    second = await _cleanup_processes(session, force=True, mux_session_id="agent-A")
                    assert "error" not in second, second

                    denied = await _cleanup_processes(session, force=True, mux_session_id="agent-B")
                    assert "error" in denied, denied
                    assert "agent-A" in denied["error"]
                    assert "owned by another agent" in denied["error"]

                    observed = await _cleanup_processes(
                        session, force=False, mux_session_id="agent-B"
                    )
                    assert "error" not in observed, observed
                    assert observed["data"]["action"] == "status"

    await asyncio.wait_for(_run(), timeout=60)


@pytest.mark.asyncio
async def test_host_and_direct_python_produce_identical_denial_payload(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """Exact result/error parity: the same claim -> competing-claim sequence
    through the host and directly against Python must yield byte-identical
    JSON payloads for both the accepted claim and the denial."""

    async def _run_sequence(params: StdioServerParameters, errlog_path: Path) -> tuple[dict, dict]:
        with open(errlog_path, "w+", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    claimed = await _cleanup_processes(
                        session, force=True, mux_session_id="agent-A"
                    )
                    denied = await _cleanup_processes(session, force=True, mux_session_id="agent-B")
                    return claimed, denied

    host_root = tmp_path / "through-host"
    host_root.mkdir()
    direct_root = tmp_path / "direct-python"
    direct_root.mkdir()

    host_claimed, host_denied = await asyncio.wait_for(
        _run_sequence(_host_params(host_dll, host_root), host_root / "stderr.log"),
        timeout=60,
    )
    direct_claimed, direct_denied = await asyncio.wait_for(
        _run_sequence(_direct_python_params(direct_root), direct_root / "stderr.log"),
        timeout=60,
    )

    assert host_claimed == direct_claimed, (host_claimed, direct_claimed)
    assert host_denied == direct_denied, (host_denied, direct_denied)


@pytest.mark.asyncio
async def test_idle_ownership_expiry_releases_through_the_host(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    params = _host_params(host_dll, tmp_path, extra_env={"NETCOREDBG_SESSION_TIMEOUT": "0.3"})
    host_errlog_path = tmp_path / "host-stderr.log"

    async def _run() -> None:
        with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    claimed = await _cleanup_processes(
                        session, force=True, mux_session_id="agent-A"
                    )
                    assert "error" not in claimed, claimed

                    # Immediately after claiming, a competing session is still denied.
                    still_denied = await _cleanup_processes(
                        session, force=True, mux_session_id="agent-B"
                    )
                    assert "error" in still_denied, still_denied

                    await asyncio.sleep(0.6)

                    # Past NETCOREDBG_SESSION_TIMEOUT with no further activity from
                    # agent-A, ownership auto-releases and a new session can claim.
                    reclaimed = await _cleanup_processes(
                        session, force=True, mux_session_id="agent-B"
                    )
                    assert "error" not in reclaimed, reclaimed

    await asyncio.wait_for(_run(), timeout=60)


@pytest.mark.asyncio
async def test_ownership_does_not_survive_a_full_disconnect_and_fresh_relaunch(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """Disconnect: the host/Python child pairing is 1:1 and torn down on
    downstream disconnect, so ownership claimed in one session can never leak
    into a brand-new session's freshly constructed SessionOwnership."""
    first_root = tmp_path / "first-session"
    first_root.mkdir()
    second_root = tmp_path / "second-session"
    second_root.mkdir()

    async def _claim(project_root: Path, errlog_path: Path) -> dict:
        with open(errlog_path, "w+", encoding="utf-8") as errlog:
            async with stdio_client(_host_params(host_dll, project_root), errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await _cleanup_processes(session, force=True, mux_session_id="agent-A")

    first_claim = await asyncio.wait_for(_claim(first_root, first_root / "stderr.log"), timeout=60)
    assert "error" not in first_claim, first_claim

    # A brand-new host process (and therefore a brand-new Python child with a
    # fresh in-memory SessionOwnership) sees no contention from a *different*
    # agent, even though the first session's claim was never explicitly released.
    async def _claim_other(project_root: Path, errlog_path: Path) -> dict:
        with open(errlog_path, "w+", encoding="utf-8") as errlog:
            async with stdio_client(_host_params(host_dll, project_root), errlog=errlog) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await _cleanup_processes(session, force=True, mux_session_id="agent-B")

    second_claim = await asyncio.wait_for(
        _claim_other(second_root, second_root / "stderr.log"), timeout=60
    )
    assert "error" not in second_claim, second_claim

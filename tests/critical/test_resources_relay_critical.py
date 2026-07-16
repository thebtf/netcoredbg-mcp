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
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

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


def _resource_update_handler(queue: asyncio.Queue[str]):
    async def handle(message) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ResourceUpdatedNotification
        ):
            queue.put_nowait(str(message.root.params.uri))

    return handle


async def _collect_updates(
    queue: asyncio.Queue[str], expected: set[str], timeout_seconds: float
) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    seen: list[str] = []
    while not expected.issubset(seen):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"Missing resource updates: {expected - set(seen)}; seen={seen}")
        seen.append(await asyncio.wait_for(queue.get(), timeout=remaining))
    return seen

async def _wait_for_state_update(
    session: ClientSession,
    queue: asyncio.Queue[str],
    expected_exec_states: set[str],
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(
                f"Missing state update for execState in {expected_exec_states}"
            )
        uri = await asyncio.wait_for(queue.get(), timeout=remaining)
        if uri != "debug://state":
            continue
        state = await session.read_resource("debug://state")  # type: ignore[arg-type]
        payload: dict[str, object] = json.loads(state.contents[0].text)
        if payload.get("execState") in expected_exec_states:
            return payload


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
            assert resources_capability.subscribe is True
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


@pytest.mark.critical
@pytest.mark.asyncio
async def test_resource_subscriptions_are_idempotent_and_stop_after_unsubscribe(
    tmp_path: Path,
) -> None:
    """Real direct-Python proof for capability, duplicate subscribe, URI error, and unsubscribe."""
    (tmp_path / "Probe.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8"
    )
    source = tmp_path / "Program.cs"
    source.write_text("class Program { static void Main() {} }\n", encoding="utf-8")
    updates: asyncio.Queue[str] = asyncio.Queue()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=_backend_env(),
        cwd=str(tmp_path),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            message_handler=_resource_update_handler(updates),
        ) as session:
            initialized = await session.initialize()
            assert initialized.capabilities.resources is not None
            assert initialized.capabilities.resources.subscribe is True
            assert initialized.capabilities.resources.listChanged is False

            with pytest.raises(McpError, match="Unknown resource") as unknown:
                await session.subscribe_resource(AnyUrl("debug://unknown"))
            assert unknown.value.error.code == -32602

            await session.subscribe_resource(AnyUrl("debug://breakpoints"))
            await session.subscribe_resource(AnyUrl("debug://breakpoints"))
            added = await session.call_tool(
                "add_breakpoint",
                {"file": str(source), "line": 1},
            )
            assert not added.isError, added
            assert await asyncio.wait_for(updates.get(), timeout=5) == "debug://breakpoints"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(updates.get(), timeout=0.3)

            await session.unsubscribe_resource(AnyUrl("debug://breakpoints"))
            removed = await session.call_tool(
                "remove_breakpoint",
                {"file": str(source), "line": 1},
            )
            assert not removed.isError, removed
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(updates.get(), timeout=0.3)


@pytest.mark.critical
@pytest.mark.asyncio
async def test_live_dap_mutations_update_state_output_threads_and_termination() -> None:
    """Real direct-Python proof for asynchronous DAP resource mutation triggers."""
    netcoredbg = _resolve_netcoredbg()
    if netcoredbg is None:
        pytest.skip("netcoredbg is required for live resource updates")
    if shutil.which("dotnet") is None:
        pytest.skip("dotnet CLI is required to build the SmokeTestApp fixture")

    build = subprocess.run(
        ["dotnet", "build", str(SMOKE_PROJECT), "-c", "Debug"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    updates: asyncio.Queue[str] = asyncio.Queue()
    env = _backend_env()
    env["NETCOREDBG_PATH"] = netcoredbg
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=env,
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            message_handler=_resource_update_handler(updates),
        ) as session:
            await session.initialize()
            for uri in ("debug://state", "debug://output", "debug://threads"):
                await session.subscribe_resource(AnyUrl(uri))

            started = await session.call_tool(
                "start_debug",
                {
                    "program": str(SMOKE_DLL),
                    "args": ["longrun"],
                    "pre_build": False,
                    "stop_at_entry": False,
                },
            )
            assert not started.isError, started
            seen = await _collect_updates(
                updates,
                {"debug://state", "debug://output", "debug://threads"},
                timeout_seconds=10,
            )
            assert set(seen) >= {"debug://state", "debug://output", "debug://threads"}

            await session.unsubscribe_resource(AnyUrl("debug://output"))
            while not updates.empty():
                updates.get_nowait()
            deadline = asyncio.get_running_loop().time() + 0.8
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                try:
                    uri = await asyncio.wait_for(updates.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                assert uri != "debug://output"

            while not updates.empty():
                updates.get_nowait()
            terminal = await _wait_for_state_update(
                session,
                updates,
                {"terminated"},
                timeout_seconds=10,
            )
            assert terminal["execState"] == "terminated"

            await session.subscribe_resource(AnyUrl("debug://output"))
            while not updates.empty():
                updates.get_nowait()
            cleared = await session.call_tool("get_output", {"clear": True})
            assert not cleared.isError, cleared
            assert await asyncio.wait_for(updates.get(), timeout=5) == "debug://output"
            output = await session.read_resource("debug://output")  # type: ignore[arg-type]
            assert output.contents[0].text == ""

            stop_result = await session.call_tool("stop_debug", {})
            assert not stop_result.isError, stop_result

            target = await asyncio.create_subprocess_exec(
                "dotnet",
                str(SMOKE_DLL),
                "longrun",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.sleep(0.2)
                while not updates.empty():
                    updates.get_nowait()
                attached = await session.call_tool(
                    "attach_debug",
                    {"process_id": target.pid},
                )
                assert not attached.isError, attached
                attached_updates = await _collect_updates(
                    updates,
                    {"debug://state", "debug://output", "debug://threads"},
                    timeout_seconds=10,
                )
                assert set(attached_updates) >= {
                    "debug://state",
                    "debug://output",
                    "debug://threads",
                }

                while not updates.empty():
                    updates.get_nowait()
                terminated = await session.call_tool("terminate_debug", {})
                assert not terminated.isError, terminated
                final_state = await _wait_for_state_update(
                    session,
                    updates,
                    {"idle", "terminated"},
                    timeout_seconds=10,
                )
                assert final_state["execState"] in {"idle", "terminated"}
            finally:
                if target.returncode is None:
                    try:
                        target.kill()
                    except ProcessLookupError:
                        pass
                await target.wait()

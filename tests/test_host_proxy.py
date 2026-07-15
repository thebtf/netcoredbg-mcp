"""Process-level compatibility proof for the .NET MCP host (CR-002).

Builds (if needed) and starts ``host/NetCoreDbg.Mcp.Host`` over real stdio,
with the interpreter running pytest as its upstream ``netcoredbg-mcp``
backend. Drives a real MCP client (the official ``mcp`` SDK, already a
project dependency) through initialize, tools/list, and tools/call to prove:

* the host's initialize response advertises the isolated ``x-mux`` sharing
  capability and a tools capability;
* tools/list forwards the authoritative runtime-smoke replay tool family
  unchanged from the Python server, and ``runtime_smoke_validate_plan``'s
  full schema/annotations payload is structurally identical (field-for-field
  dict equality) to what the same Python server advertises directly (not
  merely name parity);
* both ``--project`` and ``--project-from-cwd`` reach Python unchanged, including
  shell metacharacters in the explicit project path, and the child's working
  directory is preserved: a *relative* ``plan_path`` only resolves through
  ``runtime_smoke_validate_plan`` if all of those conditions hold;
* an inline invalid plan still returns Python's own structured validation
  result, not a proxy-level error;
* only MCP protocol frames travel on stdout -- a successful multi-round-trip
  exchange over the client's line-oriented JSON-RPC reader is itself proof of
  that, and forwarded diagnostics are captured separately on stderr.

This is a permanent compatibility contract, not an opt-in diagnostic: it
skips only when the ``dotnet`` CLI itself is unavailable. Neither this test
nor the host adds a new third-party dependency; the ``mcp`` client SDK is
already required by the project.
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
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent, Tool

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
HOST_PROJECT_DIR = REPO_ROOT / "host" / "NetCoreDbg.Mcp.Host"
HOST_PROJECT = HOST_PROJECT_DIR / "NetCoreDbg.Mcp.Host.csproj"
HOST_DLL = HOST_PROJECT_DIR / "bin" / "Release" / "net8.0" / "NetCoreDbg.Mcp.Host.dll"

pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)

# Authoritative UI replay API (see README.md, "Runtime Smoke Evidence").
# The host must proxy every one of these names unchanged from the Python server.
REPLAY_TOOL_FAMILY = frozenset(
    {
        "runtime_smoke_validate_plan",
        "runtime_smoke_run_plan",
        "runtime_smoke_validate_probe",
        "runtime_smoke_run_probe",
        "runtime_smoke_start",
        "runtime_smoke_wait_for_result",
        "runtime_smoke_evidence_bundle",
        "runtime_smoke_mark_event_cursor",
        "runtime_smoke_get_event_delta",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
        "runtime_smoke_stop",
        "runtime_smoke_cleanup_contract",
        "run_runtime_smoke",
    }
)

# Tool whose full schema/annotations payload gets compared field-for-field
# (structural dict equality) against a direct (non-proxied) Python session,
# not just checked by name.
PARITY_TOOL_NAME = "runtime_smoke_validate_plan"

# JSON-RPC 2.0 "Method not found" - the standard code direct Python itself returns for
# logging/setLevel and every other MCP method it does not implement.
METHOD_NOT_FOUND_ERROR_CODE = -32601

# Known-valid minimal runtime-smoke plan shape (mirrors the fixture already
# proven by tests/test_runtime_smoke_run_plan_facade.py).
MINIMAL_PLAN = {
    "name": "netcoredbg-mcp-host-proxy-check",
    "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
}


@pytest.fixture(scope="session")
def host_dll() -> Path:
    """Build the host exactly once per pytest process via incremental
    ``dotnet build``, so a stale gitignored binary left over from an earlier
    run can never silently satisfy this contract against current
    Program.cs/csproj source. MSBuild's own incremental build keeps repeat
    invocations across pytest processes fast when nothing changed."""
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


def _tool_payload(result: CallToolResult) -> dict:
    """Extract the JSON payload a runtime_smoke tool returns as text content."""
    assert not result.isError, f"tool call reported isError: {result}"
    assert result.content, "tool call returned no content blocks"
    first = result.content[0]
    assert isinstance(first, TextContent), f"expected a text content block, got {first!r}"
    return json.loads(first.text)


def _find_tool(tools: list[Tool], name: str) -> Tool:
    for tool in tools:
        if tool.name == name:
            return tool
    raise AssertionError(f"tool {name!r} not found in {sorted(t.name for t in tools)}")


async def _direct_python_tool_schema(project_args: list[str], cwd: Path, errlog_path: Path) -> dict:
    """Ground truth: the schema Python itself advertises for PARITY_TOOL_NAME,
    with no .NET host in between."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", *project_args],
        env=_backend_env(),
        cwd=str(cwd),
    )
    with open(errlog_path, "w+", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool = _find_tool(tools_result.tools, PARITY_TOOL_NAME)
                return tool.model_dump(mode="json", by_alias=True, exclude_none=True)


# Populated once per pytest process: tool schema is independent of CLI mode
# (--project vs --project-from-cwd), so the ground-truth direct-Python fetch
# must not be repeated for every parametrized host exchange.
_direct_schema_cache: dict[str, dict] = {}


async def _cached_direct_tool_schema(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if "schema" not in _direct_schema_cache:
        cwd = tmp_path_factory.mktemp("direct-python")
        _direct_schema_cache["schema"] = await _direct_python_tool_schema(
            ["--project-from-cwd"], cwd, cwd / "python-direct-stderr.log"
        )
    return _direct_schema_cache["schema"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_mode", ["project_flag", "project_from_cwd"])
async def test_host_proxies_initialize_tools_list_and_validate_plan(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    cli_mode: str,
    host_dll: Path,
) -> None:
    # Use a legal Windows path that would be changed by cmd.exe variable expansion.
    # The host must launch Python directly and preserve this literal path in --project.
    session_root = tmp_path / "project&%NETCOREDBG_MCP_ARG_SENTINEL%"
    session_root.mkdir()

    # A *relative* plan_path: it only resolves to this exact file if the host
    # forwarded --project/--project-from-cwd unchanged AND preserved the
    # Python child's working directory (both are required for this to pass).
    plan_path = session_root / "runtime-smoke-plan.json"
    plan_path.write_text(json.dumps(MINIMAL_PLAN), encoding="utf-8")

    project_args = (
        ["--project", str(session_root)] if cli_mode == "project_flag" else ["--project-from-cwd"]
    )

    env = _backend_env()
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    env["NETCOREDBG_MCP_ARG_SENTINEL"] = "EXPANDED_BY_COMMAND_SHELL"

    params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), *project_args],
        env=env,
        cwd=str(session_root),
    )

    proxied_schema: dict = {}

    async def _run_exchange(errlog) -> None:
        nonlocal proxied_schema
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()

                experimental = init_result.capabilities.experimental or {}
                assert experimental.get("x-mux") == {"sharing": "isolated"}, (
                    "host must advertise x-mux.sharing=isolated (mcp-mux session isolation); "
                    f"got: {experimental}"
                )
                assert init_result.capabilities.tools is not None, (
                    "host must advertise a tools capability"
                )
                assert init_result.capabilities.logging is None, (
                    "host must not advertise a logging capability until FD-002 implements "
                    "it for real - SDK 1.4.1 forces this capability on by default unless "
                    "RelayRouteCatalog.SuppressUnregisteredLogging's outgoing filter strips "
                    f"it; got: {init_result.capabilities.logging}"
                )

                # ping is answered by the SDK itself, never by a relay route: proves the
                # downstream session is alive independent of any tools/list or tools/call
                # forwarding path.
                await session.send_ping()

                # Capability-behavior parity, not just the advertised flag: Python itself
                # rejects logging/setLevel with "Method not found", and so must the host,
                # until FD-002 registers a real, negotiated logging route.
                with pytest.raises(McpError) as logging_error:
                    await session.set_logging_level("info")
                assert logging_error.value.error.code == METHOD_NOT_FOUND_ERROR_CODE, (
                    "host logging/setLevel must reject like direct Python; got: "
                    f"{logging_error.value.error}"
                )

                tools_result = await session.list_tools()
                proxied_names = {tool.name for tool in tools_result.tools}
                missing = REPLAY_TOOL_FAMILY - proxied_names
                assert not missing, (
                    f"host tools/list is missing authoritative replay tools: {sorted(missing)}"
                )
                proxied_schema = _find_tool(tools_result.tools, PARITY_TOOL_NAME).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )

                # --project/--project-from-cwd forwarding + working-directory
                # preservation, proven via relative plan_path resolution.
                valid_result = await session.call_tool(
                    "runtime_smoke_validate_plan",
                    {"plan_path": "runtime-smoke-plan.json"},
                )
                valid_payload = _tool_payload(valid_result)
                data = valid_payload["data"]
                assert data["can_run"] is True, data
                assert data["status"] == "PASS", data
                plan_source = data["plan_source"]
                assert plan_source["kind"] == "file"
                assert plan_source["format"] == "json"
                assert Path(plan_source["path"]).samefile(plan_path)

                # Cheap inline-plan assertion: Python's structured INVALID_SETUP
                # result comes back through the proxy unchanged, not a host error.
                invalid_result = await session.call_tool(
                    "runtime_smoke_validate_plan",
                    {"plan": {"not_a_real_field": True}},
                )
                invalid_payload = _tool_payload(invalid_result)
                invalid_data = invalid_payload["data"]
                assert invalid_data["can_run"] is False, invalid_data
                assert invalid_data["status"] == "INVALID_SETUP", invalid_data
                assert invalid_data["validation_errors"], invalid_data

    # Windows subprocess creation needs a real, fileno()-backed stderr target
    # (an in-memory io.StringIO is rejected), so capture into temp files.
    host_errlog_path = tmp_path / "host-stderr.log"
    with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
        await asyncio.wait_for(_run_exchange(errlog), timeout=60)

    # A clean multi-round-trip exchange over the client's line-oriented
    # JSON-RPC stdout reader is itself proof stdout carried protocol frames
    # only -- any stray non-JSON-RPC line would have broken parsing above.
    # A backend-only marker proves Python stderr crossed the .NET proxy boundary.
    stderr_text = host_errlog_path.read_text(encoding="utf-8")
    assert "[DIAGNOSTIC] Startup CWD:" in stderr_text, (
        f"expected forwarded Python diagnostics on stderr, got:\n{stderr_text}"
    )

    # Full-schema parity: names alone don't prove schemas/annotations survive
    # the proxy's C#-typed round trip unchanged. Fetched once per pytest
    # process (cli_mode does not affect the schema).
    direct_schema = await asyncio.wait_for(
        _cached_direct_tool_schema(tmp_path_factory),
        timeout=60,
    )
    assert proxied_schema == direct_schema, (
        "host tools/list must preserve the Python tool schema/annotations unchanged:\n"
        f"proxied: {proxied_schema}\ndirect:  {direct_schema}"
    )


@pytest.mark.asyncio
async def test_direct_python_has_no_logging_capability(tmp_path: Path) -> None:
    """Ground truth for the host's logging-suppression parity claim above: direct Python
    (no .NET host involved) advertises no logging capability and rejects logging/setLevel
    with "Method not found" on its own. If Python ever grew real logging support, this
    would fail first and point straight at RelayRouteCatalog.SuppressUnregisteredLogging
    needing to become a real FD-002 route instead of a suppression."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project-from-cwd"],
        env=_backend_env(),
        cwd=str(tmp_path),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            assert init_result.capabilities.logging is None

            with pytest.raises(McpError) as logging_error:
                await session.set_logging_level("info")
            assert logging_error.value.error.code == METHOD_NOT_FOUND_ERROR_CODE


def test_host_fails_before_serving_when_python_executable_is_missing(
    tmp_path: Path,
    host_dll: Path,
) -> None:
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

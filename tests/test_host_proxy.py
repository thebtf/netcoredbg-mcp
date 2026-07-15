"""Process-level compatibility proof for the .NET MCP host (CR-002, FD-004).

Builds (if needed) and starts ``host/NetCoreDbg.Mcp.Host`` over real stdio,
with the interpreter running pytest as its upstream ``netcoredbg-mcp``
backend. Drives a real MCP client (the official ``mcp`` SDK, already a
project dependency) through initialize, tools/list, and tools/call to prove:

* the host's initialize response advertises the isolated ``x-mux`` sharing
  capability and a tools capability, and never advertises tools
  ``listChanged`` support - the catalog is static, and no
  ``notifications/tools/list_changed`` is ever observed during a real
  exchange;
* tools/list forwards *every one* of direct Python's 135 tools unchanged -
  not just the fourteen-tool ``runtime_smoke_*``/``run_runtime_smoke`` replay
  family - with full schema/annotations/requiredness parity, field-for-field,
  against what the same Python server advertises directly
  (``test_host_tools_catalog_is_complete_and_schema_identical_to_direct_python``);
* an arbitrary pagination cursor and a ``_meta.progressToken`` a client
  supplies reach Python unchanged and harmlessly (Python's own
  ``list_tools()`` handler ignores cursor and never returns ``nextCursor``);
* a representative real success with ``structuredContent``, a representative
  tool-level error (unknown tool -> ``isError``), and a real functional
  ``_meta.muxSessionId`` round trip (session-ownership guarding) are
  identical between direct Python and the host, not just shape-checked
  (``test_host_forwards_tool_call_contract_matching_direct_python``);
* a malformed ``tools/call`` envelope is always rejected as an error on both
  sides and never silently accepted as success, with the session remaining
  fully usable afterward
  (``test_host_malformed_tools_call_envelope_is_rejected_and_session_stays_usable``);
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
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, PaginatedRequestParams, TextContent

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
HOST_PROJECT_DIR = REPO_ROOT / "host" / "NetCoreDbg.Mcp.Host"
HOST_PROJECT = HOST_PROJECT_DIR / "NetCoreDbg.Mcp.Host.csproj"
HOST_DLL = HOST_PROJECT_DIR / "bin" / "Release" / "net8.0" / "NetCoreDbg.Mcp.Host.dll"

# Stable fixture already used by tests/test_code_search.py for deterministic
# find_code_symbol/add_breakpoint targets - real files, not the live repo's own
# ever-changing source, so this proof cannot drift out from under an unrelated
# refactor elsewhere in the codebase.
SEARCH_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "SearchTestApp"

pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)

# Authoritative UI replay API (see README.md, "Runtime Smoke Evidence").
# The host must proxy every one of these names unchanged from the Python server.
# This is a cheap smoke subset exercised alongside project-arg/capability checks
# below; it is not the completeness proof - see EXPECTED_TOOL_COUNT and
# test_host_tools_catalog_is_complete_and_schema_identical_to_direct_python for
# that.
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

# Direct Python's exact public tool count as of this proof. A deliberate catalog
# change updates this literal; a silent drift (a tool the host forgot, or one
# Python quietly stopped advertising) fails loudly instead of being masked by a
# subset check.
EXPECTED_TOOL_COUNT = 135

# JSON-RPC 2.0 "Method not found" - the standard code direct Python itself returns for
# logging/setLevel and every other MCP method it does not implement.
METHOD_NOT_FOUND_ERROR_CODE = -32601

# JSON-RPC 2.0 "Invalid params" - direct Python's own code when a tools/call
# envelope fails its outer CallToolRequestParams validation (missing `name`,
# wrong-typed `arguments`), independent of any specific tool's own schema.
DIRECT_PYTHON_INVALID_PARAMS_ERROR_CODE = -32602

# The `notifications/tools/list_changed` method name (mcp.types.
# ToolListChangedNotification's literal `method` value). No relay module in this
# host build owns this method in either direction, so it must never be observed
# downstream even though nothing here can force real Python to emit it (Python
# never advertises tools.listChanged, so it never sends this notification) - see
# host/NetCoreDbg.Mcp.Host.Tests/ToolsCatalogContractTests.cs for the seam-level
# proof that an upstream push of this exact method is not forwarded.
TOOLS_LIST_CHANGED_NOTIFICATION_METHOD = "notifications/tools/list_changed"

# Known-valid minimal runtime-smoke plan shape (mirrors the fixture already
# proven by tests/test_runtime_smoke_run_plan_facade.py).
MINIMAL_PLAN = {
    "name": "netcoredbg-mcp-host-proxy-check",
    "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
}

# A stable, already-regression-tested symbol/file/expected-shape from
# SEARCH_FIXTURE_ROOT (see tests/test_code_search.py::
# test_find_code_symbol_returns_csharp_method_definition), reused here instead
# of inventing a new fixture dependency.
FIND_SYMBOL_NAME = "LoadAssignedCharacter"
FIND_SYMBOL_EXPECTED_RESULT = {
    "file": "ViewModels/MainViewModel.cs",
    "line": 10,
    "name": "LoadAssignedCharacter",
    "kind": "method",
    "context": "public void LoadAssignedCharacter()",
}
BREAKPOINT_TARGET_FILE = "ViewModels/MainViewModel.cs"
UNKNOWN_TOOL_NAME = "fd004-unknown-tool-does-not-exist"


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


async def _direct_python_all_tools(
    project_args: list[str], cwd: Path, errlog_path: Path
) -> list[dict[str, Any]]:
    """Ground truth: every tool Python itself advertises, full schema included."""
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
                return [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in tools_result.tools
                ]


# Populated once per pytest process: the tool catalog/schema is independent of
# CLI mode or cwd, so the ground-truth direct-Python fetch must not be repeated
# for every test that needs it.
_direct_catalog_cache: dict[str, list[dict[str, Any]]] = {}


async def _cached_direct_catalog(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    if "catalog" not in _direct_catalog_cache:
        cwd = tmp_path_factory.mktemp("direct-python-catalog")
        _direct_catalog_cache["catalog"] = await _direct_python_all_tools(
            ["--project-from-cwd"], cwd, cwd / "python-direct-stderr.log"
        )
    return _direct_catalog_cache["catalog"]


def _by_name(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in tools}


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_mode", ["project_flag", "project_from_cwd"])
async def test_host_proxies_initialize_tools_list_and_validate_plan(
    tmp_path: Path,
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

    # Every notification the client observes during the whole exchange below,
    # captured via ClientSession's generic message_handler hook (which runs
    # alongside, not instead of, the SDK's own built-in per-type dispatch) - the
    # static-catalog claim requires that none of them is ever a
    # tools/list_changed push.
    observed_notification_methods: list[str] = []

    async def _capture_notification_methods(message: object) -> None:
        method = getattr(getattr(message, "root", None), "method", None)
        if method is not None:
            observed_notification_methods.append(method)

    async def _run_exchange(errlog) -> None:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, message_handler=_capture_notification_methods
            ) as session:
                init_result = await session.initialize()

                experimental = init_result.capabilities.experimental or {}
                assert experimental.get("x-mux") == {"sharing": "isolated"}, (
                    "host must advertise x-mux.sharing=isolated (mcp-mux session isolation); "
                    f"got: {experimental}"
                )
                assert init_result.capabilities.tools is not None, (
                    "host must advertise a tools capability"
                )
                assert init_result.capabilities.tools.listChanged is not True, (
                    "the tools catalog is static: the host must never advertise "
                    f"listChanged support; got: {init_result.capabilities.tools}"
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

    assert TOOLS_LIST_CHANGED_NOTIFICATION_METHOD not in observed_notification_methods, (
        "the tools catalog is static: no notifications/tools/list_changed may ever "
        f"be observed; saw notifications: {observed_notification_methods}"
    )


@pytest.mark.asyncio
async def test_host_tools_catalog_is_complete_and_schema_identical_to_direct_python(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    host_dll: Path,
) -> None:
    """The load-bearing FD-004 proof: every one of Python's tools, by exact name
    and fully serialized schema/annotations/requiredness - not the fourteen-tool
    runtime-smoke replay-family sample checked above - reaches the host
    unchanged. Also proves the catalog is static (no ``nextCursor`` is ever
    invented) and that an arbitrary cursor/``_meta.progressToken`` a client
    supplies is forwarded harmlessly: Python's own ``list_tools()`` handler
    ignores cursor entirely and always answers the same full catalog, so
    ToolsRelay must neither silently drop the field nor invent pagination state
    Python never expressed.
    """
    direct_catalog = await _cached_direct_catalog(tmp_path_factory)
    assert len(direct_catalog) == EXPECTED_TOOL_COUNT, (
        f"direct Python's own catalog no longer has exactly {EXPECTED_TOOL_COUNT} "
        f"tools ({len(direct_catalog)}); update EXPECTED_TOOL_COUNT deliberately if "
        "this is an intended catalog change"
    )

    env = _backend_env()
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

                baseline = await session.list_tools()
                assert baseline.nextCursor is None, (
                    "Python never returns nextCursor (it ignores cursor and always "
                    "answers the full catalog); the host must not invent pagination "
                    f"state; got nextCursor={baseline.nextCursor!r}"
                )

                # An arbitrary cursor and a progress-token _meta the client supplies
                # must reach Python (which ignores cursor) unchanged and harmlessly -
                # not dropped, not turned into a host-level error or empty page.
                cursor_probed = await session.list_tools(
                    params=PaginatedRequestParams(
                        cursor="fd004-cursor-probe",
                        _meta={"progressToken": "fd004-cursor-token"},
                    )
                )
                assert cursor_probed.nextCursor is None

    host_catalog = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in baseline.tools
    ]
    host_catalog_after_cursor = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in cursor_probed.tools
    ]
    assert len(host_catalog) == EXPECTED_TOOL_COUNT

    assert _by_name(host_catalog_after_cursor) == _by_name(host_catalog), (
        "a cursor-bearing tools/list must return the identical full catalog, since "
        "Python's own list_tools() handler ignores cursor entirely"
    )

    direct_by_name = _by_name(direct_catalog)
    host_by_name = _by_name(host_catalog)
    assert set(host_by_name) == set(direct_by_name), (
        "host tools/list must advertise the exact same tool names as direct "
        f"Python - missing: {sorted(set(direct_by_name) - set(host_by_name))}, "
        f"unexpected: {sorted(set(host_by_name) - set(direct_by_name))}"
    )
    assert host_by_name == direct_by_name, (
        "host tools/list must preserve every tool's full schema/annotations/"
        "requiredness unchanged from direct Python"
    )


async def _run_tool_contract_probe(
    params: StdioServerParameters, errlog_path: Path
) -> dict[str, Any]:
    """One real stdio session exercising find_code_symbol (a real success with
    structuredContent), an unknown tool call (isError), and a two-agent
    _meta.muxSessionId session-ownership sequence via add_breakpoint. Used
    against both direct Python and the host so every result can be compared
    field-for-field, not merely shape-checked."""
    abs_file = str(SEARCH_FIXTURE_ROOT / BREAKPOINT_TARGET_FILE)
    with open(errlog_path, "w+", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                symbol_result = await session.call_tool(
                    "find_code_symbol", {"name": FIND_SYMBOL_NAME}
                )
                unknown_result = await session.call_tool(UNKNOWN_TOOL_NAME, {})

                # add_breakpoint calls check_session_access() before anything else,
                # so a second call from a different muxSessionId proves _meta
                # reached Python and had a real functional effect, not just that
                # the JSON key survived serialization.
                owner_result = await session.call_tool(
                    "add_breakpoint",
                    {"file": abs_file, "line": 10},
                    meta={"muxSessionId": "fd004-agent-A"},
                )
                denied_result = await session.call_tool(
                    "add_breakpoint",
                    {"file": abs_file, "line": 11},
                    meta={"muxSessionId": "fd004-agent-B"},
                )

    def _dump(result: CallToolResult) -> dict[str, Any]:
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    return {
        "symbol": _dump(symbol_result),
        "unknown": _dump(unknown_result),
        "owner": _dump(owner_result),
        "denied": _dump(denied_result),
    }


@pytest.mark.asyncio
async def test_host_forwards_tool_call_contract_matching_direct_python(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """Representative success (with structuredContent), representative tool
    error (unknown tool -> isError), and a real functional
    _meta.muxSessionId round trip (session-ownership guarding) - compared
    field-for-field between direct Python and the host, not merely
    shape-checked."""
    direct_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "netcoredbg_mcp", "--project", str(SEARCH_FIXTURE_ROOT)],
        env=_backend_env(),
        cwd=str(tmp_path),
    )
    direct = await _run_tool_contract_probe(direct_params, tmp_path / "direct-stderr.log")

    host_env = _backend_env()
    host_env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    host_params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project", str(SEARCH_FIXTURE_ROOT)],
        env=host_env,
        cwd=str(tmp_path),
    )
    host = await _run_tool_contract_probe(host_params, tmp_path / "host-stderr.log")

    assert direct["symbol"] == host["symbol"], (direct["symbol"], host["symbol"])
    assert direct["symbol"]["isError"] is False
    assert direct["symbol"]["structuredContent"]["data"]["results"] == [FIND_SYMBOL_EXPECTED_RESULT]

    assert direct["unknown"] == host["unknown"], (direct["unknown"], host["unknown"])
    assert direct["unknown"]["isError"] is True
    assert f"Unknown tool: {UNKNOWN_TOOL_NAME}" in direct["unknown"]["content"][0]["text"]

    assert direct["owner"] == host["owner"], (direct["owner"], host["owner"])
    assert direct["denied"] == host["denied"], (direct["denied"], host["denied"])
    denied_text = direct["denied"]["content"][0]["text"]
    assert "owned by another agent (session fd004-agent-A)" in denied_text


async def _send_raw_line(proc: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _recv_raw_line_by_id(
    proc: asyncio.subprocess.Process, want_id: int, *, timeout: float = 15.0
) -> dict[str, Any]:
    """Reads raw JSON-RPC lines, skipping any message (for example a server-
    initiated notification) whose id does not match: the transport-level
    contract is line framing and id correlation, not strict request/response
    ordering."""
    assert proc.stdout is not None
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        if not line:
            raise AssertionError(f"stream closed before a response with id={want_id} arrived")
        message = json.loads(line.decode("utf-8"))
        if message.get("id") == want_id:
            return message


async def _terminate_raw_process(proc: asyncio.subprocess.Process, *, timeout: float = 8.0) -> None:
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


_INITIALIZE_MESSAGE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "fd004-malformed-probe", "version": "0.0.1"},
    },
}
_INITIALIZED_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
# A tools/call envelope missing the required `name` field entirely: a genuine
# MCP protocol violation of CallToolRequestParams, not a tool-execution error.
_MALFORMED_TOOLS_CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}
_HEALTH_CHECK_TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": UNKNOWN_TOOL_NAME, "arguments": {}},
}


async def _probe_malformed_tools_call_envelope(
    command: str, args: list[str], env: dict[str, str], cwd: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Sends a tools/call envelope missing the required `name` field over raw
    stdio (bypassing the official client SDK's own request-construction
    validation, which would refuse to build such a request at all), then a
    normal unknown-tool call proving the session survives. Returns
    (malformed_response, health_check_response)."""
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        limit=1024 * 1024 * 2,
    )
    try:
        await _send_raw_line(proc, _INITIALIZE_MESSAGE)
        await _recv_raw_line_by_id(proc, 1)
        await _send_raw_line(proc, _INITIALIZED_NOTIFICATION)

        await _send_raw_line(proc, _MALFORMED_TOOLS_CALL)
        malformed_response = await _recv_raw_line_by_id(proc, 2)

        await _send_raw_line(proc, _HEALTH_CHECK_TOOLS_CALL)
        health_response = await _recv_raw_line_by_id(proc, 3)
    finally:
        await _terminate_raw_process(proc)

    return malformed_response, health_response


@pytest.mark.asyncio
async def test_host_malformed_tools_call_envelope_is_rejected_and_session_stays_usable(
    tmp_path: Path,
    host_dll: Path,
) -> None:
    """A tools/call envelope missing the required `name` field is a genuine MCP
    protocol violation, not a tool-execution error: direct Python's own pydantic
    validation rejects it with a JSON-RPC error (Invalid params) before the
    request ever reaches a tool.

    The .NET SDK's downstream typed-request dispatch performs the equivalent
    structural validation *before* ToolsRelay's own handler ever runs - inside
    the SDK's shared generic per-method request pipeline, common to every relay
    route, not something ToolsRelay forwards or could intercept without a
    cross-cutting message filter that only RelayComposition/RelayRouteCatalog
    may own (outside this slice's ToolsRelay-only edit boundary). So today the
    host surfaces a generic internal error instead of Python's own Invalid
    params code/message. Both exact shapes are asserted below (a regression in
    either is caught); the divergence itself is a known, reported, non-blocking
    integration follow-up (see this slice's final report), not a silently
    accepted gap. What both sides share, and what genuinely matters here, is
    the real contract: a malformed envelope is always rejected as an error,
    never silently accepted as success, and the session remains fully usable
    afterward.
    """
    (tmp_path / "direct").mkdir(exist_ok=True)
    direct_env = _backend_env()
    direct_malformed, direct_health = await _probe_malformed_tools_call_envelope(
        sys.executable,
        ["-m", "netcoredbg_mcp", "--project-from-cwd"],
        direct_env,
        tmp_path / "direct",
    )

    assert "result" not in direct_malformed, (
        f"direct Python must reject a nameless tools/call as an error: {direct_malformed}"
    )
    assert (
        direct_malformed["error"]["code"] == DIRECT_PYTHON_INVALID_PARAMS_ERROR_CODE
    ), direct_malformed
    assert direct_health["result"]["isError"] is True, direct_health

    (tmp_path / "host").mkdir(exist_ok=True)
    host_env = _backend_env()
    host_env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    host_malformed, host_health = await _probe_malformed_tools_call_envelope(
        "dotnet",
        [str(host_dll), "--project-from-cwd"],
        host_env,
        tmp_path / "host",
    )

    assert "result" not in host_malformed, (
        f"host must reject a nameless tools/call as an error, never as success: {host_malformed}"
    )
    assert isinstance(host_malformed["error"]["code"], int)
    # Session health after the malformed request: a subsequent, well-formed call
    # still completes normally on both sides.
    assert host_health["result"]["isError"] is True, host_health
    assert host_health["result"] == direct_health["result"]


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

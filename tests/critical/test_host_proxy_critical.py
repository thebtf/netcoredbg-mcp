"""Critical release-gate coverage for the real .NET MCP compatibility host (Q0).

Promotes the smallest real Release-host ``initialize`` -> ``tools/list`` ->
``tools/call`` exchange into the critical suite, then extends this same file
with representative accepted front-door surfaces: exact 135-tool catalog plus
real call/error, eight native prompts, four resources/zero templates/read,
subscribe/update/unsubscribe, downstream roots reaching ``find_code_symbol``,
progress (and capability-gated structured logging), x-mux capability/metadata
ownership, cancellation, protocol-only stdout, and clean child shutdown.

This drives the actual ``dotnet``-hosted compatibility proxy and a real Python
child over stdio through the official ``mcp`` client SDK -- the same technique
already proven by ``tests/test_host_proxy.py`` -- not direct Python tool
registration or source-text assertions. Later front-door proxied surfaces
extend this same file rather than inventing a second release-only smoke path.
Non-mandatory host-roundtrip tests reuse the ``host_dll`` fixture from
``tests/test_host_proxy.py`` via ``tests/critical/conftest.py``. The mandatory
Section 10 front-door gate validates prerequisites first, then builds the host
via ``_ensure_release_host_dll`` so a missing ``dotnet`` fails closed instead of
being skipped at collection time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import mcp.types as types
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import TextContent
from pydantic import AnyUrl

from tests.test_host_proxy import (
    EXPECTED_TOOL_COUNT,
    FIND_SYMBOL_EXPECTED_RESULT,
    FIND_SYMBOL_NAME,
    HOST_DLL,
    HOST_PROJECT,
    MINIMAL_PLAN,
    SEARCH_FIXTURE_ROOT,
    UNKNOWN_TOOL_NAME,
    _backend_env,
    _cached_direct_catalog,
    _tool_payload,
)

# No module-wide pytestmark skipif for missing dotnet. Section 10
# (test_host_proxy_critical_front_door_surfaces) is a mandatory release gate and
# must execute + fail closed via require_section10_progress_prerequisites when
# dotnet is absent. Soft collection-time skips are scoped only to non-mandatory
# host-roundtrip tests that cannot produce that fail-closed proof.

_REQUIRES_DOTNET = pytest.mark.skipif(
    shutil.which("dotnet") is None,
    reason="dotnet CLI is required to build/run the .NET MCP compatibility host",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_release_host_dll() -> Path:
    """Build the Release host after Section 10 prerequisites have already passed.

    Kept as a plain function (not a fixture) so the mandatory front-door gate can
    validate tools first and only then build - fixture setup would otherwise race
    ahead of the fail-closed helper when ``dotnet`` is missing.
    """
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


def _path_without_dotnet() -> str:
    """Return a PATH that still runs this Python but cannot resolve ``dotnet``."""
    kept: list[str] = []
    python_dir = str(Path(sys.executable).resolve().parent)
    if python_dir not in kept:
        kept.append(python_dir)
    # Preserve essential Windows system dirs so the nested interpreter can start.
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        for entry in (str(Path(system_root) / "System32"), system_root):
            if entry not in kept:
                kept.append(entry)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or entry in kept:
            continue
        dotnet_name = "dotnet.exe" if os.name == "nt" else "dotnet"
        if (Path(entry) / dotnet_name).is_file():
            continue
        kept.append(entry)
    return os.pathsep.join(kept)
# Stable, read-only tool used by the original Q0 critical smoke and reused here.
PARITY_TOOL_NAME = "runtime_smoke_validate_plan"
EXPECTED_PROMPT_NAMES = [
    "debug",
    "debug-gui",
    "debug-exception",
    "debug-visual",
    "debug-mistakes",
    "investigate",
    "debug-scenario",
    "dap-escape-hatch",
]
EXPECTED_RESOURCES: dict[str, tuple[str, str]] = {
    "debug://state": ("debug_state_resource", "application/json"),
    "debug://breakpoints": ("debug_breakpoints_resource", "application/json"),
    "debug://output": ("debug_output_resource", "text/plain"),
    "debug://threads": ("debug_threads_resource", "application/json"),
}
METHOD_NOT_FOUND_ERROR_CODE = -32601

SMOKE_PROJECT = REPO_ROOT / "tests" / "fixtures" / "SmokeTestApp"
SMOKE_DLL = SMOKE_PROJECT / "bin" / "Debug" / "net8.0-windows" / "SmokeTestApp.dll"


def require_section10_progress_prerequisites(
    *,
    netcoredbg_path: str | None = None,
    smoke_dll: Path = SMOKE_DLL,
    which=shutil.which,
) -> tuple[str, Path]:
    """Fail closed for mandatory progress coverage when prerequisites are missing.

    Section 10 progress is a release-critical proof, not optional soft coverage.
    Environment and tool prerequisites are checked here before any fixture build:
    missing NETCOREDBG_PATH, a missing netcoredbg binary, or a missing dotnet CLI
    must report non-proof rather than silently omit the gate. The SmokeTestApp DLL
    is not required to exist yet; callers that build the fixture enforce it via a
    hard post-build assertion before invoking progress tools.
    """
    path = netcoredbg_path if netcoredbg_path is not None else os.environ.get("NETCOREDBG_PATH")
    if not path:
        raise AssertionError(
            "NETCOREDBG_PATH is required for mandatory Section 10 progress coverage; "
            "set it to a real netcoredbg executable or treat this gate as non-proof."
        )
    resolved = Path(path)
    if not resolved.is_file():
        raise AssertionError(
            "NETCOREDBG_PATH must point to an existing netcoredbg executable for "
            f"mandatory Section 10 progress coverage; got {path!r}."
        )
    if which("dotnet") is None:
        raise AssertionError(
            "dotnet CLI is required for mandatory Section 10 progress coverage "
            "(SmokeTestApp fixture build)."
        )
    # The DLL may be produced by a just-in-time build; only require the parent
    # project tree when the built binary is already expected to exist.
    if smoke_dll.exists() and not smoke_dll.is_file():
        raise AssertionError(
            f"SmokeTestApp path is not a file for Section 10 progress coverage: {smoke_dll}"
        )
    return str(resolved), smoke_dll


def _combined_message_handler(
    notification_methods: list[str],
    resource_updates: asyncio.Queue[str],
    logging_messages: list[str],
):
    async def handle(message: object) -> None:
        root = getattr(message, "root", None)
        method = getattr(root, "method", None)
        if method is not None:
            notification_methods.append(method)
        if isinstance(message, types.ServerNotification):
            if isinstance(message.root, types.ResourceUpdatedNotification):
                resource_updates.put_nowait(str(message.root.params.uri))
            if isinstance(message.root, types.LoggingMessageNotification):
                data = message.root.params.data
                logging_messages.append(
                    data if isinstance(data, str) else json.dumps(data)
                )

    return handle


def _client_session_supports_list_roots() -> bool:
    return "list_roots_callback" in inspect.signature(ClientSession.__init__).parameters


@_REQUIRES_DOTNET
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
                init_result = await session.initialize()
                assert init_result.protocolVersion, "host must negotiate a protocol version"
                assert init_result.capabilities.tools is not None, (
                    "host must advertise a tools capability"
                )

                assert init_result.serverInfo.name == "netcoredbg-mcp-host", (
                    f"unexpected serverInfo.name: {init_result.serverInfo.name!r}"
                )
                experimental = init_result.capabilities.experimental or {}
                assert experimental.get("x-mux") == {"sharing": "isolated"}, (
                    f"host must advertise x-mux.sharing=isolated: {experimental}"
                )

                tools_result = await session.list_tools()
                proxied_names = {tool.name for tool in tools_result.tools}
                assert PARITY_TOOL_NAME in proxied_names, (
                    f"host tools/list is missing {PARITY_TOOL_NAME!r}: {sorted(proxied_names)}"
                )

                call_result = await session.call_tool(PARITY_TOOL_NAME, {"plan": MINIMAL_PLAN})
                data = _tool_payload(call_result)["data"]
                assert data["can_run"] is True, data
                assert data["status"] == "PASS", data

    host_errlog_path = tmp_path / "host-stderr.log"
    with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
        await asyncio.wait_for(_run_exchange(errlog), timeout=60)

    stderr_text = host_errlog_path.read_text(encoding="utf-8")
    assert "[DIAGNOSTIC] Startup CWD:" in stderr_text, (
        f"expected forwarded Python diagnostics on stderr, got:\n{stderr_text}"
    )


@_REQUIRES_DOTNET
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


@pytest.mark.critical
def test_section10_progress_prerequisites_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """@critical category: behavioral - missing progress prerequisites are non-proof."""
    monkeypatch.delenv("NETCOREDBG_PATH", raising=False)
    with pytest.raises(AssertionError, match="NETCOREDBG_PATH is required"):
        require_section10_progress_prerequisites(netcoredbg_path=None)

    with pytest.raises(AssertionError, match="existing netcoredbg executable"):
        require_section10_progress_prerequisites(
            netcoredbg_path=str(REPO_ROOT / "does-not-exist-netcoredbg.exe")
        )

    with pytest.raises(AssertionError, match="dotnet CLI is required"):
        require_section10_progress_prerequisites(
            netcoredbg_path=str(Path(__file__).resolve()),
            which=lambda _name: None,
        )


@pytest.mark.critical
def test_section10_gate_has_no_module_wide_dotnet_skip() -> None:
    """@critical category: behavioral - module-wide skipif must not hide Section 10.

    A collection-time ``pytestmark = pytest.mark.skipif(...dotnet...)`` would skip
    ``test_host_proxy_critical_front_door_surfaces`` (and the fail-closed helper
    regression) before any body runs, turning missing-dotnet into exit 0. Guard the
    source shape so that regression cannot re-enter silently.
    """
    import ast

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "pytestmark":
                raise AssertionError(
                    "tests/critical/test_host_proxy_critical.py must not assign module-wide "
                    "pytestmark (especially a skipif on missing dotnet): that hides the "
                    "mandatory Section 10 front-door gate behind a collection-time skip. "
                    "Scope soft skips to individual non-mandatory host-roundtrip tests only."
                )


@pytest.mark.critical
def test_section10_front_door_fails_closed_when_dotnet_absent_from_path() -> None:
    """@critical category: behavioral - bounded PATH probe: missing dotnet is FAILURE.

    Spawns a nested pytest on the mandatory front-door gate with PATH stripped of
    directories that contain ``dotnet``. The nested run must exit non-zero with a
    failure (not a skip) and surface the fail-closed prerequisite message. This
    catches both a reintroduced module-wide skipif and a gate that no longer
    calls ``require_section10_progress_prerequisites``.
    """
    env = os.environ.copy()
    # Satisfy the NETCOREDBG_PATH file check so the nested gate fails on missing
    # *dotnet*, not on an unset debugger path.
    env["NETCOREDBG_PATH"] = str(Path(__file__).resolve())
    env["PATH"] = _path_without_dotnet()
    # Drop vars that could short-circuit discovery of a system-wide dotnet.
    env.pop("DOTNET_ROOT", None)
    env.pop("DOTNET_HOST_PATH", None)
    env.pop("DOTNET_MULTILEVEL_LOOKUP", None)

    # Sanity: the constructed PATH must not resolve dotnet for this process either.
    assert shutil.which("dotnet", path=env["PATH"]) is None, (
        "PATH scrubber failed to hide dotnet; probe would be invalid"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).as_posix()}::test_host_proxy_critical_front_door_surfaces",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        "mandatory Section 10 front-door gate must exit non-zero when dotnet is absent "
        f"from PATH; got returncode={result.returncode}\n{combined}"
    )
    assert "dotnet CLI is required" in combined, (
        "expected fail-closed prerequisite assertion about missing dotnet CLI, got:\n"
        f"{combined}"
    )
    # Soft-skips report as "1 skipped" and often exit 0; a reintroduced module-wide
    # skipif must not be mistaken for a failing gate.
    assert "1 skipped" not in combined, (
        "missing-dotnet must not soft-skip the mandatory front-door gate:\n"
        f"{combined}"
    )
    assert "1 failed" in combined or "FAILED" in combined, (
        "expected a hard pytest failure for missing dotnet, got:\n"
        f"{combined}"
    )


@pytest.mark.critical
@pytest.mark.asyncio
async def test_host_proxy_critical_front_door_surfaces(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """@critical category: behavioral - real Release host covers accepted front-door
    surfaces without a parallel smoke path or full per-module matrix.

    Intentionally does not depend on the ``host_dll`` fixture: that fixture builds via
    ``dotnet`` at setup time, which would error before the fail-closed prerequisite
    helper could report missing tools as non-proof. Prerequisites are validated first;
    only then is the host built.
    """
    # Fail closed up front: progress Section 10 is mandatory, not optional soft coverage.
    # Must run before any host build so a missing dotnet/NETCOREDBG_PATH is AssertionError
    # non-proof rather than a fixture/collection skip.
    netcoredbg, smoke_dll = require_section10_progress_prerequisites()
    host_dll = _ensure_release_host_dll()

    marker_source = tmp_path / "Program.cs"
    marker_source.write_text(
        "class Program { static void Main() { } }\n",
        encoding="utf-8",
    )
    (tmp_path / "Probe.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>',
        encoding="utf-8",
    )

    env = _backend_env()
    env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    env["NETCOREDBG_PATH"] = netcoredbg

    notification_methods: list[str] = []
    resource_updates: asyncio.Queue[str] = asyncio.Queue()
    logging_messages: list[str] = []
    progress_events: list[tuple[float, float | None, str | None]] = []

    async def progress_callback(
        progress: float, total: float | None, message: str | None
    ) -> None:
        progress_events.append((progress, total, message))

    # Canonical expected names from the same direct-Python catalog source used by
    # tests/test_host_proxy.py — not a second hand-maintained list.
    direct_catalog = await _cached_direct_catalog(tmp_path_factory)
    expected_tool_names = {tool["name"] for tool in direct_catalog}
    assert len(expected_tool_names) == EXPECTED_TOOL_COUNT, (
        f"canonical direct-Python catalog must have exactly {EXPECTED_TOOL_COUNT} "
        f"names; got {len(expected_tool_names)}"
    )

    # ---- Session 1: tools/prompts/resources/mux/progress/cancel/stdout ----
    # Reuses the accepted --project-from-cwd resource-subscription fixture shape
    # from tests/critical/test_resources_relay_critical.py.
    params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project-from-cwd"],
        env=env,
        cwd=str(tmp_path),
    )
    host_errlog_path = tmp_path / "host-stderr-front-door.log"
    with open(host_errlog_path, "w+", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                message_handler=_combined_message_handler(
                    notification_methods, resource_updates, logging_messages
                ),
            ) as session:
                init_result = await session.initialize()
                assert init_result.serverInfo.name == "netcoredbg-mcp-host"
                experimental = init_result.capabilities.experimental or {}
                assert experimental.get("x-mux") == {"sharing": "isolated"}
                assert init_result.capabilities.tools is not None
                assert init_result.capabilities.prompts is not None
                assert init_result.capabilities.resources is not None
                assert init_result.capabilities.resources.subscribe is True
                assert init_result.capabilities.resources.listChanged is False
                assert init_result.capabilities.logging is None

                with pytest.raises(McpError) as logging_error:
                    await session.set_logging_level("info")
                assert logging_error.value.error.code == METHOD_NOT_FOUND_ERROR_CODE

                tools = await session.list_tools()
                host_tool_names = {tool.name for tool in tools.tools}
                assert host_tool_names == expected_tool_names, (
                    "host tools/list must match the exact canonical direct-Python catalog "
                    f"names - missing: {sorted(expected_tool_names - host_tool_names)}, "
                    f"unexpected: {sorted(host_tool_names - expected_tool_names)}"
                )

                call_ok = await session.call_tool(
                    PARITY_TOOL_NAME,
                    {"plan": MINIMAL_PLAN},
                    progress_callback=progress_callback,
                )
                assert _tool_payload(call_ok)["data"]["status"] == "PASS"

                call_err = await session.call_tool(UNKNOWN_TOOL_NAME, {})
                assert call_err.isError is True
                assert any(
                    isinstance(block, TextContent)
                    and f"Unknown tool: {UNKNOWN_TOOL_NAME}" in block.text
                    for block in call_err.content
                )

                prompts = await session.list_prompts()
                assert [prompt.name for prompt in prompts.prompts] == EXPECTED_PROMPT_NAMES
                rendered = await session.get_prompt("debug")
                assert rendered.messages

                resources = await session.list_resources()
                actual_resources = {
                    str(resource.uri): (resource.name, resource.mimeType)
                    for resource in resources.resources
                }
                assert actual_resources == EXPECTED_RESOURCES
                templates = await session.list_resource_templates()
                assert templates.resourceTemplates == []
                state = await session.read_resource(AnyUrl("debug://state"))
                assert '"execState"' in state.contents[0].text

                await session.subscribe_resource(AnyUrl("debug://breakpoints"))
                await session.subscribe_resource(AnyUrl("debug://breakpoints"))
                added = await session.call_tool(
                    "add_breakpoint",
                    {"file": str(marker_source), "line": 1},
                    meta={"muxSessionId": "critical-agent-A"},
                )
                assert not added.isError, added
                updated_uri = await asyncio.wait_for(resource_updates.get(), timeout=5)
                assert updated_uri == "debug://breakpoints"

                denied = await session.call_tool(
                    "add_breakpoint",
                    {"file": str(marker_source), "line": 2},
                    meta={"muxSessionId": "critical-agent-B"},
                )
                denied_text = ""
                if denied.content and isinstance(denied.content[0], TextContent):
                    denied_text = denied.content[0].text
                assert denied.isError is True or "owned by another agent" in denied_text

                await session.unsubscribe_resource(AnyUrl("debug://breakpoints"))
                while not resource_updates.empty():
                    resource_updates.get_nowait()
                removed = await session.call_tool(
                    "remove_breakpoint",
                    {"file": str(marker_source), "line": 1},
                    meta={"muxSessionId": "critical-agent-A"},
                )
                assert not removed.isError, removed
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(resource_updates.get(), timeout=0.3)

                cancel_task = asyncio.create_task(
                    session.call_tool(PARITY_TOOL_NAME, {"plan": MINIMAL_PLAN})
                )
                await asyncio.sleep(0)
                cancel_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancel_task
                after_cancel = await session.call_tool(
                    PARITY_TOOL_NAME, {"plan": MINIMAL_PLAN}
                )
                assert _tool_payload(after_cancel)["data"]["status"] == "PASS"

    stderr_text = host_errlog_path.read_text(encoding="utf-8")
    assert "[DIAGNOSTIC] Startup CWD:" in stderr_text, (
        f"expected forwarded Python diagnostics on stderr, got:\n{stderr_text}"
    )
    assert "notifications/tools/list_changed" not in notification_methods

    # ---- Session 2: progress + structured-logging capability truth ----
    # Separate session so project scope matches the accepted SmokeTestApp fixture
    # (tmp_path project correctly rejects paths outside its tree).
    smoke_project = SMOKE_PROJECT
    build = subprocess.run(
        ["dotnet", "build", str(smoke_project), "-c", "Debug"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, (
        "SmokeTestApp Debug build is required for mandatory Section 10 progress coverage:\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    assert smoke_dll.is_file(), (
        "SmokeTestApp Debug DLL is required for mandatory Section 10 progress coverage: "
        f"{smoke_dll}"
    )

    progress_events.clear()
    logging_messages.clear()
    progress_params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll), "--project", str(smoke_project)],
        env=env,
        cwd=str(tmp_path),
    )
    progress_errlog = tmp_path / "host-stderr-progress.log"
    with open(progress_errlog, "w+", encoding="utf-8") as errlog:
        async with stdio_client(
            progress_params, errlog=errlog
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                message_handler=_combined_message_handler(
                    notification_methods, resource_updates, logging_messages
                ),
            ) as session:
                init_result = await session.initialize()
                assert init_result.capabilities.logging is None
                started = await session.call_tool(
                    "start_debug",
                    {
                        "program": str(smoke_dll.resolve()),
                        "pre_build": False,
                        "stop_at_entry": True,
                    },
                    progress_callback=progress_callback,
                )
                assert not started.isError, started
                started_payload = _tool_payload(started)
                assert "error" not in started_payload, started_payload
                assert progress_events, (
                    "start_debug through the host must forward "
                    "notifications/progress before the final response; "
                    f"payload={started_payload}"
                )
                # Capability-absent: ProgressLoggingRelay suppresses
                # structured log notifications from non-advertising Python.
                assert logging_messages == []
                await session.call_tool("stop_debug", {})

    # ---- Session 3: downstream roots reach find_code_symbol ----
    # Same SearchTestApp fixture RootsRelayRealPythonTests accepts. No --project /
    # --project-from-cwd / NETCOREDBG_PROJECT_ROOT so roots must win.
    assert _client_session_supports_list_roots()
    roots_env = _backend_env()
    roots_env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = sys.executable
    roots_env.pop("NETCOREDBG_PROJECT_ROOT", None)
    roots_env.pop("MCP_PROJECT_ROOT", None)

    async def list_roots_callback(_context: object) -> types.ListRootsResult:
        return types.ListRootsResult(
            roots=[
                types.Root(
                    uri=AnyUrl(SEARCH_FIXTURE_ROOT.resolve().as_uri()),
                    name="search-fixture",
                )
            ]
        )

    roots_params = StdioServerParameters(
        command="dotnet",
        args=[str(host_dll)],
        env=roots_env,
        cwd=str(tmp_path),
    )
    roots_errlog = tmp_path / "host-stderr-roots.log"
    with open(roots_errlog, "w+", encoding="utf-8") as errlog:
        async with stdio_client(roots_params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                list_roots_callback=list_roots_callback,
            ) as session:
                await session.initialize()
                symbol = await session.call_tool(
                    "find_code_symbol",
                    {"name": FIND_SYMBOL_NAME},
                )
                payload = _tool_payload(symbol)
                assert payload["data"]["results"] == [FIND_SYMBOL_EXPECTED_RESULT]
                project_root = Path(payload["data"]["project_root"]).resolve()
                assert project_root == SEARCH_FIXTURE_ROOT.resolve()

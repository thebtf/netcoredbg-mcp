# Production Testing Playbook

## Scope

This playbook covers the public surfaces of `netcoredbg-mcp` that a user relies
on after installing the package:

- The `netcoredbg-mcp` CLI entry point.
- MCP server surface registration for tools, prompts, and resources.
- Launch environment profile handling and secret-safe metadata.
- Runtime smoke orchestration and GUI fixture availability.
- WPF DataGrid drag/drop and edge-scroll runtime-smoke v2 proof.
- A candidate, not-yet-published .NET compatibility-host consumer journey (flow
  10) that proxies to this same installed Python backend; PKG-001 owns final
  installed-package cutover evidence.

The playbook is intentionally executable on a release workstation without a
private downstream application. GUI debugging against WPF, WinForms, and
Avalonia still depends on a Windows host, `netcoredbg`, and a target .NET app;
those flows are covered by the manual smoke suite and can be run when a full
Windows debug stand is available. Avalonia is a first-class GUI fixture target
because it is a future migration path for the UI automation surface.

## UXDD Release Criterion

This playbook is the primary release gate, not a supplementary demonstration. The release report must enumerate the user journeys it claims as shipped and exercise each one through the same built-wheel installation and public CLI/MCP entry point a consumer receives.

- `PRODUCT_WORKS` is required for every claimed journey.
- `PARTIALLY_WORKS` and `BROKEN` both mean `BLOCK_RELEASE` for a claimed journey.
- Private test helpers, direct internal calls, and unit-test-only proof are not consumer evidence.
- Unit, integration, critical, runtime-smoke, build, and packaging protocols remain mandatory supporting evidence, but their success cannot override a failed or partial consumer journey.
- An optional flow may be omitted only when the governing spec, PRD, ADR, or active run contract does not claim it as shipped and the release does not change its consumer boundary.

## Prerequisites

- Python 3.10 or newer.
- Development dependencies installed through the locked `uv` environment for supporting repository checks.
- Release commands run from the repository root.
- `NETCOREDBG_PATH` points at the installed `netcoredbg.exe` for GUI smoke commands.
- For full GUI smoke coverage, build all three fixture apps: `tests/fixtures/SmokeTestApp`, `tests/fixtures/WpfSmokeApp`, and `tests/fixtures/AvaloniaSmokeApp`.
- For flow 10 (the .NET compatibility-host candidate journey): the .NET 8 SDK,
  and a runtime identifier compatible with `dotnet publish -r <RID>
  --self-contained true` on the target OS (this playbook uses `win-x64`).

## Release-Candidate Consumer Environment

Before any flow can count as UXDD evidence, build the wheel and install it into a dedicated disposable environment. All primary consumer commands below use `$ConsumerCli` or `$ConsumerPython`; source-tree `uv run` commands are supporting checks only.

```powershell
uv build
$wheel = Get-ChildItem -LiteralPath dist -Filter 'netcoredbg_mcp-*.whl' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if (-not $wheel) { throw 'Release-candidate wheel was not produced.' }
$ConsumerRoot = Join-Path $env:TEMP ('netcoredbg-mcp-release-' + [guid]::NewGuid().ToString('N'))
uv venv --python (Get-Command python).Source $ConsumerRoot
$ConsumerPython = Join-Path $ConsumerRoot 'Scripts\python.exe'
$ConsumerCli = Join-Path $ConsumerRoot 'Scripts\netcoredbg-mcp.exe'
$ConsumerProject = Join-Path $ConsumerRoot 'project'
New-Item -ItemType Directory -Path $ConsumerProject | Out-Null
uv pip install --python $ConsumerPython $wheel.FullName
```

Record the wheel path and disposable environment path in the release report. Remove the environment only after its consumer evidence has been captured.

## Consumer and Supporting Flows

### 1. Installed CLI Consumer Smoke

Command:

```powershell
& $ConsumerCli --version
```

Expected result:

- Exit code `0`.
- Output is `netcoredbg-mcp <TARGET_VERSION>`, matching the version named by the release report and the installed wheel filename.

### 2. Installed MCP Client Exchange

This is a real consumer round trip through the installed console entry point and the official MCP client SDK bundled by the wheel.

```powershell
$env:NETCOREDBG_MCP_CONSUMER_CLI = $ConsumerCli
$env:NETCOREDBG_MCP_CONSUMER_PROJECT = $ConsumerProject
$script = @'
import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REQUIRED_TOOLS = {"start_debug", "add_breakpoint", "get_call_stack", "run_runtime_smoke"}

async def main():
    params = StdioServerParameters(
        command=os.environ["NETCOREDBG_MCP_CONSUMER_CLI"],
        args=["--project-from-cwd"],
        cwd=os.environ["NETCOREDBG_MCP_CONSUMER_PROJECT"],
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = sorted(REQUIRED_TOOLS - names)
            result = {
                "x_mux": (initialized.capabilities.experimental or {}).get("x-mux"),
                "tools_capability": initialized.capabilities.tools is not None,
                "tool_count": len(names),
                "missing_tools": missing,
            }
            print(json.dumps(result, sort_keys=True))
            if result["x_mux"] != {"sharing": "isolated"} or not result["tools_capability"] or missing:
                raise SystemExit(1)

asyncio.run(main())
'@
& $ConsumerPython -c $script
```

Expected result:

- Exit code `0`.
- The initialized server advertises `x-mux.sharing=isolated` and a tools capability.
- `missing_tools` is empty and the installed server exposes the consumer-critical debug and runtime-smoke tools.

Supporting contract check; this source-tree test is mandatory but does not produce the UXDD verdict:

```powershell
uv run --locked --extra dev pytest tests/critical/test_release_critical.py -m critical
```

### 3. Supporting Protocol Check — Launch Environment Metadata Safety

Command:

```powershell
uv run --locked --extra dev pytest tests/critical/test_release_critical.py::test_launch_profile_metadata_never_exposes_environment_values
```

Expected result:

- The test passes.
- Resolved launch environments may contain values internally.
- User-facing metadata contains variable names and counts only; it does not
  contain inherited, profile, or direct environment values.

### 4. Supporting Fixture Inventory

Commands:

```powershell
dotnet build tests/fixtures/SmokeTestApp -c Debug
dotnet build tests/fixtures/WpfSmokeApp -c Debug
dotnet build tests/fixtures/AvaloniaSmokeApp -c Debug
uv run --locked --extra dev python tests/smoke_test_manual.py --list
```

Expected result:

- The fixture builds exit `0`.
- The scenario list includes `Runtime Smoke Bounded Runner`, `WPF Shift/DataGrid
  Evidence`, `WPF One-Call Runtime Smoke Workflow`, and `Avalonia UI Fixture
  Compatibility`.
- Missing GUI fixture binaries are treated as reduced coverage, not as proof
  that the corresponding GUI surface was exercised.
- Before release, run the WPF and Avalonia GUI scenarios on a Windows GUI
  workstation and record whether each scenario returned `PASS`, `BLOCKED`, or
  `FAIL`.

### 5. Supporting WPF One-Call Fixture Workflow

Command:

```powershell
if (-not $env:NETCOREDBG_PATH) { throw "Set NETCOREDBG_PATH to netcoredbg.exe before running GUI smoke." }
if (-not (Test-Path -LiteralPath $env:NETCOREDBG_PATH)) { throw "NETCOREDBG_PATH does not exist: $env:NETCOREDBG_PATH" }
uv run --locked --extra dev python -c "import asyncio, sys; import tests.smoke_test_manual as s; asyncio.run(s.test_wpf_one_call_runtime_smoke_workflow()); print(f'RESULTS: {s.passed} passed, {s.failed} failed out of {s.passed + s.failed} checks'); sys.exit(0 if s.failed == 0 else 1)"
```

Expected result:

- Exit code `0`.
- Output includes `WPF One-Call Runtime Smoke Workflow`.
- The one `run_runtime_smoke` plan returns `PASS` with cleanup evidence, or an
  honest `BLOCKED` / `FAIL` result that names the failing operation and cleanup
  state.

### 6. Supporting Avalonia Fixture Compatibility

Command:

```powershell
if (-not $env:NETCOREDBG_PATH) { throw "Set NETCOREDBG_PATH to netcoredbg.exe before running GUI smoke." }
if (-not (Test-Path -LiteralPath $env:NETCOREDBG_PATH)) { throw "NETCOREDBG_PATH does not exist: $env:NETCOREDBG_PATH" }
uv run --locked --extra dev python -c "import asyncio, sys; import tests.smoke_test_manual as s; asyncio.run(s.test_avalonia_ui_fixture_compatibility()); print(f'RESULTS: {s.passed} passed, {s.failed} failed out of {s.passed + s.failed} checks'); sys.exit(0 if s.failed == 0 else 1)"
```

Expected result:

- Exit code `0`.
- Output includes `Avalonia UI Fixture Compatibility`.
- The fixture is found through UIA, modifier cleanup succeeds, and incomplete
  Avalonia UIA grid or key-routing paths are reported as bounded `UNSUPPORTED`
  or `BLOCKED` evidence rather than being skipped.

### 7. WPF DataGrid Drag/Drop Customer-Mode Gate

Contract source:

- `docs/examples/runtime-smoke-v2-drag-drop-grid.json`

Customer-mode setup:

1. Open only this playbook, the example JSON, and public README guidance.
2. Configure a WPF DataGrid stand with stable row identity values matching the
   example selectors or adapt the generic `DataGridUnderTest` and `StableRowId`
   names to the product under test.
3. Start the release-candidate MCP server from `$ConsumerCli`, then run the v2 plan through its public `run_runtime_smoke` tool on a Windows GUI workstation with a backend that can produce real pointer route evidence and `ui.grid.viewport` identity evidence.
4. Include the offscreen row-target drag/drop case from the example: the target
   row is offscreen, the drop endpoint is expressed as a row-based target with
   `drop.ensure_visible=true`, and the run must either prove the gesture or
   fail closed before side effects if target-side realization hides the drag
   source. This is a bounded CR-075 customer-mode proof contract for broad
   `#270`; it does not close the broad helper family by itself.
5. Run the supporting fixture-backed WPF v2 drag/drop smoke; this does not substitute for the installed-server customer-mode run above:

```powershell
if (-not $env:NETCOREDBG_PATH) { throw "Set NETCOREDBG_PATH to netcoredbg.exe before running GUI smoke." }
if (-not (Test-Path -LiteralPath $env:NETCOREDBG_PATH)) { throw "NETCOREDBG_PATH does not exist: $env:NETCOREDBG_PATH" }
$script = @'
import asyncio
import json
import sys

import tests.smoke_test_manual as s

async def main():
    positive = {
        "visible_row": s.run_wpf_v2_visible_row_drag_runtime_smoke,
        "offscreen_row_target": s.run_wpf_v2_offscreen_row_target_drag_runtime_smoke,
        "edge_scroll": s.run_wpf_v2_edge_scroll_drag_runtime_smoke,
        "multi_row": s.run_wpf_v2_multi_row_drag_runtime_smoke,
    }
    summary = {}
    failed = []
    for name, runner in positive.items():
        evidence = await runner()
        summary[name] = {
            "status": evidence.get("status"),
            "reason": evidence.get("reason"),
        }
        if evidence.get("status") != "PASS":
            failed.append(name)
    negative = await s.run_wpf_v2_negative_drag_runtime_smoke()
    negative_status = negative.get("status")
    summary["negative_no_op"] = {
        "status": negative_status,
        "reason": negative.get("reason"),
        "next_step": (negative.get("blocked") or {}).get("next_step"),
    }
    if negative_status not in {"PASS", "BLOCKED"}:
        failed.append("negative_no_op")
    if negative_status == "BLOCKED" and not (negative.get("blocked") or {}).get("next_step"):
        failed.append("negative_no_op_missing_next_step")
    print(json.dumps({"summary": summary, "failed": failed}, indent=2, sort_keys=True))
    sys.exit(1 if failed else 0)

asyncio.run(main())
'@
uv run --locked --extra dev python -c $script
```

The offscreen row-target function is the minimum proof for the
`drop.ensure_visible=true` part of this gate; the full command above is the
release gate because it also exercises visible-row route evidence, edge-scroll,
multi-row payload identity, and bounded negative no-op handling.

6. Run the supporting release-critical guards:

```powershell
uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py
uv run --locked --extra dev pytest tests/critical/test_runtime_smoke_v2_critical.py -m critical
```

The primary UXDD verdict for this flow comes from steps 1-4 against the installed release-candidate server and the configured product stand. Steps 5-6 are mandatory supporting checks only.

Expected result:

- `PRODUCT_WORKS`: the configured WPF stand returns `PASS`, the result includes
  backend-produced `ui.drag` `route_evidence`, before/after `ui.grid.viewport`
  snapshots, selected payload identity continuity, row count preservation, and
  negative no-op cleanup evidence. The offscreen row-target case also includes
  bounded drop ensure-visible evidence for the row-based `drop.ensure_visible=true`
  endpoint.
- `PARTIALLY_WORKS`: the JSON example parses and the critical guard passes, but
  the live stand is absent or returns an honest `BLOCKED` result that names the
  missing pointer, route, viewport, selected-payload, drop ensure-visible,
  no-op, or cleanup capability with a concrete `next_step`. A negative no-op
  backend limitation is acceptable only when the positive drag/drop, offscreen
  row-target, edge-scroll, multi-row payload, and cleanup checks still pass and
  the missing no-op evidence is explicitly recorded.
- `BROKEN`: the plan returns `FAIL` or `INVALID_SETUP`; a drag result reports
  `PASS` without backend-produced route evidence; viewport or row identity
  evidence is missing without `BLOCKED`; the offscreen row-target case is
  represented as raw viewport guessing instead of a row-based
  `drop.ensure_visible=true` endpoint; or docs/tests use another fixture as a
  substitute for WPF DataGrid acceptance.

WinForms `dragList` primitive smoke is not a substitute for WPF DataGrid CR-001
acceptance.

### 8. Supporting Runtime-Smoke Diagnostic Schema Contract

Contract sources:

- `docs/examples/runtime-smoke-oracle-pack.json`
- `docs/examples/runtime-smoke-app-diagnostics.json`
- `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`
- `docs/examples/runtime-smoke-semantic-probe.json`
- `docs/examples/runtime-smoke-tracepoint-guardrail.json`

Expected result:

- Oracle packs, app diagnostics, semantic probes, and tracepoint guardrails use
  schema `netcoredbg.runtime_smoke.diagnostics.v1`.
- Status vocabulary is limited to `PASS`, `BLOCKED`, and `FAIL`.
- Evidence stays bounded by `max_text_length`, `max_list_items`, and
  `max_json_bytes`.
- `raw_tree`, `window_tree`, `ui_tree`, `screenshot_base64`, `access_token`,
  `api_key`, `password`, and `secret` are omitted before results leave the
  runtime-smoke boundary; `backend_result`, `exception`, `raw_output`, and
  `stack` are summarized.
- App diagnostics that declare freshness expectations such as
  `expected_process_name`, `expected_modules`, workspace artifacts, or
  `loaded_sources` preserve module `symbolStatus` evidence so live-target PDB/process proof
  can fail a stale `PASS` diagnostic artifact.
- Tracepoint guardrails name `allowed_when`, `blocked_when`, `unsafe_when`, and
  cleanup ownership with `debug.tracepoint.remove` before
  instrumentation-dependent runtime behavior is added.

Verification:

```powershell
uv run --locked --extra dev pytest tests/test_runtime_smoke_diagnostics_schema.py
```

### 9. NovaScript Action-Oracle App-Diagnostics Consumer Gate

Contract source:

- `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`
- Replay packet:
  `docs/reproduction-scenarios/novascript-action-oracle-app-diagnostics-replay-2026-06-21.md`

Provider version preflight:

```powershell
& $ConsumerCli --version
```

Expected preflight result:

- Exit code `0`.
- Output is `netcoredbg-mcp <TARGET_VERSION>`, matching the release report and wheel filename.
- The MCP session used below is started from this installed release-candidate entry point, not a source-tree editable environment.

Consumer procedure:

1. Copy the contract source into the NovaScript checkout.
2. Replace `<NOVASCRIPT_PROGRAM_DLL_OR_EXE>`,
   `<NOVASCRIPT_DEBUG_OUTPUT_DIR>`, `<NOVASCRIPT_PROJECT_FILE>`,
   `<NOVASCRIPT_REPO>`, `<ACTION_ORACLE_TRIGGER_AUTOMATION_ID>`,
   `<NOVASCRIPT_PROCESS_NAME>`, and `<NOVASCRIPT_PRIMARY_MODULE>` with the
   local NovaScript Debug build paths, the UI action that writes the
   action-oracle diagnostic payload, and the freshness identity for the actual
   launched process. EXE launches normally use the NovaScript process name;
   DLL launches through a host process should use that host process name while
   keeping the NovaScript assembly as the expected module.
3. Run the plan through the release-candidate MCP server started from `$ConsumerCli`, using the `run_runtime_smoke` tool from the NovaScript repository root.
4. Record the returned runtime-smoke envelope as the consumer evidence. A
   lifecycle run may use `runtime_smoke_start`, `runtime_smoke_tail_events`,
   `runtime_smoke_get_result`, and `runtime_smoke_stop` when observation or
   cancellation is needed.

Expected result:

- `PRODUCT_WORKS`: the final runtime-smoke status is `PASS`; the generated case
  id is `action_oracle_diagnostics`; the probe kind is `app_diagnostics`; the
  diagnostic payload uses schema `netcoredbg.runtime_smoke.diagnostics.v1`;
  freshness evidence confirms the NovaScript process/module/workspace/artifact
  expectations; cleanup reports debug stop success and an empty process
  registry.
- `PARTIALLY_WORKS`: the example parses and expands, but the live NovaScript
  stand is absent or returns a bounded `BLOCKED` result that names the missing
  launch, UI selector, diagnostic artifact, freshness, or cleanup capability
  with a concrete `next_step`.
- `BROKEN`: the plan returns `FAIL` or `INVALID_SETUP`; the generated
  action-oracle case falls back to `file.json`; app diagnostics pass without a
  fresh diagnostic artifact; unsafe diagnostic fields leave the runtime-smoke
  boundary; or cleanup leaves NovaScript/netcoredbg processes behind.

This consumer contract originated with the v0.20.5 NovaScript action-oracle slice, but each new release verdict applies to `<TARGET_VERSION>` from the installed release-candidate wheel. It does not replace the CR-003 DataGrid drag/drop replay gate above.

### 10. .NET Compatibility-Host Candidate Consumer Journey

This is a **candidate**, not-yet-published journey: `netcoredbg-mcp` still ships
only the Python wheel and console entry point documented in flows 1-9 above.
This section proves a second, additive way to reach the identical tool
surface — a self-contained .NET 8 compatibility host that proxies to the same
installed Python backend — so PKG-001 can reuse this evidence when it decides
whether to publish and cut over. This journey does not publish `netcoredbg-mcp`
as a .NET package, does not complete packaging, and does not cut the default
entry point over from Python; `netcoredbg-mcp --project-from-cwd` (flows 1-9)
remains the product's only published, installed entry point until PKG-001
ships and passes its own installed-consumer gate.

#### 10.1 Candidate Release Build/Publish

Build the self-contained single-file host from the same source checkout as the
release candidate:

```powershell
$ConsumerNetHostDir = Join-Path $ConsumerRoot 'net-host'
dotnet publish host/NetCoreDbg.Mcp.Host -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o $ConsumerNetHostDir
$ConsumerNetHost = Join-Path $ConsumerNetHostDir 'NetCoreDbg.Mcp.Host.exe'
```

Expected result:

- Exit code `0`; `dotnet publish` reports `NetCoreDbg.Mcp.Host -> $ConsumerNetHostDir`.
- `$ConsumerNetHost` is one self-contained executable — no separately published
  `.dll` or shared .NET runtime install is required on the target machine to
  launch it.
- The build comes from `host/NetCoreDbg.Mcp.Host`, the same source tree gated
  by Q0's real-host critical coverage, not a private test harness; it does not
  edit `NetCoreDbg.Mcp.Host.csproj` or publish any other project.

#### 10.2 Configuration — Python Backend Dependency Truth

The candidate host is a compatibility proxy, not a Python-free
reimplementation: every tool call still executes inside the same installed
`netcoredbg-mcp` Python package. Point it at the release-candidate backend
explicitly, so the journey proves the installed wheel from "Release-Candidate
Consumer Environment" above, not an ambient source checkout:

```powershell
$env:NETCOREDBG_MCP_PYTHON_EXECUTABLE = $ConsumerPython
```

Expected result:

- `$ConsumerNetHost` launches `$ConsumerPython -m netcoredbg_mcp <args>` as a
  direct child process (`UseShellExecute=false`, argument-list forwarding); it
  does not fall back to a bare `python` on `PATH` once this variable is set.
- The child inherits the host's own working directory, so `--project-from-cwd`
  resolves exactly like the Python journey in flow 2.
- Omitting `NETCOREDBG_MCP_PYTHON_EXECUTABLE` on a machine without a `python`
  on `PATH`, or pointing it at an interpreter without the installed wheel,
  stops the exchange from ever reaching `initialize` — this is `BROKEN`,
  never `PARTIALLY_WORKS` and never a silent `PRODUCT_WORKS`.

#### 10.3 Real External MCP Client Exchange — initialize / list / call

This is a real consumer round trip: a separate OS process (`$ConsumerNetHost`)
launched by a real external MCP client (the official Python SDK), never a
direct in-process call to `create_server()` or `RunProxyAsync`. The MCP
client SDK only forwards a safe default environment subset to a spawned
server process, so the script below builds on that default and adds only the
one variable the candidate host needs:

```powershell
$env:NETCOREDBG_MCP_CONSUMER_NET_HOST = $ConsumerNetHost
$env:NETCOREDBG_MCP_CONSUMER_PROJECT = $ConsumerProject
$env:NETCOREDBG_MCP_CONSUMER_PYTHON = $ConsumerPython
$script = @'
import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

REQUIRED_TOOLS = {"start_debug", "add_breakpoint", "get_call_stack", "run_runtime_smoke"}


async def _list_tool_names(command, args, cwd, env):
    params = StdioServerParameters(command=command, args=args, cwd=cwd, env=env)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {tool.name for tool in tools.tools}


async def main():
    python_exe = os.environ["NETCOREDBG_MCP_CONSUMER_PYTHON"]
    net_host = os.environ["NETCOREDBG_MCP_CONSUMER_NET_HOST"]
    project = os.environ["NETCOREDBG_MCP_CONSUMER_PROJECT"]
    safe_env = get_default_environment()

    # Direct-Python baseline: the same installed backend, no .NET proxy.
    direct_names = await _list_tool_names(
        python_exe, ["-m", "netcoredbg_mcp", "--project-from-cwd"], project, safe_env
    )

    # Through the candidate .NET host.
    host_env = dict(safe_env)
    host_env["NETCOREDBG_MCP_PYTHON_EXECUTABLE"] = python_exe
    params = StdioServerParameters(
        command=net_host,
        args=["--project-from-cwd"],
        cwd=project,
        env=host_env,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            host_names = {tool.name for tool in tools.tools}
            missing = sorted(REQUIRED_TOOLS - host_names)
            call = await session.call_tool(
                "runtime_smoke_validate_plan",
                {"plan": {"name": "netcoredbg-mcp-host-proxy-check", "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}]}},
            )
            payload = json.loads(call.content[0].text)
            result = {
                "server_name": initialized.serverInfo.name,
                "x_mux": (initialized.capabilities.experimental or {}).get("x-mux"),
                "tools_capability": initialized.capabilities.tools is not None,
                "direct_tool_count": len(direct_names),
                "host_tool_count": len(host_names),
                "missing_tools": missing,
                "missing_from_host": sorted(direct_names - host_names),
                "extra_in_host": sorted(host_names - direct_names),
                "catalog_match": host_names == direct_names,
                "call_is_error": call.isError,
                "call_status": (payload.get("data") or {}).get("status"),
            }
            print(json.dumps(result, sort_keys=True))
            if (
                result["x_mux"] != {"sharing": "isolated"}
                or not result["tools_capability"]
                or missing
                or not result["catalog_match"]
                or result["call_is_error"]
                or result["call_status"] != "PASS"
            ):
                raise SystemExit(1)

asyncio.run(main())
'@
& $ConsumerPython -c $script
```

Expected result:

- Exit code `0`.
- `server_name` is `netcoredbg-mcp-host` — the candidate host's own MCP
  identity, distinct from the Python server process it proxies.
- The initialized server advertises `x-mux.sharing=isolated` and a tools
  capability; `missing_tools` is empty, so every named consumer-critical
  debug and runtime-smoke tool is present.
- `catalog_match` is `true`: the complete host tool-name set fetched through
  `$ConsumerNetHost` exactly equals the complete tool-name set fetched in the
  same run directly from `$ConsumerPython`, with `missing_from_host` and
  `extra_in_host` both empty. This is a live parity proof against the same
  installed backend, not an assumed match with flow 2's own separate run.
- `call_is_error` is `false` and `call_status` is `PASS`: the forwarded
  `tools/call` for `runtime_smoke_validate_plan` against the
  repository-proven minimal plan (`tests/test_host_proxy.py::MINIMAL_PLAN`)
  completes and returns Python's own real `PASS` decision — not a protocol
  fault, and not a silently-accepted `INVALID_SETUP`/`BLOCKED`/`FAIL`.

Supporting protocol check; this source-tree test is mandatory but does not
produce the UXDD verdict:

```powershell
uv run --locked --extra dev pytest tests/critical/test_host_proxy_critical.py -m critical
```

#### 10.4 Evidence Capture

Record in the release report:

- The exact `dotnet publish` command and its reported output path.
- `$ConsumerNetHost` full path plus file size (proves a genuine self-contained
  artifact rather than a framework-dependent build missing its runtime).
- The resolved `NETCOREDBG_MCP_PYTHON_EXECUTABLE` value and its own
  `--version` output, proving which installed backend answered the exchange.
- The full JSON line printed by the client script above.

#### 10.5 Rollback to the Python Console Entrypoint

The candidate host never becomes the registered, default, or published entry
point; rolling back requires no uninstall and no data migration:

1. Stop pointing any MCP client configuration at `$ConsumerNetHost`.
2. Resume launching `$ConsumerCli --project-from-cwd` (flows 1-2 above) — the
   same installed wheel the candidate host was proxying to.
3. Confirm `$ConsumerCli --version` still succeeds; the wheel install is
   untouched by anything the candidate host did, because the host only reads
   the child's stdout/stdin/stderr streams and never writes to the Python
   installation.

#### 10.6 `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN` Semantics (for PKG-001 reuse)

- `PRODUCT_WORKS`: `dotnet publish` succeeds; the real external client observes
  `x-mux.sharing=isolated`, a tools capability, zero missing named tools, and
  `catalog_match=true` (the complete host tool-name set exactly equals the
  complete direct-Python tool-name set fetched live in the same run); and a
  real `tools/call` for the repository-proven minimal plan
  (`tests/test_host_proxy.py::MINIMAL_PLAN`) returns `call_status=PASS` (not
  merely `call_is_error=false`); rollback to `$ConsumerCli` still succeeds
  afterward.
- `PARTIALLY_WORKS`: a named pre-host-start workstation prerequisite blocks
  `dotnet publish` itself, before any process can even attempt to start —
  for example no compatible `-r <RID>` runtime pack for this workstation's
  platform — and the gap is recorded as a concrete `next_step` rather than
  silently skipped. Once the host process has started, no further failure is
  `PARTIALLY_WORKS`.
- `BROKEN`: the publish step fails for a reason other than a named pre-host-
  start prerequisite; the host process fails to start or does not reach
  `initialize`, including because no python interpreter is reachable through
  `NETCOREDBG_MCP_PYTHON_EXECUTABLE`/`PATH` or the resolved interpreter lacks
  the installed wheel; `catalog_match` is `false` or the `x-mux` capability
  diverges from the installed Python journey; the real `tools/call` for the
  minimal plan returns anything other than `call_status=PASS` (including a
  silently-accepted `INVALID_SETUP`); or `$ConsumerCli` stops working after
  the candidate host is exercised.
- This verdict is evidence for **this** candidate journey only; it does not
  itself gate the current wave's release, and it does not claim publication,
  packaging completion, or entry-point cutover. Final installed-package
  acceptance remains PKG-001's gate, which reuses this same
  build/configure/exchange/evidence/rollback contract against the packaged
  artifact instead of a source-tree publish.

## Failure-Mode Catalog

- CLI exits non-zero or fails to import the package.
- MCP server creation fails or required public tools/prompts/resources are absent.
- Launch profile metadata echoes environment values.
- Critical suite discovery finds no `@critical` tests.
- Manual smoke scenario inventory omits WPF or Avalonia fixture scenarios after
  fixture builds succeeded.
- WPF one-call smoke does not return cleanup evidence for the bounded plan.
- Avalonia compatibility is omitted or treated as unsupported without bounded
  evidence from the fixture.
- WPF DataGrid drag/drop is marked passing without `ui.drag` route evidence,
  before/after `ui.grid.viewport` evidence, or selected row identity checks.
- The offscreen row-target drag/drop proof is omitted, or uses raw viewport
  coordinates instead of a row-based drop endpoint with `drop.ensure_visible=true`.
- WinForms `dragList` primitive smoke is used as a substitute for WPF DataGrid
  CR-001 acceptance.
- NovaScript action-oracle verification uses `file.json` only, omits a generated
  `app_diagnostics` probe, or accepts stale app-diagnostics evidence.
- The .NET compatibility-host candidate journey (flow 10) uses a direct
  in-process call instead of a real external `$ConsumerNetHost` process, or
  omits the real MCP client's `initialize`/`tools/list`/`tools/call` exchange.
- The candidate host's tool catalog (a non-empty `missing_from_host` or
  `extra_in_host`, i.e. `catalog_match=false`), `x-mux` capability, or a real
  `tools/call` result diverges from the direct-Python journey without an
  honest `BROKEN` verdict naming the divergence.
- The candidate host's documentation claims publication, packaging
  completion, or that it replaces `netcoredbg-mcp --project-from-cwd` as the
  default entry point.

## Verdict Template

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| CLI version smoke | Exit 0; version printed |  |  |
| MCP surface registration | Critical suite passes |  |  |
| Launch metadata safety | No env values in metadata |  |  |
| Manual smoke surface inventory | WPF, WPF one-call, and Avalonia scenarios listed |  |  |
| WPF one-call runtime smoke | One `run_runtime_smoke` plan returns PASS or an honest BLOCKED/FAIL with cleanup evidence |  |  |
| Avalonia fixture compatibility | Fixture found; key cleanup succeeds; UIA gaps are bounded UNSUPPORTED/BLOCKED evidence |  |  |
| WPF DataGrid drag/drop customer-mode gate | `docs/examples/runtime-smoke-v2-drag-drop-grid.json` returns PASS or an honest BLOCKED with route, viewport, selected-payload, negative no-op, and offscreen row-target `drop.ensure_visible=true` evidence requirements named |  |  |
| NovaScript action-oracle app diagnostics | `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json` expands to `app_diagnostics` and returns PASS or an honest BLOCKED with freshness and cleanup evidence |  |  |
| .NET compatibility-host candidate journey | `$ConsumerNetHost` real external client exchange returns `catalog_match=true` against the same-run direct-Python tool list, matching capabilities, and PASS on a real `tools/call`, or an honest BLOCKED/FAIL naming the missing prerequisite; rollback to `$ConsumerCli` still works |  |  |

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` only when every user journey claimed by the release is
`PRODUCT_WORKS`; otherwise `BLOCK_RELEASE`. Green unit or integration tests do
not override this UXDD verdict.

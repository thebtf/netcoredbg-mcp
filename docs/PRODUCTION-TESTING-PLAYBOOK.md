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

The normal installed producer builds its own WPF/Avalonia fixtures and
production bridge. List its short default without building/launching apps:

```powershell
uv run --locked --extra dev python tests/smoke_test_manual.py --list
```

The result is only `Two-Application Core Smoke`. `--list --extended-gui` lists
the historical base checks and full GUI regression. Exact `--scenario` also
retains standalone WinForms and genuine fresh-start lifecycle checks.
For those legacy console/WinForms diagnostics, build `SmokeTestApp` separately.
Unavailable prerequisites are not evidence of an exercised GUI journey.

#### Normal two-application core smoke (run once)

Use one serial invocation; do not append the standalone commands in sections
5-7 afterward:

```powershell
if (-not $env:NETCOREDBG_PATH) { throw "Set NETCOREDBG_PATH to netcoredbg.exe before running GUI smoke." }
if (-not (Test-Path -LiteralPath $env:NETCOREDBG_PATH)) { throw "NETCOREDBG_PATH does not exist: $env:NETCOREDBG_PATH" }
uv run --locked --extra dev pytest tests/critical/test_typed_bitblt_fallback_public.py -m critical -s
```

This installs a fresh wheel and runs two short public installed-CLI/MCP
journeys: `start_debug` once for WPF, then once for Avalonia, sequentially.
Each discovers/reads `txtOutput`, invokes `btnInvoke` through UIA, asserts
`Invoked 1x`, decodes one screenshot and retains its digest/path/dimensions.
The original PID/birth must remain live through those actions. Existing public
`stop_debug` and cleanup must prove that app, debugger and real production
bridge exited before the next framework starts. Output is compact; no image
base64/UI payload dump. There is no external app owner, shared attach/reset
machinery, control timer, testhost/calibration, historical base/WinForms loop,
full gesture matrix, or app retry. `python tests/smoke_test_manual.py` is an
alternative invocation of the same short entry.

#### Explicit full regression (not part of normal smoke)

```powershell
uv run --locked --extra dev pytest tests/critical/test_typed_bitblt_fallback_public.py tests/critical/test_runtime_smoke_v2_critical.py -m gui_regression -s
```

Default pytest excludes `gui_regression`; explicit `-m gui_regression`
overrides it and runs the original installed nine-case capture matrix plus
the original direct WPF runtime tests, with their original per-case launches.
The capture cases remain `control`, `fallback`, `primary_malformed`,
`target_mismatch`, `dimension_mismatch`, `foreign_target`, `final_black`,
`ordinary_fallback`, `ordinary_final_black`.

`--extended-gui` retains the historical base/WinForms/startup inventory and
all 22 formerly reusable WPF/Avalonia scenarios, and adds that installed
capture regression. Exact `--scenario` keeps each standalone case available.
These explicit full runs are not restricted to two app launches. All original
focus/geometry, trace/identity, no-persistence, expected-BLOCKED and cleanup
assertions remain. Direct manual helpers remain source-helper evidence,
not installed proof. A short PASS never clears full-regression FAIL/UNKNOWN.
Genuine `Stealth Launch`, delayed-readiness and root-lifecycle startup checks
retain their separate original meaning. Do not append diagnostics to normal
smoke or claim the short journey proves that broader matrix.

To retain every output in one primary-owner run directory, set
`NETCOREDBG_TWO_GUI_RUN_ROOT` there and set `NETCOREDBG_MCP_TEST_OUTPUT_ROOT`
to its `test-output` child before the invocation. Builds, caches, scratch,
installed consumer, stdout/stderr and `two-application-evidence.json` then
stay under that directory. Missing prerequisites or a missing/failed case
fail closed; fixture availability is never used to skip the normal GUI proof.


### 5. Supporting WPF Fixture Workflows

#### One-Call Runtime-Smoke Workflow

Standalone diagnostic command (covered by the explicit full regression, not short smoke):

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

For v1 plans on the same registered MCP session, UI use without `stop_debug`
retains the backend for later calls. A later stop-only plan with
`cleanup: {stop_debug: graceful}` disconnects that existing backend before
session stop drains the debugger adapter. Cleanup never creates or reconnects a
backend, including when the retained backend has already disconnected. UI
disconnect failure remains in the public cleanup result while session stop still
runs. An unused backend adds no UI cleanup action. The session owner preserves
launched-versus-attached target disposal.

#### Installed WPF Submenu Consumer Journey

This journey uses the real WPF fixture as its debuggee, but it is consumer
evidence: `$ConsumerPython` runs the official MCP client SDK installed with the
built wheel and starts `$ConsumerCli`. The helper imports no repository package
code; every server operation below crosses the installed MCP `tools/call`
surface. The fixture build is preparation, not the proof itself.

Commands:

```powershell
if (-not $env:NETCOREDBG_PATH) { throw "Set NETCOREDBG_PATH to netcoredbg.exe before the submenu journey" }
if (-not (Test-Path -LiteralPath $env:NETCOREDBG_PATH)) { throw "NETCOREDBG_PATH does not exist: $env:NETCOREDBG_PATH" }
dotnet build tests/fixtures/WpfSmokeApp -c Debug
if ($LASTEXITCODE -ne 0) { throw "dotnet build failed." }
& $ConsumerCli --setup
if ($LASTEXITCODE -ne 0) { throw "Consumer setup failed." }
$env:NETCOREDBG_MCP_CONSUMER_CLI = $ConsumerCli
$env:NETCOREDBG_MCP_WPF_ROOT = (Resolve-Path tests/fixtures/WpfSmokeApp).Path
$env:FLAUI_BRIDGE_PATH = Join-Path $HOME ".netcoredbg-mcp\bridge\FlaUIBridge.exe"
if (-not (Test-Path -LiteralPath $env:FLAUI_BRIDGE_PATH)) { throw "Run $ConsumerCli --setup before the submenu journey" }
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
& $ConsumerPython tests/wpf_submenu_consumer.py
```

Expected result:

- All commands exit `0`.
- Output starts with `WPF installed submenu evidence:` and reports the installed
  CLI, public `start_debug`, `ui_find_element`, `ui_key_sequence`,
  `ui_get_window_tree`, `ui_invoke`, and `ui_text` calls.
- The bounded polling records each discovery operation and, on a deadline,
  prints `terminal_event=timeout_no_response` for no response or
  `terminal_event=deadline_elapsed_after_response` after a received response.
  Both retain the last received public response, attempt count, and deadline.
- Before native `ENTER` expansion, the bounded public tree omits `submenuChild`.
  After it, bounded polling independently rediscovers the exact popup child with
  `ui_find_element`. The journey intentionally does not require a second broad
  tree traversal while the native popup is open. The fixture has no
  submenu-specific key handler.
- The separate child invocation reports `invoked=true` and exactly
  `method=InvokePattern`; `WpfWorkflow Submenu child invoked` is the observable
  result.

### 6. Supporting Avalonia Fixture Compatibility

Standalone diagnostic command (already covered by the normal owner above):

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
5. When this drag/drop boundary is claimed or changed, explicitly select the
   existing fixture-backed WPF drag/drop diagnostics through `--scenario`
   or `--extended-gui`. Their original per-case launches and assertions are
   retained; normal two-application core smoke does not exercise this matrix.
   Keep any failed/unknown full-regression outcomes distinct from short PASS.
   Supporting source-helper results do not substitute for the installed-server
   customer-mode run in steps 1-4.

6. Run the supporting documentation guards; select direct runtime GUI
   regressions explicitly when their boundary is in scope:

```powershell
uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py
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
            prompts = await session.list_prompts()
            prompt_names = [prompt.name for prompt in prompts.prompts]
            resources = await session.list_resources()
            resource_uris = sorted(str(resource.uri) for resource in resources.resources)
            templates = await session.list_resource_templates()
            state = await session.read_resource("debug://state")
            result = {
                "server_name": initialized.serverInfo.name,
                "x_mux": (initialized.capabilities.experimental or {}).get("x-mux"),
                "tools_capability": initialized.capabilities.tools is not None,
                "prompts_capability": initialized.capabilities.prompts is not None,
                "resources_capability": initialized.capabilities.resources is not None,
                "resources_subscribe": bool(
                    initialized.capabilities.resources
                    and initialized.capabilities.resources.subscribe
                ),
                "logging_capability": initialized.capabilities.logging is not None,
                "direct_tool_count": len(direct_names),
                "host_tool_count": len(host_names),
                "missing_tools": missing,
                "missing_from_host": sorted(direct_names - host_names),
                "extra_in_host": sorted(host_names - direct_names),
                "catalog_match": host_names == direct_names,
                "call_is_error": call.isError,
                "call_status": (payload.get("data") or {}).get("status"),
                "prompt_names": prompt_names,
                "resource_uris": resource_uris,
                "resource_template_count": len(templates.resourceTemplates),
                "state_read_ok": bool(state.contents and '"execState"' in (state.contents[0].text or "")),
            }
            print(json.dumps(result, sort_keys=True))
            if (
                result["server_name"] != "netcoredbg-mcp-host"
                or result["x_mux"] != {"sharing": "isolated"}
                or not result["tools_capability"]
                or not result["prompts_capability"]
                or not result["resources_capability"]
                or not result["resources_subscribe"]
                or result["logging_capability"]
                or missing
                or not result["catalog_match"]
                or result["call_is_error"]
                or result["call_status"] != "PASS"
                or result["prompt_names"]
                != [
                    "debug",
                    "debug-gui",
                    "debug-exception",
                    "debug-visual",
                    "debug-mistakes",
                    "investigate",
                    "debug-scenario",
                    "dap-escape-hatch",
                ]
                or result["resource_uris"]
                != [
                    "debug://breakpoints",
                    "debug://output",
                    "debug://state",
                    "debug://threads",
                ]
                or result["resource_template_count"] != 0
                or not result["state_read_ok"]
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
- Prompts capability is present and `prompt_names` is exactly the eight native
  host prompts (`debug`, `debug-gui`, `debug-exception`, `debug-visual`,
  `debug-mistakes`, `investigate`, `debug-scenario`, `dap-escape-hatch`).
- Resources capability is present with `subscribe=true`; `resource_uris` is
  exactly the four `debug://` URIs; `resource_template_count` is `0`; and a
  real `resources/read` of `debug://state` succeeds (`state_read_ok=true`).
- Logging capability is absent (`logging_capability=false`), matching the
  installed Python backend; progress/logging tokens still forward when Python
  emits them (covered by the supporting critical host-proxy gate below, not by
  inventing a second consumer smoke).

The same installed-host journey also owns roots reverse routing, resource
subscribe/update/unsubscribe, progress notifications before a final tool
response, and x-mux metadata ownership. Those surfaces are exercised load-
bearingly by the supporting critical gate rather than inventing a second
parallel installed smoke here, so the consumer journey stays one real external
process exchange while still requiring the full front-door matrix to pass.

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
- The resolved `NETCOREDBG_MCP_PYTHON_EXECUTABLE` value and the installed
  backend package version from `& $ConsumerPython -m netcoredbg_mcp
  --version` — plain interpreter `--version` only proves which Python ran,
  not that the `netcoredbg-mcp` package is even installed there.
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
  `server_name` exactly equal to `netcoredbg-mcp-host` (never the Python
  server's own identity or any other value), `x-mux.sharing=isolated`, a
  tools capability, prompts capability, resources capability with
  `subscribe=true`, no logging capability (matching installed Python), zero
  missing named tools, and `catalog_match=true` (the complete host tool-name
  set exactly equals the complete direct-Python tool-name set fetched live in
  the same run); the eight native prompts list and four `debug://` resources
  with zero templates plus a successful `debug://state` read; and a real
  `tools/call` for the repository-proven minimal plan
  (`tests/test_host_proxy.py::MINIMAL_PLAN`) returns `call_status=PASS` (not
  merely `call_is_error=false`); the supporting critical host-proxy gate also
  proves roots, progress/logging, resource subscriptions, and x-mux ownership
  through the same real host process surface; rollback to `$ConsumerCli` still
  succeeds afterward.
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
  the installed wheel; `server_name` is anything other than
  `netcoredbg-mcp-host` (for example the proxied Python server's own name,
  meaning the client bypassed the candidate host entirely); `catalog_match`
  is `false` or the `x-mux` capability diverges from the installed Python
  journey; the real `tools/call` for the minimal plan returns anything
  other than `call_status=PASS` (including a silently-accepted
  `INVALID_SETUP`); or `$ConsumerCli` stops working after the candidate
  host is exercised.
- This verdict is evidence for **this** candidate journey only; it does not
  itself gate the current wave's release, and it does not claim publication,
  packaging completion, or entry-point cutover. Final installed-package
  acceptance remains PKG-001's gate, which reuses this same
  build/configure/exchange/evidence/rollback contract against the packaged
  artifact instead of a source-tree publish.

### 11. A1 Opt-In Read-Only Preview — Retained and Public-Remote Journey

This journey is separate from flow 10's Python-backed compatibility proxy and
flows 1–9's installed Python/PyPI product. A1 exposes only read-only
`find_code_symbol`; it does not add desktop debugging or change the default.
Source availability, a local publish, a unit PASS or controlled loopback download
is not evidence that an A1 GitHub prerelease exists or works for a consumer.

**A2 successor:** the [native read-only family](../specs/016-a2-native-readonly-family/spec.md)
uses the same retained/public acquisition and rollback entry points below, but
its immutable schema-2 Candidate/manifest selects the complete A2 matrix and
exactly `find_code_symbol`, `find_code_references`, and `get_source_context`.
The consumer checks all three advertised input/output schemas, exact reference
locations and numbered source context, route-specific refusals and clean EOF.
Follow the [A2 generation contract](../specs/010-a1-preview-artifact/contracts/stateless-preview-workflow.md#a2-successor-proof-generation);
do not apply A1's one-tool denominator to successor bytes or select a proof
generation from receipt fields. The A1 description below remains the historical
schema-1 contract; source/built-process PASS alone does not complete A2 delivery.

#### 11.1 Exact Inputs and Two Consumer Modes

Follow the [A1 artifact quickstart](../specs/010-a1-preview-artifact/quickstart.md#inputs)
to obtain the four expanded Actions metadata references in
`C:\preview-validation\candidate-references.json`, create separate empty ordinary
download roots, and prepare the **unchanged retained-Python oracle**. That
procedure supplies `$consumerPython` and
`.agent/tmp/t001-retained-python-consumer.py`; an arbitrary successful command is
not an oracle. Use Windows x64 with PowerShell, Python, GitHub CLI, a contained
`tests\fixtures\PreviewSearchApp` fixture and permission to create its required
reparse fixtures. Missing denial fixtures refuse rather than skip the matrix.

Run from the repository root. Replace receipt IDs and RFC 3339 UTC `Z` timestamps
with actual values. Receipt output paths must not already exist. First, retained
proof uses only the sealed build's retained archive/manifest:

```powershell
python tests/preview/validate_preview_artifact.py candidate `
  --references C:\preview-validation\candidate-references.json `
  --download-root C:\preview-validation\downloads `
  --fixture-root tests\fixtures\PreviewSearchApp `
  --receipt-output C:\preview-validation\retained-artifact-proof.json `
  --receipt-id retained-proof-<opaque-id> `
  --recorded-at <utc-rfc3339-timestamp-ending-z> `
  --python-rollback-command $consumerPython `
  --python-rollback-argument .agent/tmp/t001-retained-python-consumer.py
```

After actual publication, the separate remote journey requires the same sealed
references plus the matching positive numeric GitHub release ID:

```powershell
python tests/preview/validate_preview_artifact.py remote `
  --references C:\preview-validation\candidate-references.json `
  --release-id <positive-github-release-id> `
  --download-root C:\preview-validation\remote-downloads `
  --fixture-root tests\fixtures\PreviewSearchApp `
  --receipt-output C:\preview-validation\remote-release-proof.json `
  --receipt-id remote-proof-<opaque-id> `
  --recorded-at <utc-rfc3339-timestamp-ending-z> `
  --python-rollback-command $consumerPython `
  --python-rollback-argument .agent/tmp/t001-retained-python-consumer.py
```

`--python-rollback-argument` is repeatable. Neither mode accepts token/URL/mirror
overrides, local payload input or partial-matrix options.
Expanded transport metadata includes artifact name, ZIP digest and retention;
compact durable record locators contain only repository/run/artifact ID/path/raw
hash. Keep raw file hashes, Actions container digest and Candidate's ordered
archive-plus-manifest payload digest distinct.

#### 11.2 Public Bytes, Exact Identity and Full Runtime Matrix

Remote mode still resolves identity/catalog through retained Actions evidence,
but uses fresh **anonymous** stdlib HTTPS reads for the public archive/manifest.
It sends no Authorization or inherited GitHub credentials and accepts no publisher
cache as proof. The downloader checks the public repository and nondraft
prerelease, exact release/tag/commit, annotated tag object dereferenced to the
candidate commit, and exactly two distinct matching asset IDs/names/sizes. Bounded
HTTP 200 or allowlisted HTTPS 302 asset delivery must yield exact raw hashes;
the ordered pair digest and manifest's extracted-executable equation must pass.
Stable release/tag/asset identity is re-read after downloading; provider download
counters are not identity. A symbolic target or nullable API digest is not byte
proof. Only the verified extracted executable may run.

The controller passes `--project <absolute-fixture-root>` explicitly and requires
the full closed [consumer matrix](../specs/010-a1-preview-artifact/quickstart.md#what-both-modes-must-prove):
tools-only `server/discover`, exactly `find_code_symbol` in `tools/list`, successful
`PreviewMarker` class search via `tools/call`, every root/configuration/containment/
input/unreadable-file/resource-ceiling/protocol denial, and clean EOF/cancellation
without partial output or unintended side effects. Hostile CWD/environment values
must not change project authority. Stdout stays JSON-RPC-only; stdin close must
exit cleanly without a cancellation result or retained state. One absent or
failed scenario refuses proof; no source-bin/rebuild shortcut is accepted.

#### 11.3 Unchanged Python Rollback and Evidence Boundary

Both modes replay the same retained installed-Python driver. Remove only explicit
preview selection; do not reinstall/replace Python, alter its console entry point
or change its default selector. The controller prepends the selected interpreter's
directory only to the rollback child's PATH, leaves the parent unchanged, and
requires the driver's exact `PRODUCT_WORKS` JSON result, not just exit zero.
The Python/PyPI installation and instructions above remain intact.

A successful command exits zero, prints
`PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED`, and writes a new receipt with
`proof_stage: retained_artifact` or `remote_release`. A proof refusal exits
nonzero, prints `PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED` to stderr and writes no
receipt. Preserve the actual command, original receipt bytes/hash, candidate
binding, complete scenario denominator and exact remote asset identities.

Remote Verification and Program B Handoff are separate pure
[source-only APIs](../specs/010-a1-preview-artifact/contracts/program-b-handoff.md#source-only-sealing-apis),
not evidence collectors or authority. They require real original predecessor
records, Attempt 3.0 exact-source execution policy, admitted predecessor and
Observation 2.0 operation history, genuine pre/post byte fragments and all three
catalog Stage subsets. Source/blob equality is not observed grants or token
introspection; DENIED and unresolved UNKNOWN cannot be erased by matching state.
Missing pre-publication fragments refuse even after `published_complete`;
published recovery cannot invent them. Neither API authorizes Program B/C,
defaults or package cutover.

Controlled-loopback HTTP/MCP/EOF/tamper checks and simulated unit records are
supporting source proof, not public GitHub release receipts. Record actual source,
runtime and publication results separately before closing the corresponding
tasks. A retained-only receipt cannot establish the public-remote journey;
unit results or source availability cannot make an incomplete or failed consumer
journey `PRODUCT_WORKS`.

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
  `extra_in_host`, i.e. `catalog_match=false`), `x-mux` capability, prompts
  catalog, resources/read/subscribe surface, or a real `tools/call` result
  diverges from the direct-Python journey without an honest `BROKEN` verdict
  naming the divergence.
- The candidate host's documentation claims publication, packaging
  completion, or that it replaces `netcoredbg-mcp --project-from-cwd` as the
  default entry point.
- A1 preview proof substitutes retained/cache/local-build bytes for anonymous
  public assets, omits any closed matrix/EOF/unchanged-Python-rollback scenario,
  or treats a pure verification/handoff record as live evidence or Program B/C
  authority.
- Flow 10 invents a second parallel installed smoke instead of extending the
  same `$ConsumerNetHost` exchange plus the supporting
  `tests/critical/test_host_proxy_critical.py` gate for roots, progress/
  logging, prompts, resources/subscriptions, and x-mux.

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
| .NET compatibility-host candidate journey | `$ConsumerNetHost` real external client exchange returns `catalog_match=true` against the same-run direct-Python tool list, matching tools/prompts/resources/x-mux capabilities (logging absent), eight prompts, four resources/zero templates/read, and PASS on a real `tools/call`, with supporting critical proof of roots/progress/subscriptions; or an honest BLOCKED/FAIL naming the missing prerequisite; rollback to `$ConsumerCli` still works |  |  |
| A1 opt-in preview journey (when claimed) | Exact retained and fresh anonymous public assets bind the candidate/annotated tag/raw hashes; sole read-only capability, complete denial matrix, EOF and unchanged installed-Python rollback pass with original receipts; no Program B/C or default/package authority inferred |  |  |

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` only when every user journey claimed by the release is
`PRODUCT_WORKS`; otherwise `BLOCK_RELEASE`. Green unit or integration tests do
not override this UXDD verdict.

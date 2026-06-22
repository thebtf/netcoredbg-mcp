# Production Testing Playbook

## Scope

This playbook covers the public surfaces of `netcoredbg-mcp` that a user relies
on after installing the package:

- The `netcoredbg-mcp` CLI entry point.
- MCP server surface registration for tools, prompts, and resources.
- Launch environment profile handling and secret-safe metadata.
- Runtime smoke orchestration and GUI fixture availability.
- WPF DataGrid drag/drop and edge-scroll runtime-smoke v2 proof.

The playbook is intentionally executable on a release workstation without a
private downstream application. GUI debugging against WPF, WinForms, and
Avalonia still depends on a Windows host, `netcoredbg`, and a target .NET app;
those flows are covered by the manual smoke suite and can be run when a full
Windows debug stand is available. Avalonia is a first-class GUI fixture target
because it is a future migration path for the UI automation surface.

## Prerequisites

- Python 3.10 or newer.
- Development dependencies installed through the locked `uv` environment.
- Commands run from the repository root.
- `NETCOREDBG_PATH` points at the installed `netcoredbg.exe` for GUI smoke
  commands.
- For package installation checks, a local wheel can be built with
  `uv build`.
- For full GUI smoke coverage, build all three fixture apps:
  `tests/fixtures/SmokeTestApp`, `tests/fixtures/WpfSmokeApp`, and
  `tests/fixtures/AvaloniaSmokeApp`.

## Canonical Flows

### 1. CLI Version Smoke

Command:

```powershell
uv run --locked --extra dev python -m netcoredbg_mcp --version
```

Expected result:

- Exit code `0`.
- Output includes the package version from `src/netcoredbg_mcp/__init__.py`.

### 2. MCP Surface Registration

Command:

```powershell
uv run --locked --extra dev pytest tests/critical/test_release_critical.py -m critical
```

Expected result:

- The critical suite passes.
- The MCP server registers core debug tools including `start_debug`,
  `add_breakpoint`, and `get_call_stack`.
- The server registers core prompts and resources including `debug://state`,
  `debug://breakpoints`, and `debug://output`.

### 3. Launch Environment Metadata Safety

Command:

```powershell
uv run --locked --extra dev pytest tests/critical/test_release_critical.py::test_launch_profile_metadata_never_exposes_environment_values
```

Expected result:

- The test passes.
- Resolved launch environments may contain values internally.
- User-facing metadata contains variable names and counts only; it does not
  contain inherited, profile, or direct environment values.

### 4. Manual Smoke Surface Inventory

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

### 5. WPF One-Call Runtime Smoke Workflow

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

### 6. Avalonia UI Fixture Compatibility

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
3. Run the v2 plan through `run_runtime_smoke` on a Windows GUI workstation with
   a backend that can produce real pointer route evidence and
   `ui.grid.viewport` identity evidence.
4. Include the offscreen row-target drag/drop case from the example: the target
   row is offscreen, the drop endpoint is expressed as a row-based target with
   `drop.ensure_visible=true`, and the run must either prove the gesture or
   fail closed before side effects if target-side realization hides the drag
   source. This is a bounded CR-075 customer-mode proof contract for broad
   `#270`; it does not close the broad helper family by itself.
5. Run the fixture-backed WPF v2 drag/drop customer-mode smoke:

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

6. Run the release-critical guard:

```powershell
uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py
uv run --locked --extra dev pytest tests/critical/test_runtime_smoke_v2_critical.py -m critical
```

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

### 8. Runtime-Smoke Diagnostic Schema Gate

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
uv run --no-sync --project <NETCOREDBG_MCP_REPO> netcoredbg-mcp --version
```

Expected preflight result:

- Exit code `0`.
- Output is `netcoredbg-mcp 0.20.5`.
- If a live MCP mux session holds the development `.venv` executable, use the
  direct `.venv\Scripts\netcoredbg-mcp.exe --version` route instead of treating
  the uv sync lock as stale code.

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
3. Run the plan through the active v0.20.5 MCP server with the
   `run_runtime_smoke` tool from the NovaScript repository root.
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

This gate is the post-release consumer-readiness slice for the current v0.20.5
NovaScript-facing action-oracle/app-diagnostics path. It does not replace the
CR-003 DataGrid drag/drop replay gate above.

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

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` / `BLOCK_RELEASE`

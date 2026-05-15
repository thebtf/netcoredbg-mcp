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
- Development dependencies installed.
- Commands run from the repository root.
- `NETCOREDBG_PATH` points at the installed `netcoredbg.exe` for GUI smoke
  commands.
- For package installation checks, a local wheel can be built with
  `python -m build`.
- For full GUI smoke coverage, build all three fixture apps:
  `tests/fixtures/SmokeTestApp`, `tests/fixtures/WpfSmokeApp`, and
  `tests/fixtures/AvaloniaSmokeApp`.

## Canonical Flows

### 1. CLI Version Smoke

Command:

```powershell
python -m netcoredbg_mcp --version
```

Expected result:

- Exit code `0`.
- Output includes the package version from `src/netcoredbg_mcp/__init__.py`.

### 2. MCP Surface Registration

Command:

```powershell
python -m pytest tests/critical/test_release_critical.py -m critical
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
python -m pytest tests/critical/test_release_critical.py::test_launch_profile_metadata_never_exposes_environment_values
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
python tests/smoke_test_manual.py --list
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
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
python -c "import asyncio, sys; import tests.smoke_test_manual as s; asyncio.run(s.test_wpf_one_call_runtime_smoke_workflow()); print(f'RESULTS: {s.passed} passed, {s.failed} failed out of {s.passed + s.failed} checks'); sys.exit(0 if s.failed == 0 else 1)"
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
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
python -c "import asyncio, sys; import tests.smoke_test_manual as s; asyncio.run(s.test_avalonia_ui_fixture_compatibility()); print(f'RESULTS: {s.passed} passed, {s.failed} failed out of {s.passed + s.failed} checks'); sys.exit(0 if s.failed == 0 else 1)"
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
4. Run the release-critical guard:

```powershell
python -m pytest tests/test_runtime_smoke_v2_docs.py tests/critical/test_runtime_smoke_v2_critical.py -m critical
```

Expected result:

- `PRODUCT_WORKS`: the configured WPF stand returns `PASS`, the result includes
  backend-produced `ui.drag` `route_evidence`, before/after `ui.grid.viewport`
  snapshots, selected payload identity continuity, row count preservation, and
  negative no-op cleanup evidence.
- `PARTIALLY_WORKS`: the JSON example parses and the critical guard passes, but
  the live stand is absent or returns an honest `BLOCKED` result that names the
  missing pointer, route, viewport, selected-payload, no-op, or cleanup
  capability with a concrete `next_step`.
- `BROKEN`: the plan returns `FAIL` or `INVALID_SETUP`; a drag result reports
  `PASS` without backend-produced route evidence; viewport or row identity
  evidence is missing without `BLOCKED`; or docs/tests use another fixture as a
  substitute for WPF DataGrid acceptance.

WinForms `dragList` primitive smoke is not a substitute for WPF DataGrid CR-001
acceptance.

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
- WinForms `dragList` primitive smoke is used as a substitute for WPF DataGrid
  CR-001 acceptance.

## Verdict Template

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| CLI version smoke | Exit 0; version printed |  |  |
| MCP surface registration | Critical suite passes |  |  |
| Launch metadata safety | No env values in metadata |  |  |
| Manual smoke surface inventory | WPF, WPF one-call, and Avalonia scenarios listed |  |  |
| WPF one-call runtime smoke | One `run_runtime_smoke` plan returns PASS or an honest BLOCKED/FAIL with cleanup evidence |  |  |
| Avalonia fixture compatibility | Fixture found; key cleanup succeeds; UIA gaps are bounded UNSUPPORTED/BLOCKED evidence |  |  |
| WPF DataGrid drag/drop customer-mode gate | `docs/examples/runtime-smoke-v2-drag-drop-grid.json` returns PASS or an honest BLOCKED with route and viewport evidence requirements named |  |  |

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` / `BLOCK_RELEASE`

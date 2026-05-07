# Production Testing Playbook

## Scope

This playbook covers the public surfaces of `netcoredbg-mcp` that a user relies
on after installing the package:

- The `netcoredbg-mcp` CLI entry point.
- MCP server surface registration for tools, prompts, and resources.
- Launch environment profile handling and secret-safe metadata.
- Runtime smoke orchestration and GUI fixture availability.

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
  Evidence`, and `Avalonia UI Fixture Compatibility`.
- Missing GUI fixture binaries are treated as reduced coverage, not as proof
  that the corresponding GUI surface was exercised.

## Failure-Mode Catalog

- CLI exits non-zero or fails to import the package.
- MCP server creation fails or required public tools/prompts/resources are absent.
- Launch profile metadata echoes environment values.
- Critical suite discovery finds no `@critical` tests.
- Manual smoke scenario inventory omits WPF or Avalonia fixture scenarios after
  fixture builds succeeded.

## Verdict Template

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| CLI version smoke | Exit 0; version printed |  |  |
| MCP surface registration | Critical suite passes |  |  |
| Launch metadata safety | No env values in metadata |  |  |
| Manual smoke surface inventory | WPF and Avalonia scenarios listed |  |  |

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` / `BLOCK_RELEASE`

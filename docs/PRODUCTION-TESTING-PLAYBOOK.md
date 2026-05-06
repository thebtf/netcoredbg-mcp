# Production Testing Playbook

## Scope

This playbook covers the public surfaces of `netcoredbg-mcp` that a user relies
on after installing the package:

- The `netcoredbg-mcp` CLI entry point.
- MCP server surface registration for tools, prompts, and resources.
- Launch environment profile handling and secret-safe metadata.

The playbook is intentionally executable on a release workstation without a
private downstream application. GUI debugging against WPF/WinForms still depends
on a Windows host, `netcoredbg`, and a target .NET app; those flows are covered
by the manual smoke suite and can be run when a full Windows debug stand is
available.

## Prerequisites

- Python 3.10 or newer.
- Development dependencies installed.
- Commands run from the repository root.
- For package installation checks, a local wheel can be built with
  `python -m build`.

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

## Failure-Mode Catalog

- CLI exits non-zero or fails to import the package.
- MCP server creation fails or required public tools/prompts/resources are absent.
- Launch profile metadata echoes environment values.
- Critical suite discovery finds no `@critical` tests.

## Verdict Template

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| CLI version smoke | Exit 0; version printed |  |  |
| MCP surface registration | Critical suite passes |  |  |
| Launch metadata safety | No env values in metadata |  |  |

Overall verdict: `PRODUCT_WORKS` / `PARTIALLY_WORKS` / `BROKEN`

Gate decision: `PASS` / `BLOCK_RELEASE`

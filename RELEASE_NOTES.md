# netcoredbg-mcp v0.18.5

Released: 2026-06-19

## Summary

`v0.18.5` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes CR-052 after `v0.18.4`, so package consumers can request
bounded DataGrid viewport identity evidence through the public MCP helper
surface.

## Highlights

- `ui_grid(action="viewport")` now returns bounded visible-row DataGrid viewport
  identity snapshots by delegating to the existing runtime-smoke
  `ui.grid.viewport` adapter.
- The helper forwards selector, `rows`, `identity`, adapter-owned `expect`,
  `phase`, and `probe_name` fields without duplicating runtime-smoke v2
  before/after comparison logic.
- Direct public helper calls now fail closed for comparison-only expectations
  such as `viewport_moved` or `direction`; use `runtime_smoke_run_probe` or
  `runtime_smoke_run_plan` with `kind="ui.grid.viewport"` for before/after
  viewport comparisons.
- Broad issue `#270` remains open for broader DataGrid offscreen/scroll action
  semantics, arbitrary click variants, downstream replay tails, and broader
  helper ergonomics.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  tightens runtime-smoke validation/freshness behavior.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.5
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.5
  netcoredbg-mcp --setup
  ```

- Runtime-smoke and UI helper failures continue to prefer bounded `BLOCKED`,
  `INVALID_SETUP`, `WARN`, or `FAIL` evidence over false `PASS` results.
- Broad issue-backlog rows remain open by design; this release ships bounded
  provider-side slices, not every downstream consumer replay.

## Release Gates

- Release-prep PR must pass MCP PR review before merge.
- Critical suite must pass with `uv run --locked --extra dev pytest tests/critical -m critical`.
- Runtime-smoke docs/schema gates must pass for the shipped examples.
- Package build and disposable wheel install smoke must pass.
- Production playbook must be executed and recorded before tagging.
- After tag push, GitHub Release, PyPI workflow, remote tag, and local
  workstation deployment must be verified before the release is called shipped.

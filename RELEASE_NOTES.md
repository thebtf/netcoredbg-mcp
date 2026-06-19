# netcoredbg-mcp v0.18.6

Released: 2026-06-19

## Summary

`v0.18.6` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes CR-053 after `v0.18.5`, so package consumers can opt into
ensure-visible handling for DataGrid row select and click actions without
changing the default visible-row-only behavior.

## Highlights

- DataGrid row select and click actions now accept explicit
  `ensure_visible=True` behavior through the public `ui_grid` helper, legacy
  runtime-smoke execution, and runtime-smoke v2 actions.
- Existing row actions remain visible-row-only by default, preserving
  deterministic behavior for current consumers.
- Runtime-smoke v2 now normalizes unsupported or invalid ensure-visible
  preflight results to terminal failure statuses instead of allowing a skipped
  row action to report `PASS`.
- Broad issue `#270` remains open for broader DataGrid offscreen/scroll action
  semantics, arbitrary click variants, downstream replay tails, and broader
  helper ergonomics.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  tightens runtime-smoke validation/freshness behavior.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.6
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.6
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

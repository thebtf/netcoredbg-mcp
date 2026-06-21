# netcoredbg-mcp v0.19.0

Released: 2026-06-21

## Summary

`v0.19.0` is a feature release for the runtime-smoke and UI-emulation roadmap.
It publishes the accumulated post-`v0.18.8` provider-side work for WPF DataGrid
drag/drop evidence gates, richer app-diagnostics evidence, NovaScript action
oracles, and stricter fail-closed runtime-smoke behavior.

## Highlights

- WPF DataGrid drag/drop plans now cover positive row-target drops, offscreen
  target realization, viewport preflight, and before/after `ui.grid.viewport`
  evidence; negative no-op evidence remains backend-limited and explicitly
  bounded in the playbook.
- Runtime-smoke v2 now carries clearer app-diagnostics event deltas, live
  diagnostic history, intra-case progress, and source-aware cursor guidance.
- NovaScript action-oracle generation can emit bounded `app_diagnostics` probes
  for action success oracles instead of routing every oracle through `file.json`.
- UI grid helpers expanded across viewport, assert-range, ensure-visible,
  right-click row, and double-click row evidence paths.
- Reproduction/backlog notes were reconciled with Engram follow-up comments for
  the broad `#268` through `#272` issue group while keeping those broad rows
  open for remaining lifecycle scope.

## Upgrade Notes

- This is a MINOR feature release. It keeps the existing MCP server shape while
  adding runtime-smoke v2 and UI evidence capabilities.
- Review the updated WPF drag/drop example and production testing playbook before
  adopting the new customer-mode flows:
  `docs/examples/runtime-smoke-v2-drag-drop-grid.json` and
  `docs/PRODUCTION-TESTING-PLAYBOOK.md`.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.19.0
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.19.0
  netcoredbg-mcp --setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain open
  by design. This release ships bounded provider-side slices and recorded
  lifecycle follow-up evidence; it does not claim full downstream replay closure.
- Some UI automation behavior remains Windows GUI and backend dependent. Release
  claims are scoped to the documented runtime-smoke and production-playbook
  evidence paths.
- WPF DataGrid negative no-op drag evidence remains backend-limited in the local
  FlaUI fixture. The release playbook records it as actionable bounded `BLOCKED`
  coverage; positive drag/drop, offscreen row-target, edge-scroll, multi-row
  payload, and cleanup checks passed.

## Release Gates

- Release-git-readiness has current PASS evidence after stale dirty work was
  preserved with patch artifacts, stash entries, and backup refs.
- Release-prep PR must pass MCP PR review before merge.
- Critical suite must pass with
  `uv run --locked --extra dev pytest tests/critical -m critical`.
- Runtime-smoke docs/schema gates must pass for the shipped examples.
- Package build and disposable wheel install smoke must pass.
- Production playbook must be executed and recorded before tagging.
- After tag push, GitHub Release, PyPI workflow, remote tag, and local
  workstation deployment must be verified before the release is called shipped.

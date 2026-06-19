# netcoredbg-mcp v0.18.2

Released: 2026-06-19

## Summary

`v0.18.2` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes the CR-045 through CR-048 slices that landed after
`v0.18.1`, so consumers can test YAML plan files, read-only probe validation,
runner-exception cleanup evidence, and app-diagnostic freshness guardrails from
the package instead of source `main`.

## Highlights

- `plan_path` now accepts `.yaml` and `.yml` runtime-smoke plans in the existing
  validation and run-plan facades.
- Durable v2 runner exceptions now keep v2-shaped exception evidence and execute
  declared cleanup/contamination handling.
- `runtime_smoke_validate_probe` validates a single v2 probe without starting a
  durable run, launching a target, claiming session ownership, or creating an
  evidence directory.
- App diagnostics now fail closed when an app-written `PASS` contradicts live
  debug freshness evidence for process, module, source, workspace, or artifact
  expectations.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  tightens runtime-smoke validation/freshness behavior.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.2
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.2
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

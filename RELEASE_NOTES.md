# netcoredbg-mcp v0.18.3

Released: 2026-06-19

## Summary

`v0.18.3` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes CR-049 after `v0.18.2`, so consumers can test resilient
v2 runner-exception cleanup when a case cleanup adapter itself raises during
failure finalization.

## Highlights

- Runtime-smoke v2 runner-exception finalization now records raised case
  cleanup adapter exceptions as cleanup failure evidence.
- Plan-level cleanup still runs after a raised case cleanup adapter, including
  declared `debug.stop` and `process.registry.assert_empty` steps.
- Cleanup adapter failures now carry exception type, message, and traceback
  diagnostics in the evidence payload.
- This release resolves the PR #139 MCP review follow-up without closing broad
  issue rows `#268`, `#269`, or `#271`.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  tightens runtime-smoke validation/freshness behavior.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.3
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.3
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

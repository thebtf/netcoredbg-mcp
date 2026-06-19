# netcoredbg-mcp v0.18.8

Released: 2026-06-19

## Summary

`v0.18.8` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes CR-056 after `v0.18.7`, giving package consumers
incremental `app_diagnostics.poll` acquisition through a durable `since` cursor
without changing existing poll calls that do not provide a cursor.

## Highlights

- `app_diagnostics.poll.since` accepts `{mtime_ns, name}` cursor values that use
  the same ordering as directory candidate selection.
- Directory polling ignores stale or equal diagnostic JSON snapshots instead of
  re-consuming a previous app-written `PASS` artifact.
- Successful directory polls return a `cursor` with the matched file's
  `mtime_ns` and `name`, so the next consumer poll can continue from that point.
- Public `runtime_smoke_run_probe` app-diagnostics runs inherit the same
  fail-closed stale-snapshot behavior.
- Broad issue `#272` remains open for the remaining app-diagnostics lifecycle,
  orchestration, and generic probe-authoring scope beyond this bounded cursor
  slice.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  adds optional incremental app-diagnostics polling. Existing `poll` payloads
  without `since` keep their previous latest-matching-file behavior.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.8
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.8
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

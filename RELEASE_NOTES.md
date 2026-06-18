# netcoredbg-mcp v0.18.1

Released: 2026-06-19

## Summary

`v0.18.1` is a patch release for the UI emulation and runtime-smoke hardening
milestone. It ships the CR-044 review follow-ups merged after `v0.18.0`, so the
published package and this workstation receive the same read-only validation and
worktree-scope fixes already present on `main`.

## Highlights

- Runtime-smoke `plan_path` validation no longer acquires mux ownership for
  read-only validation calls.
- Read-only validation no longer mutates `session.project_path`, preserving the
  active debug session target while observers inspect plans.
- Worktree lookup caching in project path validation is scoped by the supplied
  project root, avoiding stale decisions across worktrees or plan-file scopes.
- Broad issue-backlog work remains open; this patch only publishes the bounded
  CR-044 follow-up fixes already merged through PR #134, PR #135, and PR #136.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.0` public API surface and
  fixes validation/session-scope behavior around runtime-smoke plan files.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.1
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.1
  netcoredbg-mcp --setup
  ```

- Runtime-smoke and UI helper failures intentionally prefer bounded `BLOCKED`,
  `INVALID_SETUP`, or `FAIL` evidence over false `PASS` results.
- Some broad issue-backlog rows remain open by design; this release ships the
  bounded slices already merged to `main`, not every downstream consumer replay.

## Release Gates

- Release-prep PR must pass MCP PR review before merge.
- Critical suite must pass with `uv run --locked pytest tests/critical -m critical`.
- Runtime-smoke docs/schema gates must pass for the shipped examples.
- Package build and disposable wheel install smoke must pass.
- Production playbook must be executed and recorded before tagging.
- After tag push, GitHub Release, PyPI workflow, remote tag, and local
  workstation deployment must be verified before the release is called shipped.

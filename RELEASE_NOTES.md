# netcoredbg-mcp v0.21.0

Released: 2026-07-01

## Summary

`v0.21.0` is a MINOR feature release that gives runtime-smoke v2 a real
runner-vs-operator input separation, replacing the earlier `GetLastInputInfo`
window heuristic. No-operator runs can now prove that the only input during a
scenario came from the runner itself, instead of guessing "maybe the operator
touched it". The release also carries the function-breakpoint clear rollback
fix (#220, restoring #80).

## Highlights

- **Provenance at the source.** A low-level input event recorder captures every
  keyboard/mouse event with its provenance. Runner injection surfaces (clicks,
  drags, key sequences, modifier holds, literal and special keys) are stamped
  with a shared `RunnerInputSignature`, so a signed event is provably the
  runner's own input.
- **3-way attribution, no ambiguity.** `run_confidence` classifies each event as
  runner-injected (`CLEAN_PROVEN`), foreign-injected, or physical
  (`DIRTY_UNPROVEN`). The previous `RUNNER_GLOBAL_INPUT_AMBIGUOUS` verdict is
  gone — real external input is now separable from emulated input rather than
  suppressed or merely flagged as "maybe".
- **Fail-closed by construction.** An empty-but-present event stream is proven
  `CLEAN` (the recorder was active and nothing happened). A malformed or absent
  stream fails closed as `DIRTY_UNPROVEN` / unproven, so a broken monitor never
  reads as a clean run.
- **Breakpoint clear rollback.** Clearing function breakpoints now rolls back on
  a failed `_sync_function_breakpoints` response instead of reporting a false
  success, restoring the intended error propagation (#220, #80).

## Upgrade Notes

- No-operator plans keep the same public verdict vocabulary — `CLEAN_PROVEN`,
  `DIRTY_UNPROVEN`, and fail-closed `BLOCKED` — but a clean run is now proven by
  event provenance rather than a last-input timestamp window.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.21.0
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.21.0
  netcoredbg-mcp setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design.
- Provenance capture is a Windows desktop-session capability; non-Windows or
  inaccessible desktop sessions remain `BLOCKED`/unproven for this adapter.

## Release Gates

- Release-git-readiness: local `main` clean, synchronized with `origin/main`,
  containing merged PRs `#221` (input-provenance separation) and `#220`
  (breakpoint clear rollback) at `b021037`.
- Version parity prepared across `pyproject.toml`,
  `src/netcoredbg_mcp/__init__.py`, README release copy, changelog, and the
  planned annotated tag `v0.21.0`.
- CR-109 regression suite: `269 passed`; Ruff on changed files: clean;
  `git diff --check`: clean; C# bridge Debug build: `0 warnings, 0 errors`.
- Live smoke gate (`NETCOREDBG_PATH` set, GUI fixtures built): `217 passed,
  10 failed out of 227 checks`. All 10 failures classified PRE-EXISTING by
  running the identical suite on baseline `6d07502` (before CR-109), which
  reproduced the same 10 failures (`216 passed, 10 failed`); zero in-branch
  regressions from CR-109. The failing checks are platform/backend limitations
  (UIA `ControlType`/`RangeValuePattern` unsupported, Avalonia DataGrid,
  virtualized-list realize, console codepage on the emoji clipboard round-trip,
  drag wall-clock timing under load).
- Test discovery: `1851 tests collected`.

# netcoredbg-mcp v0.20.2

Released: 2026-06-22

## Summary

`v0.20.2` is a PATCH release for the next CAP-UI operator-safety layer after
`v0.20.1`. Runtime-smoke v2 can now request `run_confidence.no_operator`
evidence alongside `input_policy.no_global_input`, allowing scenario runs to
distinguish clean product verdicts from operator-contaminated or unproven
execution windows.

This is a confidence and evidence layer. It does not claim a full isolated
desktop, VM runner, or background app-dispatch implementation.

## Highlights

- Runtime-smoke v2 accepts and validates `run_confidence.no_operator`.
- Scenario transitions call the `runtime.input_monitor.check` adapter before
  and after the action window when no-operator confidence is requested.
- Dirty monitor evidence returns terminal `BLOCKED` with `DIRTY_UNPROVEN`
  evidence and restart guidance instead of product `FAIL`.
- Missing, malformed, or unknown confidence monitor statuses fail closed as
  `UNPROVEN` / `BLOCKED` instead of falling through to `PASS`.
- Clean monitor evidence preserves normal product `PASS` / `FAIL` verdicts.

## Upgrade Notes

- This is an additive PATCH release. Existing runtime-smoke v2 plans behave as
  before unless they opt in to `run_confidence.no_operator`.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.2
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.2
  netcoredbg-mcp --setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This release ships bounded CAP-UI-002 confidence evidence;
  it does not claim full broad FR closure.
- `run_confidence.no_operator` can classify dirty or unproven execution
  windows, but it does not itself isolate the operating system keyboard,
  pointer, foreground window, or desktop session.
- A full background or isolated UI scenario runner remains a separate roadmap
  capability that needs fresh RED-first product evidence.

## Release Gates

- Release-git-readiness passed before release-prep: local `main`, `origin/main`,
  and `HEAD` all resolved to `92257d8`; worktree inventory contained only the
  root checkout; remote tag `v0.20.2` did not exist.
- Test discovery reports `1811 tests collected`.
- Version parity passed for `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`,
  `uv.lock`, README release copy, changelog, and release notes; `uv lock
  --check` resolved successfully.
- CLI version smoke from the release worktree passed:
  `uv run --locked --extra dev python -m netcoredbg_mcp --version` ->
  `0.20.2`.
- Critical suite passed:
  `uv run --locked --extra dev pytest tests/critical -m critical` ->
  `14 passed`.
- Runtime-smoke docs/schema gate passed:
  `uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py -q`
  -> `44 passed`.
- CR-107 adjacent release confidence gate passed:
  `uv run --locked --extra dev pytest ... -q` -> `424 passed`.
- Changed-file Ruff passed for `src/netcoredbg_mcp/__init__.py`.
- `git diff --check` returned no whitespace errors; Git reported only the
  checkout's existing LF-to-CRLF working-copy warning.
- Package build passed with `uv build`; rebuilt artifacts are
  `dist/netcoredbg_mcp-0.20.2.tar.gz` and
  `dist/netcoredbg_mcp-0.20.2-py3-none-any.whl`.
- Disposable wheel smoke passed from
  `dist/netcoredbg_mcp-0.20.2-py3-none-any.whl`:
  `netcoredbg-mcp --version` -> `netcoredbg-mcp 0.20.2`; import smoke ->
  `0.20.2`.
- Production playbook provider-side gates passed on this workstation:
  fixture builds for `SmokeTestApp`, `WpfSmokeApp`, and `AvaloniaSmokeApp`;
  `53` manual smoke scenarios listed; WPF one-call runtime smoke `4/4`;
  Avalonia compatibility `4/4`; WPF DataGrid drag/drop customer-mode gate with
  visible-row/offscreen/edge-scroll/multi-row `PASS` and negative no-op
  `BLOCKED` with `next_step`.
- CR-107 post-merge evidence before release prep:
  - focused/adjacent/docs gate -> `424 passed in 19.70s`;
  - changed-file Ruff -> `All checks passed!`;
  - `git diff --check HEAD~1..HEAD` -> clean.
- Release-prep PR #212 passed MCP PR review with `0` unresolved comments and
  merged as `2c06333`. The annotated `v0.20.2` tag, GitHub Release, PyPI
  publish workflow, PyPI install smoke, and local workstation deployment were
  all verified before this release was marked shipped.

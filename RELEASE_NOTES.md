# netcoredbg-mcp v0.20.1

Released: 2026-06-22

## Summary

`v0.20.1` is a PATCH release for the first operator-isolated runtime-smoke
policy layer. It publishes CR-105 / PR #208 after `v0.20.0`: runtime-smoke v2
plans can request `input_policy.no_global_input`, actions are classified by
input route, and physical/global-input actions fail closed before mutating the
operator's active desktop.

## Highlights

- Runtime-smoke v2 accepts and validates `input_policy.no_global_input`.
- Action output now records `input_policy`, `input_classification`,
  `physical_fallback_attempted`, `operator_isolated`, `required_capability`,
  and `requested_target`.
- Physical/global-input routes return `BLOCKED` before focus, keyboard, mouse,
  drag, cursor, clipboard, or physical fallback adapter calls when
  `no_global_input` is active.
- Safe app-dispatch/UIA invoke routes, including built-in `ui.click`, remain
  available under the policy.

## Upgrade Notes

- This is an additive PATCH release. Existing runtime-smoke v2 plans behave as
  before unless they opt in to `input_policy.no_global_input`.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.1
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.1
  netcoredbg-mcp --setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This release ships bounded CAP-UI-001 policy evidence; it
  does not claim full broad FR closure.
- `no_global_input` prevents known runtime-smoke v2 physical/global fallbacks
  from mutating the operator desktop. It does not provide a full isolated
  desktop, VM runner, or app-specific dispatch hook system.
- New app-dispatch hooks or isolated-runner behavior require fresh product
  evidence and a separate RED-first CR.

## Release Gates

- Release-git-readiness passed on `main@9efd4e9`: local `main` was synchronized
  with `origin/main`; stale CR-105 and v0.20.0 release worktree/branches were
  preserved under backup refs before cleanup.
- Test discovery reports `1800 tests collected`.
- Version parity passed for `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`,
  `uv.lock`, README release copy, changelog, and release notes; `uv lock
  --check` resolved successfully.
- Critical suite passed through the lock-safe route:
  `uv run --no-sync --locked --extra dev pytest tests/critical -m critical`
  -> `14 passed`.
- Runtime-smoke docs/schema gate passed:
  `uv run --no-sync --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py -q`
  -> `44 passed`.
- CR-105 adjacent action/schema gate passed:
  `uv run --no-sync --locked pytest tests/test_runtime_smoke_v2_actions.py tests/test_runtime_smoke_schema.py -q`
  -> `151 passed`.
- Changed-file Ruff passed for `src/netcoredbg_mcp/__init__.py`.
- Package build passed with `uv build`; rebuilt artifacts are
  `dist/netcoredbg_mcp-0.20.1.tar.gz` and
  `dist/netcoredbg_mcp-0.20.1-py3-none-any.whl`.
- Disposable wheel smoke passed from
  `dist/netcoredbg_mcp-0.20.1-py3-none-any.whl`:
  `netcoredbg-mcp --version` -> `netcoredbg-mcp 0.20.1`; import smoke ->
  `0.20.1`.
- Production playbook provider-side gates passed on this workstation:
  CLI version smoke, fixture builds, `53` manual smoke scenarios listed, WPF
  one-call runtime smoke `4/4`, Avalonia compatibility `4/4`, and WPF DataGrid
  drag/drop customer-mode gate with visible-row/offscreen/edge-scroll/multi-row
  `PASS` and negative no-op `BLOCKED` with `next_step`.
- CR-105 post-merge evidence before release prep:
  - focused CR-105 tests -> `8 passed in 0.79s`;
  - adjacent action/schema tests -> `151 passed in 12.16s`;
  - changed-file Ruff -> `All checks passed!`;
  - `git diff --check HEAD~1..HEAD` -> clean.
- Release-prep PR must still pass MCP PR review before merge. After tag push,
  remote tag, GitHub Release, PyPI workflow, and local workstation deployment
  must still be verified before the release is called shipped.

# netcoredbg-mcp v0.18.0

Released: 2026-06-19

## Summary

`v0.18.0` is the UI emulation and runtime-smoke hardening milestone. It turns a
large backlog of consumer-facing debugging failures into bounded, reproducible
evidence surfaces for agents: semantic UI helpers, stronger FlaUI bridge
behavior, richer runtime-smoke probes, tracepoint guardrails, and release
playbooks that future agents can execute instead of guessing.

## Highlights

- Added semantic UI evidence helpers for text, focus, properties, DataGrid
  state, monitor events, screenshots, clicks, selected rows, and ensure-visible
  workflows.
- Added runtime-smoke v2 facades for plan validation, plan execution, diagnostic
  probes, wait/event cursors, debug preflight, tracepoint policies, app
  diagnostics, trace cursor deltas, agent-mode defaults, and plan-file inputs.
- Hardened FlaUI bridge behavior around stale bridge sessions, pointer routing,
  selection compatibility, screenshot orientation, transient focus exceptions,
  and target-process focus boundaries.
- Added issue-reproduction ledgers and NovaScript replay packets so broad UI
  issues are tracked as bounded slices rather than closed from partial evidence.
- Added this release protocol so PyPI/GitHub publication, local deploy smoke,
  critical-suite, production playbook, and version-parity gates are explicit.

## Upgrade Notes

- This is a MINOR release because it adds public MCP/runtime-smoke/UI automation
  capabilities while preserving existing package and CLI entry points.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.0
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.0
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

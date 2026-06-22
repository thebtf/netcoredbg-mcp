# netcoredbg-mcp v0.20.4

Released: 2026-06-22

## Summary

`v0.20.4` is a PATCH release for the CAP-UI no-operator confidence roadmap.
Runtime-smoke v2 now ships a default Windows `runtime.input_monitor.check`
adapter, so plans that request `run_confidence.no_operator` can prove clean
operator windows on supported desktop sessions instead of stopping at a missing
adapter boundary.

The adapter uses current desktop-session input evidence, catches input before
and during action windows, and fails closed when the monitor cannot prove a clean
window.

## Highlights

- Adds the default `runtime.input_monitor.check` operation adapter for Windows
  runtime-smoke v2 plans.
- Reports `CLEAN_PROVEN` for stable no-operator windows backed by
  `windows.GetLastInputInfo` evidence.
- Maps adapter-level `DIRTY` evidence to returned
  `run_confidence.classification == "DIRTY_UNPROVEN"` when current
  desktop-session input advances between or during monitored windows.
- Reports `BLOCKED` for unsupported platforms, malformed monitor calls, missing
  baselines, or non-monotonic tick evidence.
- Updates README/README.ru release headlines to `v0.20.4` and documents the new
  input-monitor capability.

## Upgrade Notes

- This is an additive PATCH runtime-smoke release. Existing plans that do not
  request `run_confidence.no_operator` keep their existing behavior.
- `runtime.input_monitor.check` is Windows desktop-session evidence, not full
  OS input isolation. Adapter-level dirty input is returned to callers as
  `DIRTY_UNPROVEN` confidence evidence so the caller can restart the scenario
  instead of recording a product failure.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.4
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.4
  netcoredbg-mcp setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This release does not claim full broad FR closure.
- `runtime.input_monitor.check` does not isolate keyboard, pointer, foreground
  window, or desktop focus. It proves or disproves clean operator windows using
  current desktop-session input evidence.
- Non-Windows or inaccessible desktop sessions remain `BLOCKED`/unproven for
  this adapter.

## Release Gates

- Release-git-readiness passed before release-prep: local `main` was clean,
  synchronized with `origin/main`, and contained one post-`v0.20.3` commit:
  `5b0c111 feat(runtime-smoke): add input monitor adapter`.
- Test discovery reports `1822 tests collected`.
- Version parity is expected across `pyproject.toml`,
  `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog,
  release notes, and annotated tag `v0.20.4`.
- Local release-prep gates passed:
  - critical suite: `14 passed`;
  - runtime-smoke docs/schema/v2 critical gate: `44 passed`;
  - focused input-monitor gate: `10 passed`;
  - package build: `dist/netcoredbg_mcp-0.20.4.tar.gz` and
    `dist/netcoredbg_mcp-0.20.4-py3-none-any.whl`;
  - wheel install smoke: disposable venv CLI reported `netcoredbg-mcp 0.20.4`
    and imported `RuntimeInputMonitor`;
  - production playbook applicability: fixture builds passed after the
    sandbox-only `obj/apphost.exe` access-denied retry, manual smoke inventory
    listed 53 scenarios, and installed-wheel input monitor live-read evidence
    reported `windows.GetLastInputInfo` for the current desktop session.
- Publication gates completed after release-prep: release PR review/merge,
  annotated tag `v0.20.4`, GitHub Release publication, successful publish
  workflow for tag `v0.20.4`, and local workstation deploy smoke reporting
  `netcoredbg-mcp 0.20.4`.

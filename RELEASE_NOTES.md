# netcoredbg-mcp v0.20.5

Released: 2026-06-22

## Summary

`v0.20.5` is a docs-only PATCH release that republishes the corrected package
documentation after `v0.20.4`. The runtime behavior is unchanged from
`v0.20.4`; this release makes the PyPI README, release notes, production
playbook, and NovaScript consumer example match the shipped no-operator
input-monitor surface.

Use this version when you want package metadata and install-time documentation
to describe the current `runtime.input_monitor.check` capability accurately.

## Highlights

- Updates README/README.ru release headlines to `v0.20.5`.
- Publishes the review-hardened no-operator wording to package consumers:
  adapter-level `DIRTY` evidence is returned as
  `run_confidence.classification == "DIRTY_UNPROVEN"`.
- Refreshes the production testing playbook and NovaScript action-oracle
  app-diagnostics example for the current package release.
- Keeps the `v0.20.4` runtime feature history intact while moving the docs
  refresh from `Unreleased` into a published patch release.

## Upgrade Notes

- This release does not change runtime behavior relative to `v0.20.4`.
- Existing plans that rely on `runtime.input_monitor.check` keep the same
  `CLEAN_PROVEN`, `DIRTY_UNPROVEN`, and fail-closed `BLOCKED` semantics.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.5
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.5
  netcoredbg-mcp setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This docs-only release does not claim full broad FR closure.
- `runtime.input_monitor.check` is still desktop-session evidence, not full OS
  input isolation.
- Non-Windows or inaccessible desktop sessions remain `BLOCKED`/unproven for
  this adapter.

## Release Gates

- Release-git-readiness passed before release-prep: local `main` was clean,
  synchronized with `origin/main`, and contained the merged docs-redoc PR
  `#217` at `3c848467fe37ca89efbe497589efac218c05f074`.
- Version parity was prepared across `pyproject.toml`,
  `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog,
  release notes, and the planned annotated tag `v0.20.5`.
- Local release-prep gates passed:
  - runtime-smoke docs/schema/reproduction gate: `94 passed`;
  - critical suite: `14 passed`;
  - server smoke: `6 passed`;
  - Ruff check for the docs oracle: clean;
  - test discovery: `1822 tests collected`;
  - package build: `dist/netcoredbg_mcp-0.20.5.tar.gz` and
    `dist/netcoredbg_mcp-0.20.5-py3-none-any.whl`;
  - wheel install smoke: disposable venv CLI reported
    `netcoredbg-mcp 0.20.5`, package import reported `0.20.5`, and installed
    metadata reported `0.20.5`;
  - production playbook fixture builds passed for `SmokeTestApp`,
    `WpfSmokeApp`, and `AvaloniaSmokeApp` after cleaning stale generated
    `obj` build artifacts; manual smoke inventory listed `53` scenarios.
- Gate environment note: the live MCP development upstream held
  `.venv\Scripts\netcoredbg-mcp.exe`, so release-prep tests used the existing
  `.venv\Scripts\python.exe` with repo-local temp directories, while the package
  smoke used a fresh disposable wheel environment.

# netcoredbg-mcp v0.20.0

Released: 2026-06-22

## Summary

`v0.20.0` is a MINOR feature release for runtime-smoke named pack evidence. It
publishes the post-`v0.19.0` CR-100 through CR-104 work: tracked NovaScript
downstream replay evidence, broad-issue lifecycle/no-repeat guards, and the
new named `oracle_pack` / `app_diagnostics` pack-manifest lifecycle.

## Highlights

- Named `oracle_pack` and `app_diagnostics` runs now expose bounded
  `pack_manifest` descriptors through run-plan, run-probe, evidence-bundle, and
  event-delta facades.
- Final evidence can materialize a readable `pack-manifest.json` with source
  classifications, cleanup/freshness/redaction/limits rollups, and safe
  artifact refs.
- The NovaScript action-oracle app-diagnostics replay is now tracked as
  downstream `PASS` with the adapted `ui.grid.select` plan, while broad
  `#268..#272` issue tails remain explicitly open by design.
- Release-readiness cleanup preserved stale CR branch heads under backup refs
  before removing local release worktree residue.
- Source distributions now exclude local agent scratch, virtual environments,
  build outputs, and fixture `bin` / `obj` residue; the release-critical suite
  includes a guard for that packaging boundary.

## Upgrade Notes

- This is a MINOR feature release. The new manifest fields are additive and
  existing compact runtime-smoke output remains available for current consumers.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.0
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.0
  netcoredbg-mcp --setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This release ships bounded provider-side slices and recorded
  lifecycle evidence; it does not claim full broad FR closure.
- A future `oracle_pack` file-source manifest-materialization CR is not opened
  by this release. Current behavior allows `materialized=false` unless fresh
  consumer evidence proves a readable persisted manifest is required for that
  path.
- Some UI automation behavior remains Windows GUI and backend dependent.
  Release claims are scoped to documented runtime-smoke gates and post-merge
  verification evidence.

## Release Gates

- Release-git-readiness passed on `main@618e1c3`: local `main` is synchronized
  with `origin/main`, stale CR branches were preserved under backup refs, and
  the stale CR-104 worktree was removed.
- Version parity passed for `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`,
  `uv.lock`, README release copy, changelog, and release notes.
- Test discovery reports `1792 tests collected`.
- Critical suite passed:
  `uv run --locked --extra dev pytest tests/critical -m critical`
  -> `14 passed`.
- Runtime-smoke docs/schema gate passed:
  `uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py`
  -> `44 passed`.
- CR-104 runtime-smoke named-pack slice passed:
  `uv run --locked pytest tests/test_runtime_smoke_evidence_manifest.py tests/test_runtime_smoke_diagnostics_schema.py tests/test_runtime_smoke_v2_probes/test_oracle_pack.py tests/test_runtime_smoke_v2_probes/test_app_diagnostics.py tests/test_runtime_smoke_run_plan_facade.py tests/test_runtime_smoke_run_probe_facade.py tests/test_runtime_smoke_event_delta_facade.py tests/test_reproduction_scenarios_docs.py -q`
  -> `227 passed`.
- Package build passed with `uv build`; rebuilt artifacts are
  `dist/netcoredbg_mcp-0.20.0.tar.gz` and
  `dist/netcoredbg_mcp-0.20.0-py3-none-any.whl`. The rebuilt sdist has 343
  entries, is about 2.0 MB, and contains no `.agent*`, `.venv`, `dist`,
  fixture `bin`, or fixture `obj` residue.
- Disposable wheel smoke passed from
  `dist/netcoredbg_mcp-0.20.0-py3-none-any.whl`:
  `netcoredbg-mcp --version` -> `netcoredbg-mcp 0.20.0`; import smoke ->
  `0.20.0`.
- Production playbook provider-side gates passed on this workstation:
  CLI version smoke, MCP surface registration, launch metadata safety, fixture
  builds, `53` manual smoke scenarios listed, WPF one-call runtime smoke
  `4/4`, Avalonia compatibility `4/4`, and WPF DataGrid drag/drop customer-mode
  gate with visible-row/offscreen/edge-scroll/multi-row `PASS` and negative
  no-op `BLOCKED` with `next_step`.
- NovaScript live consumer execution was not re-run from this provider
  workspace. This release carries the recorded CR-104 replay `PASS` and the
  provider contract/docs/example gates; the downstream live consumer validation
  remains owned by the separate NovaScript Engram request.
- Release-prep PR must still pass MCP PR review before merge.
- After tag push, GitHub Release, PyPI workflow, remote tag, and local
  workstation deployment must still be verified before the release is called
  shipped.

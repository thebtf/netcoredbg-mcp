# netcoredbg-mcp v0.23.0

Released: 2026-07-14

## Summary

`v0.23.0` is a MINOR feature release focused on safe observation before action.
Agents can inspect launch compatibility without mutating the shared debugger,
move the real pointer over one exact UI target without clicking, and sample
adapter-owned debuggee activity without pausing or probing the OS process.
Runtime-smoke focus, target-realization, diagnostic-alias, and output-checkpoint
paths are also more reliable on slowly materializing Windows UI surfaces.

## Highlights

- **Read-only launch compatibility preflight.**
  `inspect_debug_launch_compatibility(program)` reports the target runtime,
  active `dbgshim`, cached same-major candidate, compatibility verdict, and
  predicted launch-time mutation. It does not build, launch, claim session
  ownership, create cache state, or replace files. Existing `start_debug`
  behavior remains fail-open when no matching cached shim is available.
- **Selector-scoped pointer hover.** `ui_hover(...)` and runtime-smoke v2
  `ui.hover` require exactly one resolved target in the existing foreground
  debuggee window. Success evidence covers pointer position, hit testing,
  unchanged focus, timeout, and cleanup; zero/multiple matches and
  `no_global_input` fail before pointer mutation.
- **Bounded debuggee activity telemetry.** `debuggee_activity(window_ms)` samples
  continued/stopped/step, output, module, and trace deltas already owned by the
  adapter. It never pauses, steps, evaluates, or falls back to Win32
  `Process.Responding`. Executed-instruction counts are explicitly unavailable
  because the current NetCoreDbg/DAP contract exposes no such counter.
- **Runtime-smoke reliability.** The implicit `default` output checkpoint is
  anchored at `isolated_profile.launch`; `diagnostic-latest.json` resolves to the
  newest snapshot; target realization receives bounded retries; and
  `key_sequence` can recover from a pre-find miss through its focus path.

## Upgrade Notes

All new APIs are additive; there is no intentional breaking change.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.0
# or
pipx upgrade netcoredbg-mcp
```

Install on a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.0
netcoredbg-mcp setup
```

## Known Residual Scope

- Engram #356 remains open for downstream Tier-3 / CR-112 liveness
  classification. This release ships Tier-2 adapter-owned activity evidence;
  it does not fabricate an executed-instruction counter.
- Real pointer hover requires Windows, the FlaUI bridge, an interactive desktop,
  and one already-foreground exact target. Unsupported backends and ambiguous
  selectors return bounded `BLOCKED` evidence.
- The full pytest run emits one existing `RuntimeWarning` from
  `tests/test_build_cleanup.py` about a mocked `create_subprocess_exec`
  coroutine; it does not fail the suite or affect the released runtime path.

## Release Gates

- Integration base: merged PRs `#223`, `#224`, and `#225` on
  `main@a1994d6`.
- Full suite: `2023 passed, 3 skipped`.
- Critical suite: `14 passed`.
- Runtime-smoke docs/schema/critical suite: `45 passed` when run serially; GUI
  suites are intentionally not run concurrently because they share one desktop,
  foreground window, and pointer.
- Ruff: all checks passed.
- FlaUI bridge: Debug and Release builds passed.
- Package build: `netcoredbg_mcp-0.23.0.tar.gz` and
  `netcoredbg_mcp-0.23.0-py3-none-any.whl` built successfully.
- Disposable wheel install: both `netcoredbg-mcp --version` and
  `python -m netcoredbg_mcp --version` reported `0.23.0`.
- Merged-source live capability evidence:
  - launch preflight returned `verdict=compatible` and
    `mutationPerformed=false`;
  - WPF/FlaUI hover resolved one exact target with unchanged focus, no click,
    and zero cleanup residue;
  - a 1000 ms activity sample observed adapter output/module deltas while
    reporting instruction counts unavailable.

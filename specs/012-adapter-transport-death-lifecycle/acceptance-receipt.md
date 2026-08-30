# Wave 1 acceptance receipt: adapter transport-death lifecycle

## Receipt identity

- **Outcome:** `ACCEPTED_INTERNAL_WAVE`
- **Release intent:** `none`
- **Program:** `specs/011-issue450-sonar-release-program/`
- **Wave contract:** `specs/012-adapter-transport-death-lifecycle/`
- **Source base:** `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`
- **Accepted implementation candidate:** `099ff2abad5037c82b7f85506f76b5afa8dac578`
- **Final judgment:** `agent://JudgeWave1Final` returned `ACCEPT` with no blocking or nonblocking findings.

This receipt closes Wave 1 only. It does not authorize a tag, package publication, prerelease, public release, Sonar waiver, Wave 2 implementation, or v0.23.11 shipment.

## User-visible outcome

When the adapter transport reaches stdout EOF, reader failure, DAP termination, or process completion, the Python debug route no longer leaves `get_debug_state` at `RUNNING` with `debuggeeAlive=true` while later DAP requests reject the dead adapter.

One generation-bound finalizer now owns pending-request terminalization, bounded process and stream joining, immutable terminal facts, and one manager callback. `SessionManager` owns one current-generation public outcome. An explicit stop retains its existing reset-to-idle path. A stale prior generation cannot mutate a newer session.

The historical reason that netcoredbg closed stdout in issue #450 remains unknown. The implementation retains bounded evidence for the next occurrence without labelling the incident as a crash, foreign kill, or debugger defect.

## Accepted source boundary

The implementation candidate changes these Wave-1 surfaces:

- `src/netcoredbg_mcp/dap/client.py`
- `src/netcoredbg_mcp/session/manager.py`
- `src/netcoredbg_mcp/session/state.py`
- `tests/test_client.py`
- `tests/test_session.py`
- `tests/test_debuggee_liveness.py`
- `tests/test_resource_updates.py`
- `tests/test_stealth_mode.py`, limited to completing an existing `SessionManager.stop()` test double with `stop=AsyncMock()`
- `specs/012-adapter-transport-death-lifecycle/tasks.md`

The candidate does not change `src/netcoredbg_mcp/build/**`, process cleanup policy, Sonar configuration, coverage configuration, `.github/**`, package metadata, the public Python/default selector, or the stateless-preview route.

## Deterministic RED evidence

The first regression used current symbols and a real `DAPClient` plus `SessionManager` seam. It started from `RUNNING` with debuggee PID `29736`, supplied raw stdout EOF without DAP `terminated`, and retained one pending request.

Command:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_client.py::TestDAPClientTransportDeath::test_stdout_eof_publishes_terminal_manager_state
```

Observed before implementation:

- state remained `running`;
- `debuggeeAlive` remained `true`;
- no manager state transition occurred;
- no state/thread resource update occurred; and
- the pending request failed promptly with `netcoredbg process died — pending request cancelled`.

The expanded RED matrix also observed missing stderr/process observers, no terminal callback seam, prior-client terminal delivery mutating a newer session, premature terminal publication during explicit stop, and a raw `BrokenPipeError` when request send raced terminalization.

## GREEN behavioral proof

Final focused command:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_client.py `
  tests/test_session.py `
  tests/test_debuggee_liveness.py `
  tests/test_resource_updates.py `
  tests/test_stealth_mode.py::test_session_manager_stop_cancels_stealth_foreground_restore_task
```

Result: `127 passed`.

The focused proof covers:

- raw EOF from a dead adapter;
- stdout, stderr, and process observers;
- known and unobserved adapter exit outcomes;
- DAP `exited` versus `terminated` semantics;
- DAP termination, EOF, and process-exit ordering;
- reader failure;
- one immutable callback record;
- stale prior-generation isolation;
- explicit-stop reset precedence;
- truthful public liveness and one state/thread publication path;
- pending-request settlement and pipe-error normalization;
- fixed-capacity stderr and event previews;
- direct, JSON, prefixed, nested, and backslash-escaped credential redaction;
- Windows and single-segment POSIX path redaction;
- C0, C1, and Unicode-format control neutralization; and
- hard event sequence, event-name, container-depth, container-item, and final-text bounds.

Adjacent lifecycle command:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_event_coverage.py `
  tests/test_debuggee_activity.py `
  tests/test_terminate.py `
  tests/test_debug_launch_preflight.py `
  tests/test_step_in_targets.py
```

Result: `116 passed`.

## Static proof

The accepted source passed:

```powershell
uv run --locked --extra dev ruff check <Wave-1 Python source and focused tests>
uv run --locked --extra dev mypy `
  src/netcoredbg_mcp/dap/client.py `
  src/netcoredbg_mcp/session/manager.py `
  src/netcoredbg_mcp/session/state.py
python -m compileall -q <Wave-1 Python source and focused tests>
git diff --check
```

Results:

- Ruff: clean for the scoped Wave-1 files.
- mypy: no issues in the three source files.
- compileall: clean.
- diff check: clean.

## Broader-suite evidence

A full project run observed `2413 passed`, `3 skipped`, `1 warning`, and `59 subtests passed`, with three failures:

1. one incomplete `SessionManager.stop()` test double, corrected by adding its missing async `stop` method; and
2. two Windows foreground-sensitive WPF cases, both of which passed together on the bounded focused retry.

The full run is supporting evidence, not a fabricated all-green claim. Wave-1 acceptance is based on the exact focused nonzero-denominator proof and the independent review chain.

## Independent review and correction chain

- `agent://Wave1FinalExactChecker`: full TD-001 through TD-010 PASS on predecessor candidate `127b9dff9989ea2636e0921fbb4adb0283f44b51`.
- `agent://Wave1FinalRaceCheck`: found one error-surface nit where a send/finalization race could leak `BrokenPipeError`; the candidate now normalizes that race to the terminal request error and has a focused regression.
- `agent://Wave1FinalSecurityCheck`: found credential/control and metadata-bound bypasses; the candidate added structured sanitization, prefixed/JSON credential handling, C0/C1/format neutralization, path redaction, and hard bounds.
- `agent://Wave1PythonAdversarial`: found the single-segment POSIX path bypass; `/root` and `/tmp` are now covered and redacted.
- `agent://Wave1NestedRedactionCheck`: found an already-escaped nested JSON credential bypass; structured recursive sanitization and escaped-quote redaction now cover the exact payload.
- `agent://Wave1ExactNestedRecheck`: PASS for the changed nested-redaction criterion at `099ff2abad5037c82b7f85506f76b5afa8dac578`.
- `agent://JudgeWave1Final`: terminal `ACCEPT`, with T013 authorized to close and this receipt authorized to be created.

Earlier passes remain historical evidence for their exact predecessor bytes. The exact successor recheck closes only the source fields changed after those passes, as required by the exact-head evidence-recheck contract.

## Requirement closure

- **TD-001:** closed by the retained behavioral RED and final direct EOF GREEN.
- **TD-002:** closed by one frozen bounded terminal record per generation.
- **TD-003:** closed by independent stdout, stderr, and process observers.
- **TD-004:** closed by no-await one-finalizer election and terminal-ordering tests.
- **TD-005:** closed by bounded, sanitized known-or-unknown diagnostics.
- **TD-006:** closed by one current-generation manager state/resource outcome.
- **TD-007:** closed by separate debuggee, protocol, adapter-process, and transport facts.
- **TD-008:** closed by exact scoped diff review and unchanged-route boundaries.
- **TD-009:** closed by detailed domain, ownership, race, and public-projection documentation.
- **TD-010:** closed by focused proof, independent review, exact successor recheck, and terminal judgment.

## Handoff

Wave 1 is closed internally. The next parent transition is the separately scoped SpecKit child:

```text
architect specs/013-owner-scoped-prebuild-cleanup --of specs/011-issue450-sonar-release-program
```

Wave 2 must not claim that global image-name cleanup caused the historical issue #450 incidents. It owns the independently proven foreign-owner cleanup risk only.

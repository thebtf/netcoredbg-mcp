# Implementation Plan: Adapter Transport-Death Lifecycle

**Branch:** `work/issue450-eof-sonar-remediation`  
**Date:** 2026-08-30  
**Spec:** [spec.md](spec.md)  
**Source base:** `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`  
**Parent:** `specs/011-issue450-sonar-release-program/`, Wave 1 internal verification only.

## Summary

Repair the source-proven lifecycle gap in which `DAPClient._read_loop` treats adapter stdout EOF as terminal for DAP work but never informs `SessionManager`. The implementation will add one bounded terminal-facts snapshot, three client-owned observers, one idempotent finalization owner, and one manager-owned terminal transition. The public `get_debug_state` result will no longer claim a running, live debuggee after adapter transport death.

The work does not identify or fix the historical adapter producer failure. It records facts needed to diagnose a later producer failure. It does not touch the global pre-build cleanup defect, Sonar remediation, coverage, workflows, release files, Python/default routing, or stateless-preview selection.

## D1 boundary contract

### Inputs

The client receives terminal observations from the existing adapter process and its streams:

- a DAP `terminated` protocol event;
- stdout EOF or a reader failure;
- an observed adapter process completion;
- explicit `DAPClient.stop()`;
- independently drained adapter stderr; and
- normal DAP events, including `exited`.

### Output

For one adapter run, the client publishes one immutable terminal snapshot to one manager-owned callback. For unrequested transport death, the manager consumes that snapshot once, transitions an active session through the existing terminal state path, wakes waiters, and uses the existing state/thread resource-notification mechanism once. During an explicit manager stop, the callback records the same terminal facts while the established manager stop/reset path remains the sole public state/resource outcome.

The public result after unrequested transport death is a truthful terminal session state. It may retain a historical debuggee PID and a safe terminal diagnostic summary, but it must not report that PID as a live debuggee after finalization. After an explicit manager stop completes, the existing reset-to-idle result remains the public outcome.

### Attaches to

| Existing file | Planned responsibility |
|---|---|
| `src/netcoredbg_mcp/dap/client.py` | Own the terminal snapshot, stdout/stderr/process observers, the one guarded finalizer, bounded cleanup, and terminal callback dispatch. |
| `src/netcoredbg_mcp/session/manager.py` | Register exactly one terminal callback and own one terminal state/resource mutation for unrequested transport death or the existing one reset path for an explicit manager stop. |
| `src/netcoredbg_mcp/session/state.py` | Hold and serialize a safe immutable terminal summary with the existing state. |
| `src/netcoredbg_mcp/tools/debug.py` | Remain the unchanged public `get_debug_state` projection path. |
| `tests/test_client.py` | Own fake-subprocess RED, observer, diagnostics, and terminal-race coverage. |
| `tests/test_session.py` | Own manager callback and DAP event-semantics coverage. |
| `tests/test_debuggee_liveness.py` | Own truthful `debuggeeAlive` behavior after terminal transport loss. |
| `tests/test_resource_updates.py` | Own one logical state/thread notification-path coverage. |

### Does not touch

The following are explicit non-integration boundaries:

- `src/netcoredbg_mcp/build/**`, including global/image-name cleanup and any owner-scoped process work;
- `scripts/run_sonarqube_exact_head.py`, `SonarQube.Analysis.xml`, `pyproject.toml`, `uv.lock`, coverage tools, thresholds, baseline, exclusions, or policy;
- `.github/**`, `docs/RELEASE-PROTOCOL.md`, tags, packages, changelog, and release processes;
- public Python package/entrypoint/default selection and the stateless-preview route; and
- a fix or asserted cause for the historical adapter producer failure.

## Technical context

| Concern | Decision |
|---|---|
| Runtime | Existing Python async DAP client and session manager. No new runtime, transport, or route is introduced. |
| Process lifecycle | The client already creates the adapter with `stdin`, `stdout`, and `stderr` pipes. It will observe all three lifecycle sources rather than treating stdout as the only signal. |
| Public state | Reuse `DebugState.TERMINATED` as a terminal debug-session state. Preserve a distinction between session terminality and physical debuggee exit. |
| Diagnostics | Use bounded in-memory terminal facts. The terminal snapshot is not a raw stream log or persistent incident store. |
| Concurrency | One guarded finalization task per client run serializes cleanup, snapshot creation, pending-request failure, and manager callback. |
| Testing | Start with a deterministic fake asyncio subprocess RED. Add focused Python client, manager, liveness, and resource-update tests before and alongside implementation. |
| Review | One independent reviewer re-derives the D1 requirements against the exact atomic candidate commit after focused proof exists. |

## Current-source grounding

The plan is bound to the exact source base and investigation record, not to a guessed producer cause.

- `src/netcoredbg_mcp/dap/client.py:165-179` starts the process with stdout and stderr pipes but only starts `_read_loop`.
- `src/netcoredbg_mcp/dap/client.py:270-325` treats a falsey stdout line as EOF, faults pending requests, and best-effort terminates the process without process-wait, stderr-drain, terminal snapshot, or manager callback.
- `src/netcoredbg_mcp/session/manager.py:847-869` registers DAP event handlers only. It has no transport-terminal registration.
- `src/netcoredbg_mcp/session/manager.py:1043-1059` transitions on DAP `terminated`, while `exited` only retains a debuggee exit code.
- `src/netcoredbg_mcp/session/state.py:534-554` derives `debuggeeAlive` from stored PID plus nonterminal state.
- `src/netcoredbg_mcp/tools/debug.py:545-565` returns that serialization through the public read-only `get_debug_state` tool.
- `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md` supplies the causal chain and requires the direct fake-subprocess RED before source changes.

## Chosen shape

### Frozen arena synthesis

The selected base is the client-owned `_DapRun` capsule. This corrects the arena's swapped proposal labels. `SessionManager` issues and binds the branded generation before awaiting `DAPClient.start()`. `DAPClient` receives that generation, constructs one `_DapRun`, and returns the same identity after startup. This ordering removes callback-before-binding ambiguity without moving process, stream, pending-request, or cleanup ownership out of the client.

The selected base carries four grafts: pre-start manager issuance with identity equality, active/stopping generation comparison in the manager, named callback dispositions for unrequested, stop, stale, and duplicate facts, and projection-before-state-transition ordering. It rejects a manager-owned subprocess finalizer, an async terminal queue, derived manager lifecycle phases, a PID-plus-generation return record, and an explicit-stop transport cause. Stop remains manager policy when the callback is consumed.

### Terminal fact shape

The implementation will use one frozen internal value, provisionally named `DapTransportTerminal`. The name identifies the boundary, not a public API. Its field names and private helper names remain implementation details, but the value must carry these semantic groups:

| Semantic group | Required content | Why it exists |
|---|---|---|
| First terminal trigger | One of DAP protocol termination, stdout EOF, reader failure, or adapter process completion. | Explains which observed transport signal won finalization without overwriting later observed facts. Explicit manager stop is policy, not a transport trigger. |
| Adapter-run identity and process fact | One manager-issued generation, adapter PID, whether process completion was observed, and return code only when observed. | Prevents a later client run or a debuggee DAP fact from being merged into the wrong terminal record. |
| DAP facts | Whether DAP `terminated` was observed; the last event sequence/name; safe bounded event-body summary; optional DAP `exited` debuggee exit code. | Preserves protocol semantics and event ordering. |
| Stream facts | Stdout EOF state; bounded reader-error category/detail when applicable; bounded stderr tail plus truncation/unknown state. | Captures transport evidence without unbounded retention. |
| Finalization fact | Whether bounded observation/cleanup completed or timed out. | Makes cleanup boundaries observable without claiming an unobserved cause or embedding manager shutdown policy in a transport record. |

The value becomes immutable only after the finalizer has given process and stderr observers their bounded opportunity to add facts. A later signal after publication cannot mutate the snapshot, trigger a second callback, or start another cleanup path. A snapshot is scoped to one manager-issued adapter-run generation.

### Observer and finalizer ownership

1. Before awaiting `DAPClient.start()`, `SessionManager` issues and binds a new adapter-run generation and installs the one terminal sink. `DAPClient.start(generation=...)` constructs one `_DapRun` with that identity, starts three independent tasks for it, and returns the same identity without an intervening manager await.
2. The stdout reader records the last parsed DAP event before invoking its existing handlers. A DAP `terminated` event requests finalization. A DAP `exited` event remains a nonterminal DAP fact.
3. The stderr observer drains chunks into a fixed-capacity tail. It never publishes a state transition by itself.
4. The process waiter records only adapter process completion and return code, then requests finalization.
5. EOF, reader fault, DAP `terminated`, process completion, and explicit client stop all request one guard-protected finalizer for their captured `_DapRun`. The election has no `await` between phase check, first-trigger assignment, and finalizer-task assignment. Further requests can contribute facts until the snapshot boundary but cannot start duplicate cleanup.
6. The finalizer faults unsettled requests once, gives natural process completion and stderr drain a documented bounded chance, performs the existing controlled terminate/kill escalation only when still required, captures the frozen snapshot, and invokes the manager callback once.
7. `SessionManager` accepts only a snapshot whose generation matches its active client run. For unrequested matching-generation transport death, it owns the one terminal state transition, execution-waiter wake, and state/thread resource publication. The manager, not the snapshot, decides whether a matching callback is an explicit stop by comparing its stopping generation at callback consumption. That callback records terminal facts but does not create a preliminary terminal transition. The existing reset-to-idle path is the one state/resource outcome. A callback consumed with no matching stop is unrequested. Direct DAP `terminated` handling migrates into the callback path so a protocol event cannot bypass the finalizer.

### Semantic separation

| Fact | Meaning | Must not mean |
|---|---|---|
| DAP `exited` | The debuggee exited and supplied a DAP exit code. | The adapter process exited; DAP debugging terminated; a stdout EOF cause. |
| DAP `terminated` | Debugging of the debuggee terminated. | The debuggee physically exited; an adapter return code is known. |
| Adapter process completion | The adapter process waiter observed completion and possibly a return code. | The DAP debuggee exit code. |
| Stdout EOF or reader fault | The DAP transport cannot continue. | A crash, a DAP `exited` event, or a proven reason for the stream closure. |
| Session `TERMINATED` state | The manager cannot continue the DAP session. | A fresh OS liveness result for a retained debuggee PID. |

## Alternatives and D1 challenge-LITE

**Round 1 verdict: REVISE.** The premise was accepted: raw stdout EOF makes the DAP transport unusable, so pending requests must terminalize and the manager cannot keep publishing `RUNNING`/`debuggeeAlive=true`. The challenger required three refinements before a final verdict: bind terminal facts to one client-run generation, distinguish requested shutdown from observed transport death, and make the first RED assert the desired manager/public-notification outcome rather than merely describe stale current behavior.

**Round 2 verdict: REVISE.** The challenger confirmed the defect and generation-fenced callback boundary, then required one scope correction: keep the three user-required observers, bounded diagnostics, and one finalizer, but leave planned-versus-unrequested shutdown classification in current manager state rather than freezing it inside the transport snapshot.

**Applied final correction and verdict: GO.** The record now carries only run, trigger, process, protocol, stream, and bounded-finalization facts. At callback consumption, the generation-fenced manager decides whether its current stop operation owns the existing reset path or an unrequested transport death needs one terminal transition. The chosen guarded client finalizer plus one manager callback is the smallest shape that meets TD-001 through TD-010 without guessing the producer cause or opening route/build/Sonar scope.

| Alternative | Disposition | Reason |
|---|---|---|
| Let every observer call `SessionManager` directly. | Rejected. | Concurrent EOF, process, protocol, and stop signals would duplicate state transitions and cleanup ownership. |
| Make a manager-owned coordinator consume raw client observer events. | Rejected for this D1 child. | It moves subprocess timing and stream-drain details into the manager, enlarges the existing boundary, and does not reduce the number of required facts. |
| Add a public OS liveness probe to `get_debug_state`. | Rejected. | It masks the missing lifecycle transition, races PID reuse, and does not make the adapter client usable. |
| Use a guarded client finalizer and one manager callback. | Selected. | It directly closes the source-proven seam, retains bounded diagnostics, preserves current route/state infrastructure, and keeps explicit manager reset behavior intact. |

The LITE challenge considered only premise and alternatives. It did not create an ADR, a tracer-bullet map, a milestone map, or a broader program decomposition because those belong to the parent D3 program.

## Integration and race test plan

| Test category | Existing files | Required observable result |
|---|---|---|
| Deterministic RED | `tests/test_client.py` plus the existing `SessionManager` seam | A fake process returns `b""` from stdout while manager state is `RUNNING` with a debuggee PID. Current source fails because state remains live and the manager/resource callback is absent. The repaired source publishes one terminal state/resource outcome and faults pending work. |
| Client unit and diagnostics | `tests/test_client.py` | Three observers start; known and unknown adapter exit facts remain distinct; bounded stderr and last-event data survive; no unbounded buffer or inferred cause appears. |
| Manager integration | `tests/test_session.py` | Callback registration occurs once. Unrequested transport terminalization replaces direct DAP-terminal state mutation, wakes waiters, and makes one manager terminal state mutation. An explicit manager stop records the same fact without a preliminary terminal mutation. |
| Public liveness | `tests/test_debuggee_liveness.py` | A historical PID may remain diagnostic data, but `debuggeeAlive` is not true after unrequested transport terminalization. |
| Resource behavior | `tests/test_resource_updates.py` | One unrequested logical transition enters the existing state/thread resource path once. An explicit stop uses only its existing reset path. Duplicate terminal signals do not create another logical outcome. |
| Race matrix | `tests/test_client.py`, with manager assertions in the existing session/resource tests | DAP `terminated` → EOF → process exit; process exit → EOF; EOF while process remains live briefly; and reader fault each produce one terminal callback/transition. Explicit stop racing EOF/process exit produces one owner and the existing single reset path, not a terminal-plus-reset pair. |
| DAP semantic separation | `tests/test_session.py`, `tests/test_client.py` | `exited` without `terminated` retains the DAP exit code; `terminated` does not fabricate a debuggee exit; adapter return code remains adapter-only. |

Focused test commands and a behavioral walkthrough are documented in [quickstart.md](quickstart.md). They are implementation-phase commands; this planning task does not execute them.

## Requirements-to-files map

| Requirement | Source files | Test files | Observable acceptance |
|---|---|---|---|
| TD-001 | `src/netcoredbg_mcp/dap/client.py`; `src/netcoredbg_mcp/session/manager.py`; `src/netcoredbg_mcp/session/state.py` | `tests/test_client.py` | The first added fake-process test fails against current code and later proves terminal public state rather than stale `RUNNING`. |
| TD-002 | `src/netcoredbg_mcp/dap/client.py` | `tests/test_client.py` | Exactly one frozen bounded snapshot exists for one process run, with no mutable post-publication facts. |
| TD-003 | `src/netcoredbg_mcp/dap/client.py` | `tests/test_client.py` | Stdout, stderr, and process completion have separate observers and distinct fact ownership. |
| TD-004 | `src/netcoredbg_mcp/dap/client.py` | `tests/test_client.py` | Every terminal ordering converges to one finalizer, one pending-request terminalization, one cleanup decision, and one callback. |
| TD-005 | `src/netcoredbg_mcp/dap/client.py`; `src/netcoredbg_mcp/session/state.py` | `tests/test_client.py`; `tests/test_session.py` | PID, known-or-unknown adapter exit, bounded stderr, last event, and bounded reader error are retained without unbounded data or causality claims. |
| TD-006 | `src/netcoredbg_mcp/session/manager.py`; `src/netcoredbg_mcp/session/state.py`; unchanged projection `src/netcoredbg_mcp/tools/debug.py` | `tests/test_session.py`; `tests/test_debuggee_liveness.py`; `tests/test_resource_updates.py` | One callback causes one terminal state/resource path for unrequested transport death and prevents a false live claim. During explicit manager stop, the callback records facts and the existing reset-to-idle remains the one public outcome. |
| TD-007 | `src/netcoredbg_mcp/dap/client.py`; `src/netcoredbg_mcp/session/manager.py` | `tests/test_client.py`; `tests/test_session.py` | Debuggee exit, DAP termination, adapter exit, and EOF remain distinct in storage and behavior. |
| TD-008 | Explicit unchanged comparison surfaces: `src/netcoredbg_mcp/build/**`, `scripts/run_sonarqube_exact_head.py`, `SonarQube.Analysis.xml`, `.github/**`, `pyproject.toml`, public route files | Focused tests above plus exact candidate diff review | No forbidden surface is changed. No suppression, exclusion, threshold, baseline, or route change exists. |
| TD-009 | `src/netcoredbg_mcp/dap/client.py`; `src/netcoredbg_mcp/session/manager.py`; `src/netcoredbg_mcp/session/state.py`; this packet | Focused source-review checklist | Docstrings and comments explain the one owner, bounded joining, state handoff, and semantic distinctions. |
| TD-010 | All listed child source/test files; later `specs/012-adapter-transport-death-lifecycle/acceptance-receipt.md` | Focused client/manager/liveness/resource test group | Nonzero focused evidence, one atomic candidate commit, and one independent review precede the receipt. |

## Execution sequence

1. Add the deterministic fake-subprocess RED first. Preserve its failing observation against the source base before implementing the fix.
2. Add the remaining focused diagnostics, semantic, resource, and race RED tests in the existing test files.
3. Implement the client terminal facts, observer lifetime, and guarded finalizer in `dap/client.py`.
4. Bind the one manager callback, terminal state projection, execution-waiter wake, and existing resource path in `session/manager.py` and `session/state.py`.
5. Add source docstrings and ownership comments that explain why terminal ownership cannot be split across observers.
6. Run only the focused nonzero-denominator test group and the documented behavioral walkthrough. Do not broaden to a formatter, linter, build, or project-wide suite as part of this child task.
7. Inspect the exact scoped candidate diff. Create one atomic candidate commit containing the completed Wave 1 source, focused tests, and documentation updates.
8. Obtain one independent code review against that exact candidate commit. The reviewer re-derives TD-001 through TD-010, confirms the source/test boundaries, and reports no unresolved blocking defect.
9. Only after steps 1–8 are evidenced, create and commit `specs/012-adapter-transport-death-lifecycle/acceptance-receipt.md` bound to the exact candidate SHA. The receipt must cite the RED-to-GREEN result, integration/race/public behavior proof, bounded diagnostics proof, independent review, and unchanged-route comparison.

## One-checker commitment

One independent reviewer, who did not author the Wave 1 implementation, will review the exact atomic candidate commit after focused proof. The reviewer must re-derive the terminal ownership and DAP semantic separation from `dap/client.py`, `session/manager.py`, and `session/state.py`, then compare them to the focused test evidence and TD-001 through TD-010. This is the sole D1 independent checker. No acceptance receipt may claim this check before it occurs.

## Completion boundary

A passing focused test run alone is not Wave 1 closure. The child becomes ready for its delayed receipt only when its exact candidate commit, focused behavioral evidence, and one independent review agree. That receipt is an internal Wave 1 acceptance artifact, not a release authorization; the parent D3 program alone schedules Wave 2 and the one v0.23.11 public ship moment.

# Feature Specification: Adapter Transport-Death Lifecycle

**Feature branch:** `work/issue450-eof-sonar-remediation`  
**Created:** 2026-08-30  
**Status:** Implemented and internally accepted. `acceptance-receipt.md` records the candidate, focused proof, and review. This packet does not authorize a tag or release.
**Source base:** `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`  
**Engram issue:** #450  
**Parent:** `specs/011-issue450-sonar-release-program/`, Wave 1 of the one-public-release v0.23.11 program.  
**Parent anchors:** PRG-002 transport-death correctness; PRG-007 public Python/default-route preservation; PRG-010 GitHub-first research and documentation quality.  
**Root-cause record:** `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md`.

## Child boundary

This D1 child corrects one observed contradiction: an adapter transport can reach EOF or process death while `get_debug_state` still reports a running, live debuggee and later DAP requests reject the dead adapter.

The child owns the legacy Python DAP client's transport-to-session lifecycle bridge. It adds bounded evidence for the next producer failure without claiming why the historical adapter closed stdout. It does not repair a debugger producer, pre-build cleanup, Sonar findings, coverage, build configuration, release mechanics, or route selection.

**Release intent:** none. This is an internal verified Wave 1. It ends in one atomic candidate commit and an exact-head child acceptance receipt only after the required proof and independent review. It does not create a tag, package, prerelease, or public shipping moment. Only Wave 5 may ship v0.23.11.

## Frozen design decision

The selected arena base is the client-owned per-run `_DapRun` capsule. This names the selected content rather than the arena's swapped proposal labels. Before process startup, `SessionManager` issues and binds a branded generation, installs the terminal sink, and passes that exact generation to `DAPClient.start()`. `DAPClient` receives and uses the generation for its `_DapRun`, observers, finalizer, and immutable terminal fact, then returns the same identity. No callback can arrive before the manager has a binding.

The synthesis grafts manager-issued pre-start generation, active/stopping generation comparison, named callback dispositions, and projection-before-state-transition ordering onto the client-owned capsule. It rejects manager-owned subprocess finalization, an async terminal queue, derived manager lifecycle phases, a PID-plus-generation return record, and an explicit-stop transport cause. Explicit manager stop is policy only when the manager consumes a matching callback.

The per-run finalizer election has no `await` between phase check, first-trigger assignment, and sole-finalizer-task assignment. One observer therefore cannot elect a second finalizer while another observer is suspended in election.

## Design depth

**D1 feature justification:** This packet is about one reversible transport/session boundary inside the existing Python debug route. A wrong implementation is user-visible because it can report a false live debuggee, but it neither creates a subsystem nor changes a public route. The packet will be consumed by the Wave 1 implementer and one independent reviewer. The parent D3 program owns release sequencing, global remediation, and all later waves.

## User scenarios and testing

### User Story 1 — Receive truthful terminal state after adapter transport loss (Priority: P1)

A debugger user has launched or attached successfully. If the adapter closes stdout, faults its reader, or exits without sending a DAP `terminated` event, the next public state observation must no longer describe the session as running or the retained debuggee PID as live.

**Why this priority:** Issue #450 observed the opposite state: a visible app and adapter disappeared, `get_debug_state` returned `state=running` and `debuggeeAlive=true`, and `pause_execution` then reported `DAP client not running`.

**Independent test:** A deterministic fake asyncio subprocess starts from manager state `RUNNING` with a debuggee PID, immediately returns stdout EOF, and has no DAP `terminated` event. The test uses existing manager state-listener and state/thread resource seams to assert the desired one terminal state notification, one logical state/thread publication, no public live-debuggee claim, and prompt terminalization of pending DAP work. Against the source base, those desired assertions fail because state remains stale and no manager-visible transition occurs.

**Acceptance scenarios:**

1. **Given** a configured running session with a known debuggee PID, **when** the adapter stdout reader receives EOF without DAP `terminated`, **then** the manager publishes one terminal or unavailable state through one state/thread notification path and `debuggeeAlive` is false or explicitly unavailable.
2. **Given** unsettled DAP requests when transport death begins, **when** the one finalization owner completes, **then** each request completes with one terminal failure rather than waiting for its ordinary request timeout.
3. **Given** an adapter returns after a DAP `terminated` event, **when** stdout EOF and the process waiter subsequently complete, **then** the public state transition, manager callback, and resource notification remain single-shot.

---

### User Story 2 — Diagnose the next adapter failure without inventing its cause (Priority: P1)

A maintainer needs enough bounded lifecycle evidence to distinguish adapter process exit, stdout closure, reader failure, explicit stop, and DAP protocol facts. The evidence must say what was observed and what remained unknown; it must not label a pipe closure as a crash or synthesize a debuggee exit code.

**Why this priority:** The Issue #450 incident retained no adapter exit code, stderr tail, last DAP event, or reliable disappearance order. The current root cause is the missing notification seam, not a proven producer defect.

**Independent test:** A controlled fake process emits a bounded stderr tail and a DAP event before a terminal signal. The terminal snapshot retains the adapter PID, known-or-unknown adapter exit outcome, bounded stderr-tail metadata, and a bounded last-event summary. A malformed-frame or reader-fault path retains a bounded error classification without retaining unbounded raw input.

**Acceptance scenarios:**

1. **Given** a process exit has been observed, **when** terminalization is published, **then** the record distinguishes an observed adapter return code from an unknown return code.
2. **Given** stderr is large, non-line-delimited, or still draining around process exit, **when** the bounded drain closes or its bound expires, **then** the terminal record retains only the configured bounded tail and whether truncation occurred.
3. **Given** a DAP `exited` event arrives before transport loss, **when** the terminal record is published, **then** the debuggee exit code remains a DAP fact and does not become an adapter return code or proof that the DAP session was terminated.

---

### User Story 3 — Keep terminal races and route boundaries contained (Priority: P2)

A maintainer needs concurrent terminal signals to converge without duplicate cleanup, duplicate manager callbacks, stale resource notifications, or a route change. An explicit `stop`, DAP `terminated`, stdout EOF, reader fault, and adapter process exit must use one guarded finalizer for one client-run generation. A terminal fact from an older generation must not mutate a newer manager session. An explicit manager stop uses its existing intentional reset path, not a preliminary terminal publication followed by a second reset.

**Why this priority:** Process, stream, protocol, caller shutdown, and a later session can overlap. Per-observer cleanup or an unfenced callback would recreate the stale-state defect as duplicate or stale state/resource behavior.

**Independent test:** Controlled race tests schedule DAP `terminated`, stdout EOF, process completion, explicit stop, and a late prior-generation terminal record. Each unrequested current-generation case observes one finalizer-owned cleanup attempt, one manager terminal transition, one state/thread notification revision, and no second callback. An explicit-stop race observes one finalizer, one manager-owned reset path, and no extra terminal transition or resource publication before that reset. A prior-generation record produces no mutation of the active newer session.

**Acceptance scenarios:**

1. **Given** DAP `terminated` arrives before adapter process exit, **when** the finalizer waits through its bounded process and stderr observation window, **then** it records `protocol terminated` separately from whether adapter exit was observed.
2. **Given** stdout EOF occurs while the adapter remains live for a short interval, **when** the finalizer reaches its bounded natural-exit wait, **then** only the guarded owner may perform the existing controlled termination escalation and subsequent wait.
3. **Given** explicit stop races with EOF or process exit, **when** the manager is in its active stop operation when it consumes the matching terminal callback, **then** one finalizer owns cleanup and the existing `SessionManager.stop()` reset path is the sole manager state/resource outcome. The transport callback records transport facts and must not create a preliminary terminal transition. If an unrequested transport terminal callback was already consumed before stop begins, a later user stop is a separate deliberate reset operation, not a duplicate of that terminal event.
4. **Given** preview selection is absent and a user follows the existing Python route, **when** this child is implemented, **then** the public Python package, console entrypoint, default selection, and stateless-preview boundaries remain unchanged.

## Edge cases

- A truthy non-DAP stdout line is not EOF. The current parser's `if not header_line` distinction remains intact.
- A malformed header, incomplete content read, or handler-independent reader exception is a reader fault. It is not automatically an adapter crash.
- A DAP `exited` event without `terminated` records the debuggee exit fact but does not, by itself, claim the debugging session or adapter process ended.
- A DAP `terminated` event does not prove that the debuggee exited or that an adapter return code exists.
- Adapter exit can occur before stdout EOF, after stdout EOF, or while stderr still has buffered bytes. The first terminal trigger does not erase later facts gathered within the bounded finalization window.
- A child or inherited handle can keep a pipe open. Stream/process joining is bounded; the record explicitly marks unobserved data rather than waiting indefinitely.
- A second `stop`, repeated observer completion, a late DAP terminal event, or a late reader fault must not issue another kill, callback, terminal state change, or duplicate resource publication. An intentional manager stop may still make its one existing reset-to-idle transition.
- A terminal snapshot from an earlier client-run generation is stale evidence. It must not change a newer manager state, wake newer-session waiters, or publish newer-session resources.
- A terminal record may retain an historical debuggee PID. That PID is not a live-process claim once the session is terminal.

## Functional requirements

- **TD-001 — Deterministic RED:** Before product code changes, add a deterministic fake-subprocess regression in the existing Python DAP test surface. It MUST start from `RUNNING` with a debuggee PID, drive raw stdout EOF without DAP `terminated`, and assert the desired terminal state, one existing manager state notification, and one state/thread resource outcome through existing test seams. Against `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`, that behavioral assertion MUST fail because state remains stale and no manager-visible transition occurs. It MUST not fail because a planned symbol is absent.
- **TD-002 — Immutable terminal fact:** The DAP client MUST create exactly one immutable, bounded terminal snapshot for one adapter-run generation. The implementation may call it `DapTransportTerminal`; its private field spellings are not a wire contract. Its semantic content MUST include the run generation, first terminal trigger, adapter identity, observed-or-unknown adapter exit outcome, DAP protocol facts, bounded last-event data, bounded stderr-tail data, bounded reader-failure data when present, and explicit truncation or unknown markers. The manager's planned-versus-unrequested shutdown classification remains manager state, not a transport-snapshot field.
- **TD-003 — Three observers:** At adapter launch, the client MUST own independent observers for DAP stdout, adapter stderr, and adapter process completion. Stdout parses DAP traffic, stderr contributes only bounded diagnostics, and the process waiter establishes only adapter-process facts.
- **TD-004 — One guarded finalizer:** All terminal triggers—DAP `terminated`, stdout EOF, reader fault, adapter process completion, and explicit stop—MUST request one idempotent finalization owner per adapter-run generation. Only that owner may terminalize unsettled requests, choose the existing bounded cleanup escalation, take the immutable snapshot, and notify the manager. It MUST fence late snapshots so that an earlier generation cannot mutate a newer session.
- **TD-005 — Bounded diagnostics:** The final snapshot MUST retain the adapter PID, an observed adapter return code when available, a bounded stderr tail with truncation state, and a bounded last-DAP-event summary. Diagnostic capture MUST not wait indefinitely, store unbounded output, or claim a historical EOF producer cause.
- **TD-006 — One manager transition and public resource outcome:** The manager MUST consume a current-generation client terminal snapshot through one transport-terminal callback. For unrequested transport death in an active matching generation, it MUST make one existing terminal/unavailable state transition, wake execution waiters, and use the existing state/thread resource-notification path once. When an explicit manager stop is active at matching callback consumption, the callback MUST record terminal facts without creating a preliminary terminal transition; the existing manager reset-to-idle path remains the one state/resource outcome. A stale-generation callback MUST make no state, waiter, or resource mutation. `get_debug_state` MUST not report `RUNNING` with `debuggeeAlive=true` after an unrequested current-generation callback completes.
- **TD-007 — Preserve DAP semantics:** The implementation MUST keep DAP `exited` (debuggee exit plus exit code), DAP `terminated` (debugging session termination), adapter process exit, and stdout transport closure as distinct facts. It MUST NOT fabricate DAP `exited`, infer a debuggee exit from DAP `terminated`, or equate an adapter return code with the DAP debuggee exit code.
- **TD-008 — Preserve scope boundaries:** The child MUST NOT change `src/netcoredbg_mcp/build/**`, cleanup policy, Sonar configuration or findings policy, coverage configuration, `.github/**`, release files, package/default selection, public Python route, or the stateless-preview route. It MUST NOT suppress, exclude, baseline, accept risk for, or weaken any Sonar condition.
- **TD-009 — Explain the contract at both boundaries:** Code changed for this child MUST include concise but detailed docstrings and race/ownership comments where the terminal record, observer lifetime, finalizer ownership, manager callback, state projection, and DAP semantic separation would otherwise be ambiguous. This packet MUST keep public behavior, internal ownership, primary-source rationale, and non-goals documented separately.
- **TD-010 — Focused proof and delayed acceptance:** The child MUST prove the direct fake-process regression, manager integration, public-state/resource behavior, diagnostic bounds, DAP semantic separation, current-generation fencing, stop-versus-EOF precedence, and terminal race matrix with focused nonzero-denominator tests. Only after those tests pass, one atomic candidate commit exists, and one independent review covers that exact commit may `specs/012-adapter-transport-death-lifecycle/acceptance-receipt.md` be created and committed with the exact wave SHA. This planning packet is not that receipt.

## Success criteria

- **TD-SC-001:** The TD-001 fake-subprocess scenario fails before the implementation because its desired terminal-state, manager-notification, and state/thread-publication assertions observe no transition. It passes after implementation, with final public state not `running` and no claim that the retained debuggee PID is live.
- **TD-SC-002:** Every tested ordering of DAP `terminated`, stdout EOF or reader failure, and process completion invokes one finalization owner and one matching-generation manager terminal callback/transition. An explicit-stop race consumes the same one finalizer through exactly one existing manager reset path when the manager stop is active at callback consumption. A stale earlier-generation record does not mutate a newer session.
- **TD-SC-003:** The terminal snapshot's diagnostic buffers never exceed their configured bounds. Tests cover a non-line-delimited stderr tail, a large stderr stream, a known adapter return code, and an unknown adapter return code.
- **TD-SC-004:** The exited-without-terminated scenario preserves the DAP exit code and finalizes after actual transport death without fabricating `protocol terminated` or an adapter exit cause.
- **TD-SC-005:** Focused public-state and resource-notification tests observe the existing terminal serialization and exactly one logical state/thread publication path after unrequested transport death. A separate explicit-stop race test observes only the existing reset-to-idle path.
- **TD-SC-006:** The candidate diff contains only the DAP client, session manager/state, their focused existing test files, and their documentation updates. It contains no build-cleanup, Sonar, coverage, workflow, release, package, Python-default-route, or stateless-preview change.
- **TD-SC-007:** The delayed acceptance receipt, if created, binds the exact atomic candidate SHA, the deterministic RED-to-GREEN evidence, focused race and public-behavior evidence, the one independent review result, and the unchanged-route comparison. It cannot be written before that evidence exists.

## Integration points

| Existing boundary | Required child action | Must remain unchanged |
|---|---|---|
| `src/netcoredbg_mcp/dap/client.py` | Own terminal facts, stdout/stderr/process observers, request failure, and one finalizer. | DAP parsing, request wire format, public Python route selection, and ordinary nonterminal event dispatch. |
| `src/netcoredbg_mcp/session/manager.py` | Register one transport-terminal callback and make the one manager-owned state/resource transition. | Session launch/attach behavior except callback registration, build/pre-build behavior, and unrelated event handlers. |
| `src/netcoredbg_mcp/session/state.py` | Serialize a safe bounded terminal projection alongside existing state so a historical PID cannot imply liveness after finalization. | Existing state enum values and all unrelated state fields. |
| `src/netcoredbg_mcp/tools/debug.py` | Remain the read-only `get_debug_state` public projection owner and serve as an unchanged comparison surface. | Tool name, route selection, access control, and response envelope behavior. |
| `tests/test_client.py` | Add the first direct fake-subprocess RED and client observer/finalizer/diagnostic/race tests. | Existing request, event, and command tests. |
| `tests/test_session.py`, `tests/test_debuggee_liveness.py`, `tests/test_resource_updates.py` | Add manager callback, semantic separation, truthful liveness, and single resource-publication integration tests. | Existing event, liveness, and subscription contracts. |

## Assumptions and non-goals

- The source-proven root cause is the missing DAPClient-to-SessionManager transport-death notification. The cause of the historical adapter stdout closure remains unknown.
- Existing `DebugState.TERMINATED` is the narrow destination for a terminal debug session. It means the session can no longer service DAP requests; it is not proof that a debuggee physically exited.
- The child reuses the existing Python default route. No native/stateless route becomes selected, deprecated, or removed.
- The adjacent global `taskkill /IM netcoredbg.exe` defect remains Wave 2. Nothing in this child attributes the two #450 incidents to that path.
- No Sonar scan, coverage measurement, build, test execution, formatter, commit, review, acceptance receipt, tag, or release is performed by authoring this packet.

# Plan — Typed native DAP step-over

## Architecture


### Current source-only execution and cancellation revision — 2026-10-19

Root accepted normative route B from `agent://DecideB3ExecutableContract`, replacing—not satisfying—the unpublished physical-first-byte retention promise. Primary `.agent/runs/b3-native-step-over/contract-b-20261019/preservation.json` binds verbatim original contracts, assertions, `artifact://6732`, and the existing forward-port WIP on `work/b3-native-step-over-current` at base `a462e1d6964d51ef884b9351744672ecce9ae6e4`. No old worktree replacement or reset is authorized.

Chronology is explicit: root's original public RED was 35 behavioral failures plus six passing scratch cases (`current-red-receipt.json`, `artifact://6627`) before production forward-port. Production already exists for this revision; stronger causal/deadline/cancellation tests and contract reconciliation are post-hoc additions, not a new full test-first history. Original zero-byte RED 6732 stays failed and superseded. Root owns fresh exact-candidate verification, independent review, acceptance, and commits; `release_intent:none` remains. Historical planning/G0 wording below is not a claim that implementation or this revised acceptance has passed.

B3 adds one permanent inspect→act→observe vertical slice to the existing stateful native route. It neither generalizes execution control nor imports Python behavior. The public boundary is one closed call, `step_over(debugSessionId, threadId)`. The private DAP operation is only `next({threadId})`; its acknowledgement is not the final user outcome. The final observation is an exact fresh `stopped(reason:"step", threadId)` for the same supplied Int32, a typed terminal, a typed timeout, or a redacted typed failure.

```mermaid
sequenceDiagram
  participant C as Modern MCP client
  participant R as DebugSessionRegistry
  participant L as SessionSlot lease
  participant S as NetCoreDbgSession
  participant G as _callStackAdmissionGate + _stateGate
  participant D as controlled adapter / netcoredbg

  C->>R: step_over(debugSessionId, threadId)
  R->>R: validate exact object, resolve opaque token
  R->>L: TryAcquire()
  L->>S: StepOverAsync(explicit Int32)
  S->>G: require stopped authority for target
  G->>G: reserve one active B3 record; clear stopped/B2 frame authority
  alt record occupied or authority absent
    G-->>S: typed refusal; zero DAP I/O
  else admitted
    G->>D: correlated next({threadId})
    G-->>S: release admission gate after bytes commit
    D-->>S: exact next acknowledgement
    alt later exact stopped(step, target)
      D-->>S: stopped(reason=step, threadId=target)
      S->>G: update lifecycle; credit acknowledged record
      S-->>R: step_over_success
    else exited or terminated
      D-->>S: terminal event
      S-->>R: step_over_terminal
    else deadline or caller cancellation after write
      S->>L: release lease; join close/drain
      S-->>R: timeout or propagated cancellation
    else malformed active exchange
      S->>L: release lease; join close/drain
      S-->>R: dap_step_over_protocol_error
    end
  end
  R-->>C: complete normalized result
```

### Ownership and boundaries

| Owner | Responsibility | Explicit non-responsibility |
| --- | --- | --- |
| `Program.DebugSessionRegistry` | Catalog order, closed MCP input parsing, opaque handle lookup, one `SessionSlot` lease, result serialization, and result-to-drain decision | DAP event interpretation, stopped authority, active-record association, or a generic execution registry |
| `SessionSlot` | Lease admission, abort token, exact token removal, shared close/drain/stop/dispose | Retaining observations, selecting a thread, translating DAP responses, or serializing B3 execution |
| `DebugAdapter.NetCoreDbgSession` | One internal `next`, request correlation, one private active B3 record, DAP reader-order lifecycle processing, stopped-thread authority, and 30-second post-write deadline | Public cache, generic DAP surface, Python fallback, release ownership |
| Existing `_callStackAdmissionGate` | Serializes B2 capture and B3 authority invalidation/reserve/register/write with reader lifecycle transitions | Waiting for remote completion or acting as a public lock contract |
| Existing `_stateGate` | Protects snapshot state, stopped/frame authority, and private B3 active-record visibility | I/O and public MCP serialization |
| Controlled DAP adapter and native tests | Deterministic response/event order, single-flight, malformed paths, timeout/cancellation, and race evidence | Production execution policy |

`NetCoreDbgSession` has the existing DAP framing/correlation seam: `BeginRequestAsync`, `SendRequestAsync`, `WaitForResponseAsync`, reader `HandleResponse`, and lifecycle handlers. B2 already uses `_callStackAdmissionGate` to serialize stopped/continued/terminal transitions against typed request capture and write, while `_stateGate` protects the snapshot and call-stack eligibility. B3 reuses those owners and adds only its session-private nullable active record; it adds neither a parallel lifecycle subsystem nor a new `SessionSlot` admission mode.

### Admission, authority, observation, and invalidation

1. The registry validates the closed object before touching a session. A valid opaque handle acquires exactly one `SessionSlot` lease and links caller cancellation to its existing abort token.
2. While holding the established admission gate, `StepOverAsync` takes state protection, verifies capability-independent authority for the explicit target, then reserves the one nullable active B3 record. A present record returns typed refusal before request registration or DAP I/O. An admitted record clears every stopped and B2 call-stack/frame-authority token, registers pending `next`, and commits exactly `{threadId}`. The operation has no `singleThread`, `granularity`, or ambient selector.
3. A stopped event grants authority independently of the delayed-stack capability: `allThreadsStopped:true` creates one all-target epoch; otherwise an explicit `threadId` creates only that target; a missing target without all-stop creates none. Existing `continued`/terminal lifecycle processing invalidates it. B2 uses that authority plus `supportsDelayedStackTraceLoading`; B3 uses authority alone. `next` implies continuation, so admission invalidates authority before the write even when the adapter omits `continued`.
4. On successful write, the operation releases admission only after complete header/payload/flush commit. Exactly that commit starts the one absolute 30-second acknowledgement/observation deadline; admission and the earlier possible-write marker do not. The record tracks target, exact acknowledgement, possible-write/full-commit states, deadline, and completion. A positive correlated acknowledgement is necessary but insufficient: only a later exact `stopped(reason:"step",threadId:target)` succeeds. Other stops update ordinary lifecycle but never settle B3 or extend its deadline; all-stop is not an exact-target substitute.
5. `exited` and `terminated` settle the active operation before or after acknowledgement. Negative acknowledgement retains the session. Cancellation that finishes before writer handoff, without initiated/late I/O, clears pending/active records and retains the same token/slot/adapter without restoring stopped/B2 authority. The possible-write marker stays before `WriteAsync`: opaque writer entry or possible, partial, or unknown transmission requires release-before-drain and only then cancellation propagation, even when a fixture observed zero underlying bytes. This deliberately permits conservative drain in the last cancellation-check-to-writer-entry window; it does not promise physical-first-byte atomicity. Post-commit timeout and protocol errors use the same existing `SessionSlot.CloseAndDrainAsync` owner. No raw DAP fields escape.

### Result contract and numeric bounds

| Result | Error | Closed fields |
| --- | --- | --- |
| `step_over_success` | false | `kind`, `threadId` |
| `step_over_terminal` | false | `kind`, `terminal` (`exited` or `terminated`), optional Int32 `exitCode` only if existing state recorded it |
| `step_over_timed_out` | false | `kind`, `threadId`, `timeoutSeconds` fixed at `30` |
| `dap_step_over_refused` | true | `kind`, `error:"DAP_STEP_OVER_REFUSED"` |
| `dap_step_over_protocol_error` | true | `kind`, `error:"DAP_STEP_OVER_PROTOCOL_ERROR"` |

Existing `invalid_tool_arguments` and `DEBUG_SESSION_NOT_FOUND` remain their established complete native results. Every application result has `resultType:"complete"`, normalized `structuredContent`, and one identical JSON text item. The input `threadId` and any recorded terminal `exitCode` are Int32. `timeoutSeconds` is the fixed integer `30`. B3 has no response collection, payload pagination, or new serialized-content size limit; it reuses the existing result serialization boundary and does not emit raw adapter data.

### DAP evidence and uncertainty boundary

Vendored `NextArguments` requires an Int32 `threadId`; `singleThread` and `granularity` are optional. The vendor specification says response precedes a `stopped(reason:"step")` event, but does not declare that `StoppedEvent.threadId` equals the next request target and allows other threads to run. The B3 exact observer equality is therefore a B3 requirement and controlled-fixture proof, not a DAP causality claim.

The existing native handler records stopped/continued/exited/terminated state and B2 call-stack eligibility. B3 must preserve that source-of-truth state update for every event, then credit only the active, positively acknowledged record whose later event names the exact target. This avoids hiding debugger state while preventing a foreign/stale stop from passing as successful control flow.

## Migration and compatibility

This is a parallel additive change. The native catalog gains `step_over` after `get_call_stack`; the frozen modern front-door schema gains only its closed input and result variants. No live data, public selector, consumer default, package, or existing tool requires migration. The retained installed Python package remains selected default/rollback and is replayed only as a non-selection journey. Rollback is simply not selecting the additive native route.

Python is a contrast, not an implementation dependency. Python `step_over` calls `execute_and_wait`; its manager resolves `thread_id or current_thread_id`, and pause may fall back to first enumerated thread. Its preparation replaces an asyncio event and waits for stopped, exited, terminated, or timeout. B3 deliberately imports none of this: it has no optional thread, no mutable ambient state selection, no first-thread scan, no Python retry or error envelope, and no automatic source/stack inspection.

## Requirements-to-files

| Requirement | Tickets | Future implementation/test owners; this packet does not modify them |
| --- | --- | --- |
| B3-REQ-001/002/006 | B3-T02, B3-T03 | `host/NetCoreDbg.Mcp.Stateless/Program.cs`; `host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StepOverContractTests.cs` (new); `ModernProtocolContractTests.cs`; `StructuredContentSchemaParityTests.cs`; `specs/001-mcp-stateless-strangler/contracts/modern-front-door.schema.json` |
| B3-REQ-003/004/005/007 | B3-T01, B3-T02, B3-T03 | `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs`; existing B2 `_callStackAdmissionGate`, `_stateGate`, `PendingRequest`, `HandleResponse`, `HandleStoppedEvent`, `HandleContinuedEvent`, and `HandleTerminalEvent` seams; no public operation registry |
| B3-REQ-003/004/005/007 | B3-T02, B3-T03 | `host/NetCoreDbg.Mcp.Stateless.Tests/DebugAdapter/NetCoreDbgSessionContractDriver.cs`; `DebugAdapter/NetCoreDbgSessionTests.cs`; `ModernMcp/CapabilityLifecycleContractTests.cs`; `ModernMcp/SessionSlotContractTests.cs`; `ModernMcp/SessionSlotLifecycleTests.cs` |
| B3-REQ-002/004/005/006/007 | B3-T01, B3-T02, B3-T03 | `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs`; `ModernMcp/ModernMcpProcessDriver.cs`; fixture configuration passed through `CONTROLLED_DAP_OPTIONS` |
| B3-REQ-002/004/005 | B3-T01, B3-T02 | `docs/dap-protocol/specification.md`; `docs/dap-protocol/debugAdapterProtocol.json`; no vendored document modification is planned |
| B3-REQ-008 | B3-T05 | `specs/006-a1-local-preview/quickstart.md` installed-Python journey evidence only; no Python, selector, package, release, or publication file changes |
| B3-REQ-009 | B3-G0, B3-T01, B3-T04, B3-T05 | `.agent/runs/b3-native-step-over/{g0-challenger.md,protocol-seam.md,security-review.md,receipt.yaml}`; installed S2 security contract; focused native test-project receipts |

## Bounded ticket graph

`B3-G0 FULL challenger -> B3-T01 Explore -> B3-T02 RED Test -> B3-T03 Code -> B3-T04 Review -> B3-T05 Test`.

The graph is intentionally one tracer bullet: source evidence establishes `next` and the current seams; RED tests make a closed end-to-end contract fail; one implementation ticket adds only typed native step-over; review attacks its lifecycle/trust boundary; final tests prove the exact candidate and retained Python non-selection. Every edge is blocking because subsequent work would otherwise reason from an unadmitted contract or unproven observable behavior.

## FULL challenger

The original planning `REVISE` is historical. The current-base protocol receipt records a later prospective `GO` and root's original public RED before production forward-port. The following questions preserve the G0 scope; the accepted cancellation revision does not reopen that completed pre-code event or backdate later tests. Independent exact-candidate code/S2 review still follows the frozen revised candidate.

| Challenge question | Required disposition before code |
| --- | --- |
| 0A premise | Confirm a one-operation typed `next` slice is needed to advance the native inspect route without prematurely adding an execution-control suite. |
| 0B existing-code leverage | Verify reuse of `DebugSessionRegistry`, `SessionSlot`, B2 admission/state gates, pending correlation, stopped-authority lifecycle seams, controlled fixture, and schema parity harness. |
| 0C alternatives | Compare typed `next`, first-non-exact-event rejection, live-session preservation after uncertain acts, `continue` first, generic forwarding, and Python reuse against the bounded observation requirement. |
| 0D scope | Confirm one private per-session single-flight record, capability-independent stopped authority, and close/drain after uncertain post-write actions are prerequisites—not generic execution, queue, selector, UI, Python, or release work. |
| 9-point FULL review | Record staleness, false dependencies, complexity, value, scope creep, assumptions, bias, and S2 closed-input/lifecycle trust-boundary review; issue `GO`, `REVISE`, or `RETHINK`. |

## Milestone map

| Milestone | Tickets | Value statement | Binding constraints | Closure / shipping moment |
| --- | --- | --- | --- | --- |
| M1 — bounded native step-over | B3-G0 through B3-T05 | An opt-in native client can inspect a stopped explicit thread but cannot advance it; one single-flight typed `next` acknowledgement-to-later-exact-stop loop fixes that without exposing a generic execution API. | Preserve opaque token and SessionSlot ownership; explicit Int32 target; one typed request; capability-independent stopped authority; B2 authority invalid before act; later exact reason/thread observation; one 30-second post-write deadline; uncertain-act drain; Python selected rollback; `release_intent:none`. | After a fresh terminal FULL challenger disposition, RED→GREEN evidence, independent S2 review, focused current-candidate nonzero-denominator tests, and retained Python non-selection proof, the controlled native journey is independently demoable. The change is technically releasable through the repository protocol but is intentionally not authorized for release. |

## Focused validation plan

Root runs only focused checks after source work and the separately owned shared TESTONLY layout patch are integrated. The patch binds absolute validated child-only `NETCOREDBG_MCP_TEST_SOURCE_ROOT` to this candidate and `NETCOREDBG_MCP_TEST_ARTIFACTS_PATH` / `NETCOREDBG_MCP_TEST_SCRATCH_ROOT` to primary checkout `.agent`. Do not build into worktrees/source-bin or inner `.agent`. For this candidate set source to `D:/Dev/netcoredbg-mcp/.agent/worktrees/b3-native-step-over-current-20261016`, artifacts to `D:/Dev/netcoredbg-mcp/.agent/artifacts/build/b3-contract-b-20261019`, and scratch to `D:/Dev/netcoredbg-mcp/.agent/tmp/b3-contract-b-20261019`; set `DOTNET_ADD_GLOBAL_TOOLS_TO_PATH=false` only in the child. The pre-integration freeze is not a ready-to-run output-binding claim.

1. `dotnet test D:/Dev/netcoredbg-mcp/.agent/worktrees/b3-native-step-over-current-20261016/host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj --artifacts-path D:/Dev/netcoredbg-mcp/.agent/artifacts/build/b3-contract-b-20261019 --filter "FullyQualifiedName~StepOverContractTests|FullyQualifiedName~ModernProtocolContractTests|FullyQualifiedName~GetThreadsContractTests|FullyQualifiedName~GetCallStackContractTests|FullyQualifiedName~NetCoreDbgSessionTests|FullyQualifiedName~CapabilityLifecycleContractTests|FullyQualifiedName~SessionSlotContractTests|FullyQualifiedName~SessionSlotLifecycleTests|FullyQualifiedName~StructuredContentSchemaParityTests" --logger "console;verbosity=normal" --logger "trx;LogFileName=b3-t05.trx" --results-directory D:/Dev/netcoredbg-mcp/.agent/runs/b3-native-step-over/contract-b-20261019/t05`; record a nonzero denominator for each named group in primary `.agent/runs/b3-native-step-over/receipt.yaml`.
2. Controlled MCP scenario: all-stop explicit target -> `step_over` -> verify one `next({threadId})` -> acknowledgement -> later exact same-thread `stopped(reason:"step")` -> normalized success -> state remains queryable -> stop.
3. Controlled negative scenarios: invalid/no-token no-I/O; unavailable token/no-I/O; absent-authority/concurrent/negative-ack refusal; B2 invalidation; capability independence; ignored pre-ack/unrelated/missing/different stops then exact success or bounded drain; terminals; one 30-second deadline starting after header/payload/flush, not the early marker. Pre-writer cancellation must retain identical token/slot/adapter, clear pending/active records, restore no authority, emit no late `next` after barrier release, and complete a separate test-owned correlated registry `get_threads` before cleanup. Writer-entry zero-byte, prefix-header, opaque-OCE, final-token-check/entry, full-header cancellation, post-commit cancellation/timeout, and protocol errors must release their lease before completed drain and later not-found.
4. Retained installed-Python `PRODUCT_WORKS` non-selection journey from `specs/006-a1-local-preview/quickstart.md`.

No validation command is executed by this planning packet.

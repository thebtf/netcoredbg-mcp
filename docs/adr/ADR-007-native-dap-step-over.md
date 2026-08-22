# ADR-007: Add one typed native DAP step-over operation with correlated acknowledgement and bounded fresh-stop observation

## Status

Proposed

## Context

B1 introduced the native stateful walking skeleton: an opaque `debugSessionId`, registry-owned `SessionSlot` lease and cleanup lifecycle, one `NetCoreDbgSession` per live handle, correlated DAP I/O, and normalized complete MCP results. B2 added explicit-thread `get_call_stack` and capability-gated stack paging over private stopped-state/frame-eligibility authority. B2 provenance is binding but not current-candidate test evidence: B2 merged to `main` at `eb84c55`. This B3 packet has `release_intent: none`.

An opt-in native client can identify a stopped thread and inspect its stack, but cannot advance that stopped thread in the native route. The bounded first execution-control slice is DAP `next`, exposed only as `step_over(debugSessionId, threadId)`.

The vendored DAP specification is the protocol authority. `docs/dap-protocol/specification.md` §§1824–1870 states that `next` executes one step for the specified thread, allows other threads to run freely, returns an acknowledgement with no required body, and then sends `stopped` with `reason:"step"`. `docs/dap-protocol/debugAdapterProtocol.json` definitions `NextRequest`, `NextArguments`, and `NextResponse` (lines 1745–1785) make `threadId` a required Int32 and identify `singleThread` and `granularity` as optional. The optional `singleThread` behavior is conditional on `supportsSingleThreadExecutionRequests`; B3 has no verified capability contract for it. The schema’s `StoppedEvent` has an optional Int32 `threadId` and says that missing or false `allThreadsStopped` makes only its named thread expandable (lines 189–241). A `continued` event may be omitted when a request itself implies execution continuation; if present, omitted or true `allThreadsContinued` means all threads resumed (lines 243–270).

The DAP text promises response-before-step-stop ordering, but it does not state that the subsequent `StoppedEvent.threadId` must equal the `NextArguments.threadId`. Therefore a syntactically valid stopped event is not sufficient evidence that this caller’s step completed. B3 must test and require exact equality at the native operation boundary; a missing or different stopped `threadId`, or a reason other than `step`, is never credited to the request.

This artifact is D2: an incorrect decision would require coordinated rework across the public MCP dispatcher/schema, `DebugSessionRegistry`, `SessionSlot` lifetime, `NetCoreDbgSession` reader and pending-request logic, B2 frame authority, the controlled DAP adapter, and native process/contract tests. It will be consumed across sessions by future execution-control slices.

## Decision

1. Add one additive modern-native public tool: `step_over(debugSessionId, threadId)`. The input object is closed and accepts exactly the two required fields. `debugSessionId` is a non-whitespace opaque string under the existing native lookup policy; `threadId` is an Int32. Missing arguments, missing fields, empty or whitespace token, a non-string token, a non-integral or out-of-range number, or any extra field returns `invalid_tool_arguments` before DAP I/O. An unavailable non-whitespace token returns the existing `DEBUG_SESSION_NOT_FOUND` result without probing token grammar or scanning sessions.
2. `step_over` issues only the internal typed DAP request `next` with exactly `{ threadId }`. It sends neither `singleThread` nor `granularity`, and introduces no generic DAP request forwarding, capability registry, ambient thread selection, current-frame selection, breakpoint, pause, continue, or other step operation.
3. Reuse the existing session’s shared `_callStackAdmissionGate` and `_stateGate`; do not create a second lifecycle authority. While admission is held, B3 first requires capability-independent current stopped-thread authority for the supplied target and then reserves the one nullable active B3 record for that `NetCoreDbgSession`. The record is fail-fast single-flight: a concurrent `step_over` sees it occupied and returns the typed refusal with zero DAP I/O. The admitted record contains only private record identity, explicit target, exact-acknowledgement state, committed-write state, deadline, and completion signals; it is neither a public cache, registry, selector, nor a generic execution framework. Admission clears all stopped and B2 frame authority, registers the correlated `next` pending request, and writes it before releasing admission.
4. Stopped-thread authority is event-owned and capability-independent. A `stopped` event with `allThreadsStopped:true` establishes an all-target epoch; otherwise an explicit Int32 `threadId` establishes only that target; a stop without either grants none. Existing continued and terminal transitions invalidate that authority under the established lifecycle serialization. B2 stack paging continues to require both this authority and `supportsDelayedStackTraceLoading`; B3 requires this authority alone. Admission is itself a continuation boundary, so it clears every stopped/frame authority before `next` is written; a later `continued` preserves the invalidation.
5. The correlated `next` response must acknowledge the exact pending sequence and command. Only after that positive acknowledgement may a later reader `stopped` event with `reason:"step"` and the exact explicit Int32 target complete the record successfully. Every stopped event—pre-acknowledgement, missing target, different target, or another reason—still updates ordinary lifecycle/authority but is ignored by the B3 waiter. `allThreadsStopped:true` does not substitute for the exact target match. `exited` or `terminated` may settle the active record before or after acknowledgement. Wrong command/correlation, malformed active response, framing/write/reader failure, or unexpected internal failure is a redacted protocol error. No raw DAP `message`, body, event payload, stack frame, exception text, or adapter error crosses the public boundary.
6. One absolute 30-second deadline starts only after the admitted `next` bytes are committed and bounds the acknowledgement plus remaining observation with one decreasing time budget. A negative acknowledgement and cancellation before any bytes commit clear the record and preserve the usable session. A timeout or caller cancellation after bytes commit leaves an unresolved adapter action: the handler settles the typed timeout or propagates cancellation, releases its lease, and joins existing `SessionSlot.CloseAndDrainAsync`; later token use returns `DEBUG_SESSION_NOT_FOUND`. No retry, polling, implicit `get_threads`, or follow-up stack/source request occurs.
7. The complete normalized outcomes are closed:

   | Outcome | `kind` | `isError` | Required public fields |
   | --- | --- | --- | --- |
   | Exact acknowledged post-ack step stop | `step_over_success` | false | `threadId` (the explicit Int32) |
   | Process exit or termination while observed | `step_over_terminal` | false | `terminal` (`exited` or `terminated`), `exitCode` only when the existing state recorded an Int32 exit code |
   | Post-write operation deadline expires | `step_over_timed_out` | false | `threadId`, `timeoutSeconds:30` |
   | Occupied B3 record, valid negative acknowledgement, or absent current stopped authority before write | `dap_step_over_refused` | true | `error:"DAP_STEP_OVER_REFUSED"` |
   | Malformed/corrupt/uncorrelated active exchange or framing/write/reader/internal failure | `dap_step_over_protocol_error` | true | `error:"DAP_STEP_OVER_PROTOCOL_ERROR"` |

   Every application outcome uses the existing complete-result convention: `structuredContent` is the normalized object and exactly one text content item is its identical JSON serialization. Existing `invalid_tool_arguments` and `DEBUG_SESSION_NOT_FOUND` retain their current complete-result shapes. No variant exposes raw DAP values beyond the normalized explicit thread id and existing recorded terminal exit code.
8. `SessionSlot` remains the only lease, abort, token-removal, close-and-drain, stop, and dispose owner. A pre-write refusal, negative acknowledgement, and pre-write cancellation preserve session usability. A post-write timeout, post-write caller cancellation, or protocol error releases its own lease before joining the existing `CloseAndDrainAsync` path; later calls receive established not-found behavior. B3 adds no public cleanup registry, execution-state cache, queue, or exclusive `SessionSlot` mode.

## Alternatives

| Alternative | Decision | Rationale |
| --- | --- | --- |
| Typed `step_over` backed by internal DAP `next`, one private active record, exact acknowledgement, and later exact-target step observation | Chosen | It is the smallest native inspect→act→observe slice. It reuses B1 lifecycle ownership and B2 admission/state gates while isolating the execution-only exclusion to one session-private record. |
| Rejecting the first non-exact stopped event | Rejected | DAP events have no request correlation and `next` may resume other threads. Local bookkeeping cannot prove causality; ordinary unrelated lifecycle events must be ignored, not classified as a public failure. |
| Typed native `continue_execution` first | Rejected | It may run indefinitely or terminate without a native breakpoint. Establishing a meaningful observe contract would require breakpoint or pause authority, widening B3 beyond one bounded vertical slice. |
| Generic MCP-to-DAP execution forwarding | Rejected | It would publish arbitrary commands, payloads, capability variance, and raw adapter failures as public authority. |
| Keep a live session after post-write timeout or caller cancellation | Rejected | A delayed old step event could contaminate a later operation. Safe retention needs a larger quarantine/resynchronization mechanism; existing slot close-and-drain is bounded and deterministic. |

## Component map and data flow

```mermaid
sequenceDiagram
  participant C as Modern MCP client
  participant R as DebugSessionRegistry
  participant L as SessionSlot
  participant S as NetCoreDbgSession
  participant G as admission + state gates
  participant D as netcoredbg DAP

  C->>R: step_over(debugSessionId, threadId)
  R->>R: closed input + opaque lookup
  R->>L: acquire lease
  L->>S: StepOverAsync(explicit Int32)
  S->>G: verify stopped authority; reserve single-flight record; clear authority
  G->>D: correlated next({threadId})
  G-->>S: release admission
  D-->>S: correlated next acknowledgement
  Note over S: exact positive acknowledgement, then remaining time ≤ 30 seconds
  D-->>S: later stopped(reason=step, exact threadId)
  S->>G: verify acknowledged record and exact target
  S-->>R: normalized typed outcome
  R->>L: release lease; protocol error only joins existing drain
  R-->>C: complete result with no raw DAP payload
```

`Program.DebugSessionRegistry` owns tool catalog order, exact input validation, opaque-token lookup, one `SessionSlot` lease, complete result normalization, output serialization, and protocol-error eviction decision. It does not make suspension, frame, or DAP event decisions.

`SessionSlot` remains lifecycle-only: it admits or rejects a lease, carries the abort token, removes the exact token during close, and owns one-winner drain/stop/dispose. It does not retain an operation result.

`DebugAdapter.NetCoreDbgSession` owns the one internal `next` request, correlation, 30-second post-write deadline, one private active B3 record, reader-order event handling, and lifecycle/frame-authority transitions. `_callStackAdmissionGate` serializes B2 target capture and B3 authority invalidation/reservation/register/write with reader lifecycle transitions; `_stateGate` protects DAP session state, stopped/frame authority, and private active-record visibility. The operation releases admission before awaiting I/O. The reader always applies ordinary lifecycle state, but credits B3 only after exact acknowledgement and exact explicit-target step observation.

## Compatibility, migration, and provenance

This is an additive parallel change. The native catalog receives one new `step_over` entry after `get_call_stack`; the frozen modern application schema receives only its closed input and result variants. No stored data, public selector, package, default route, or existing native tool changes shape.

The installed Python `netcoredbg-mcp` remains the selected default and rollback route. Its `step_over` calls `execute_and_wait`, and `SessionManager.step_over` resolves `thread_id or current_thread_id`; Python therefore allows an omitted ambient current-thread fallback, including falsy-zero fallback behavior. Its pause path may query threads and choose the first thread. `prepare_for_execution`/`wait_for_stopped` replace and await an asyncio event that can resolve for stopped, exited, or terminated. B3 takes none of those behaviors: no optional thread, no current/first thread lookup, no Python error envelope, source enrichment, retry, package selection, or cross-route invocation.

Rollback is not selecting the additive native executable; no user or data migration exists. B3 performs no release, publication, Git action, or source/test implementation.

## Security and lifecycle boundaries

The trust boundary is closed client input -> opaque capability lookup -> slot lease -> capability-independent stopped-thread authority -> internal typed `next` -> exact acknowledgement -> later exact reader observation -> normalized result. Client input is validated before DAP I/O. The opaque token is never interpreted as a process id or DAP selector. The exact thread is not derived from process state. The session-private active record cannot be enumerated or reused by a different call; it blocks concurrent step-over without blocking safe inspection leases. Raw adapter error text and raw event bodies are redacted. A post-write uncertain action closes through the existing `SessionSlot` order rather than creating a competing quarantine/close owner.

## Full challenge status

**Current result: REVISE; no admission is claimed by this repair.** The recorded FULL challenger identified the association, capability-independent authority, security, baseline, and receipt gaps repaired here. Before implementation, a fresh independent `B3-G0` FULL challenger must issue `GO`, `REVISE`, or `RETHINK` against this exact repaired ADR/spec/plan/tasks packet and write its durable verdict to root `.agent/runs/b3-native-step-over/g0-challenger.md`. It must challenge the premise, existing-code leverage, alternatives, smallest scope, source freshness, dependencies, complexity, value, scope creep, assumptions, bias, and the closed-input/lifecycle trust boundary. This document does not fabricate that recheck or a Governor verdict.

## Deferred work

`continue_execution`, `pause_execution`, `step_into`, `step_out`, breakpoint management, step targets, single-thread execution, stepping granularity, current-thread/first-thread selection, source or stack reads after the step, generic DAP forwarding, UI/bridge work, Python changes or retirement, selector/default changes, release, and publication are outside B3.

# Plan — Native DAP frame inspection

## Architecture

```mermaid
sequenceDiagram
  participant C as Modern MCP client
  participant R as DebugSessionRegistry
  participant L as Registry-owned SessionSlot
  participant S as NetCoreDbgSession operation/state gate
  participant D as netcoredbg DAP
  C->>R: get_call_stack(debugSessionId, threadId, startFrame?, levels?)
  R->>R: validate closed input and resolve opaque token
  R->>L: acquire operation lease
  L->>S: GetCallStackAsync
  S->>S: under one gate, require capability and requested target's current private token
  alt capability absent or token invalidated before write
    S-->>R: redacted stack-trace refusal; no stackTrace write
  else capture current target token and write finite request
    S->>D: correlated finite stackTrace(threadId, startFrame, levels)
    D-->>S: correlated success or refusal
    S-->>R: bounded typed frame result
  end
  R->>R: normalize and enforce 256 KiB structuredContent ceiling
  R-->>C: call_stack_success or redacted typed error
  opt protocol error
    R->>L: release own lease, then CloseAndDrainAsync
    L->>L: close admission and remove exact token
    L->>S: existing stop/dispose path after drain
  end
```

`Program.DebugSessionRegistry` owns the public catalog order, exact MCP input validation, opaque-token lookup, complete result envelope, serialized `structuredContent` ceiling, and one `SessionSlot` lease per live token. It does not inspect session state or capability before dispatch. `SessionSlot` remains lifecycle ownership only: it serializes admission, supplies the abort token, removes the exact token on close, and invokes the existing one-winner drain/stop/dispose path.

`DebugAdapter.NetCoreDbgSession` owns the only new internal typed DAP command, a narrow initialize-derived `supportsDelayedStackTraceLoading` session capability record, and one internal DAP operation/state gate. The initialize record is true only for an explicit boolean true and is neither exposed nor generalized into a feature registry. Under the gate, a `stopped` event with `allThreadsStopped:true` replaces every prior eligibility token with one all-target token. A `stopped` event whose `allThreadsStopped` is omitted or false atomically clears every all-target and per-target token, then installs only a per-target token for its supplied `threadId`; if it omits `threadId`, it installs no token. `continued` has three exhaustive transitions: omitted `allThreadsContinued` or `true` atomically clears all-target and every per-target token; `false` with a named `threadId` invalidates only that target and preserves unrelated target eligibility while excluding the named target from any all-target representation; `false` without `threadId` fails closed by clearing every token, making no target eligible, and retaining no unknown token. `GetCallStackAsync` holds admission from capability/token checking until it has registered and written the correlated finite request, capturing the requested target's private token for that request only. At the correlated response's serial-reader dispatch position, the single DAP reader validates the request-local captured target token under `_stateGate`; it does not reacquire the admission gate. A held response after target continuation or resume→restop therefore returns refusal with no frames and preserves the session; a new token admits only a newly admitted request. Tokens are never exposed or cached, and the registry has no state/capability precheck. This makes races testable: admission writes before the event under the former token, or refuses with no post-event write; after valid named partial continuation, only the named target is refused and unrelated targets remain eligible.

Empty names are valid when within the 1,024-byte limit. A source object without `path`, including a sourceReference-only object, is likewise valid and normalizes to `source:null,line:0,column:0`; raw `sourceReference` is deliberately excluded because it is an adapter-local handle for a future source-retrieval authority, not execution-location data B2 needs. Frame `line` and `column` accept only integers in `0..9007199254740991`, inclusive, matching the vendored DAP `uint64` safe-integer maximum; negative, non-integral, and above-range values are protocol errors.

B1 is binding provenance, not current-head test evidence: implementation `3ed4b85` was merged by `11b69e8`; its native 52/52 receipt was a dirty pre-merge observation, with no post-merge native rerun claimed here. B2-T05 must create current-candidate B1 baseline evidence rather than inheriting that receipt.

## Alternatives

ADR-006 selects an internal typed bounded `stackTrace` seam with an initialize-derived paging gate and session-local state/write atomicity. Generic DAP forwarding is rejected because it exports arbitrary adapter authority. Retaining call-stack inspection only in Python is rejected because it leaves the native stateful walking skeleton unable to locate execution. Scopes and frame consumption are deliberately deferred to a later slice.

## Migration

Parallel additive change: the native catalog gains `get_call_stack` after `get_threads`, moving the current ten-tool native order to eleven tools. The modern frozen application schema receives only the new input/result variants and `invalid_tool_arguments` tool name required for B2; existing variants are not broadened. No native default selector exists to remove.

Python is neither called nor changed by this native request. Retained installed Python is the default/rollback and is replayed solely as a non-selection journey from `specs/006-a1-local-preview/quickstart.md`. There is no user/data migration, cache backfill, frame-id persistence, release, or publication. Rolling back B2 means not selecting the additive native executable; no Python consumer contract or stored data requires reversal.

## Requirements-to-files

| Requirement | Ticket | Future implementation/test owners (not executed by this planning packet) |
|---|---|---|
| B2-REQ-001/002/005/006 | B2-T02, B2-T03 | `host/NetCoreDbg.Mcp.Stateless/Program.cs`; `host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/GetCallStackContractTests.cs` (new); `ModernProtocolContractTests.cs`; `StructuredContentSchemaParityTests.cs`; `specs/001-mcp-stateless-strangler/contracts/modern-front-door.schema.json` |
| B2-REQ-003/004/007/008 | B2-T02, B2-T03 | `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs` (private initialize-capability record, request-local target-token capture, atomic stopped/continued eligibility transitions, and serial-reader validation of the request-local captured target token under `_stateGate` at the correlated response's dispatch position without admission-gate reacquisition; no public lifecycle token); `host/NetCoreDbg.Mcp.Stateless.Tests/DebugAdapter/NetCoreDbgSessionContractDriver.cs`; `NetCoreDbgSessionTests.cs`; `SessionSlotContractTests.cs`; `ModernMcp/SessionSlotLifecycleTests.cs` |
| B2-REQ-003/004/006 | B2-T01, B2-T02, B2-T03 | `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs`; `DebugAdapter/NetCoreDbgSessionContractDriver.cs`; `ModernMcp/ModernMcpProcessDriver.cs` (including all-thread `continued` omitted/true, named partial continuation, missing-thread partial fail-closed, held-response continuation and resume→restop no-frame checks, plus `line`/`column` safe-integer boundaries) |
| B2-REQ-004/008 | B2-T01, B2-T02 | `docs/dap-protocol/specification.md`; `docs/dap-protocol/debugAdapterProtocol.json`; no Python source change |
| B2-REQ-009 | B2-G0, B2-T04 | fresh independent `B2-G0` recheck over the corrected ADR/spec/plan/tasks packet, including all-thread/named-partial continuation and held-response token-current publication, then installed platform security-review contract `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md`; durable security evidence only at root `.agent/runs/b2-native-frame-inspection/security-review.md` |
| B2-REQ-008/009 | B2-T05 | `GetThreadsContractTests.cs`; `SessionSlotContractTests.cs`; `SessionSlotLifecycleTests.cs`; `NetCoreDbgSessionTests.cs`; `StructuredContentSchemaParityTests.cs`; `specs/006-a1-local-preview/quickstart.md` journey evidence only; no package, selector, or Python file change |

## Bounded ticket graph

`B2-G0 Admission recheck -> B2-T01 Explore -> B2-T02 RED Test -> B2-T03 Code -> B2-T04 Review -> B2-T05 final Test`. `B2-G0` independently admits only the corrected all-thread/named-partial `continued`, request-local token-current response-publication, strict partial-stop replacement, and safe-integer frame-bound contract; this repair supersedes the prior incomplete recheck. Each later ticket is a vertical tracer bullet or a bounded validation step: T01 resolves only existing ownership and DAP facts; T02 makes the observable capability, gate, lifecycle transition, held-response, valid-frame, and cleanup contract fail; T03 closes that contract through the named owners; T04 independently judges the exact candidate with the installed S2 security-review contract; T05 demonstrates the finished native and retained-Python journeys plus current-candidate B1 baseline. No ticket is an expand-contract migration because B2 has no live consumer migration.

## FULL challenger

**Status: REVISE findings incorporated; fresh `B2-G0` recheck required before implementation.** The architect FULL challenger found bounded, same-scope corrections: capability-gated paging, atomic stopped/continued-state and request admission, all-thread/named-partial continuation invalidation, request-local token-current response publication, strict partial-stop eligibility replacement, safe-integer frame bounds, S2 review/evidence ownership, current-candidate B1 baseline evidence, exact milestone closure, and valid empty-name/sourceReference-only normalization. This repair supersedes the prior incomplete recheck; it resolves the contract in B2-REQ-003/004/009, the scenarios, file map, and B2-T02/T04/T05 checkpoints but makes no fabricated recheck-clean claim. `B2-G0` must issue `GO`, `REVISE`, or `RETHINK` with the required taxonomy and inspect premise, existing B1 lifecycle leverage, alternatives, minimal scope, staleness, false dependencies, complexity, value, scope, verification, and rollout before implementation admission.

## Milestone map

| Milestone | Tickets | Value statement | Binding constraints | Shipping moment |
|---|---|---|---|---|
| M1 — bounded native frame inspection | B2-G0 through B2-T05 | An opt-in native client could enumerate a thread but not identify its execution location; bounded typed frame inspection fixes that. | Preserve B1 opaque-token and one-slot lifecycle ownership; finite paging only under initialize-recorded delayed-stack capability; atomic stopped/all-thread/named-partial continuation eligibility transitions; request-local token-current response publication; frame `line`/`column` limited to DAP-safe integers; Python stays selected rollback; `release_intent: none`. | `B2-G0` first independently admits the corrected contract; then the native contract is demoable through a controlled DAP client, current-candidate baseline and S2 evidence are recorded, and the retained Python journey remains independently working; `release_intent:none` bars tag, publish, and selector action. |
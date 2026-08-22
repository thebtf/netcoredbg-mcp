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
  S->>S: under one gate, require stopped epoch and initialize capability
  alt capability absent or continued wins admission
    S-->>R: redacted stack-trace refusal; no stackTrace write
  else eligible stopped epoch
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

`DebugAdapter.NetCoreDbgSession` owns the only new internal typed DAP command, a narrow initialize-derived `supportsDelayedStackTraceLoading` session capability record, and one internal DAP operation/state gate. The initialize record is true only for an explicit boolean true and is neither exposed nor generalized into a feature registry. Under the gate, `stopped` starts or increments an opaque stopped epoch, `continued` invalidates it, and `GetCallStackAsync` holds admission from the stopped-plus-capability check until it has registered and written the correlated finite request. The epoch is not exposed or cached. This makes the continued race binary and testable: a fixture must observe the write before continued under the former epoch, or the refusal with no post-continued write. It sends only `stackTrace` with the explicit thread and resolved finite page; it validates correlation, response body, each projected field, and optional `totalFrames` before the registry serializes public content. It neither becomes a generic DAP proxy nor reads source files. DAP `success:false`, absent delayed-stack capability, and not-stopped state become the same typed refusal. All malformed, wrong-command/correlation, timeout, reader-failure, field-bound, frame-count, or final-size failures become the protocol-error path and use existing slot cleanup.

Empty names are valid when within the 1,024-byte limit. A source object without `path`, including a sourceReference-only object, is likewise valid and normalizes to `source:null,line:0,column:0`; raw `sourceReference` is deliberately excluded because it is an adapter-local handle for a future source-retrieval authority, not execution-location data B2 needs.

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
| B2-REQ-003/004/007/008 | B2-T02, B2-T03 | `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs` (private initialize-capability record and operation/state gate; no public `DapSessionState` epoch); `host/NetCoreDbg.Mcp.Stateless.Tests/DebugAdapter/NetCoreDbgSessionContractDriver.cs`; `NetCoreDbgSessionTests.cs`; `SessionSlotContractTests.cs`; `ModernMcp/SessionSlotLifecycleTests.cs` |
| B2-REQ-003/004/006 | B2-T01, B2-T02, B2-T03 | `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs`; `DebugAdapter/NetCoreDbgSessionContractDriver.cs`; `ModernMcp/ModernMcpProcessDriver.cs` |
| B2-REQ-004/008 | B2-T01, B2-T02 | `docs/dap-protocol/specification.md`; `docs/dap-protocol/debugAdapterProtocol.json`; no Python source change |
| B2-REQ-009 | B2-T04 | installed platform security-review contract `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md`; durable evidence only at root `.agent/runs/b2-native-frame-inspection/security-review.md` |
| B2-REQ-008/009 | B2-T05 | `GetThreadsContractTests.cs`; `SessionSlotContractTests.cs`; `SessionSlotLifecycleTests.cs`; `NetCoreDbgSessionTests.cs`; `StructuredContentSchemaParityTests.cs`; `specs/006-a1-local-preview/quickstart.md` journey evidence only; no package, selector, or Python file change |

## Bounded ticket graph

`B2-T01 Explore -> B2-T02 RED Test -> B2-T03 Code -> B2-T04 Review -> B2-T05 final Test`. Each is a vertical tracer bullet or a bounded validation step: T01 resolves only existing ownership and DAP facts; T02 makes the observable capability, gate, valid-frame, and cleanup contract fail; T03 closes that contract through the named owners; T04 independently judges the exact candidate with the installed S2 security-review contract; T05 demonstrates the finished native and retained-Python journeys plus current-candidate B1 baseline. No ticket is an expand-contract migration because B2 has no live consumer migration.

## FULL challenger

**Status: REVISE findings incorporated; independent recheck pending.** The architect FULL challenger found bounded, same-scope corrections: capability-gated paging, atomic stopped-state/request admission, S2 review/evidence ownership, current-candidate B1 baseline evidence, exact milestone closure, and valid empty-name/sourceReference-only normalization. This packet resolves them in B2-REQ-003/004/009, the scenarios, file map, and B2-T02/T04/T05 checkpoints; it makes no fabricated challenger-clean claim. Before implementation admission, an independent rerun must issue `GO`, `REVISE`, or `RETHINK` with the required taxonomy and inspect premise, existing B1 lifecycle leverage, alternatives, minimal scope, staleness, false dependencies, complexity, value, scope creep, assumptions, bias, and security assumptions. B2-T04 cannot pass without that verdict.

## Milestone map

| Milestone | Tickets | Value statement | Binding constraints | Shipping moment |
|---|---|---|---|---|
| M1 — bounded native frame inspection | B2-T01 through B2-T05 | An opt-in native client could enumerate a thread but not identify its execution location; bounded typed frame inspection fixes that. | Preserve B1 opaque-token and one-slot lifecycle ownership; finite paging only under initialize-recorded delayed-stack capability; atomic stopped/write gate; Python stays selected rollback; `release_intent: none`. | The native contract is demoable through a controlled DAP client, current-candidate baseline and S2 evidence are recorded, and the retained Python journey remains independently working; `release_intent:none` bars tag, publish, and selector action. |
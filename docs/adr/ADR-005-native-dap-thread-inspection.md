# ADR-005: Add typed native thread inspection without a raw DAP surface

## Status
Proposed

## Context
`NetCoreDbg.Mcp.Stateless` owns a request-local modern MCP front door, opaque `debugSessionId` registry, native DAP lifecycle/framing/correlation, and bounded cleanup. It currently exposes lifecycle state only; the selected Python route exposes `get_threads` as `{data:[{id,name}], state}`. DAP defines `threads` as an unconditional request with a required `body.threads` array of `{id:int32,name:string}`; it has no `supportsThreadsRequest` capability.

## Decision
1. Add one additive native MCP tool named `get_threads` with a closed input object containing exactly required `debugSessionId`. Its published schema admits a string with `minLength: 1`; the registry accepts it only when `debugSessionId.Trim().Length > 0`. A missing arguments object, omitted `debugSessionId`, non-string values, empty or whitespace-only strings, and extra fields return `invalid_tool_arguments` before DAP I/O. Every valid string is opaque: every unavailable non-whitespace string returns `DEBUG_SESSION_NOT_FOUND` without exposing token grammar.
2. Add an `internal` strongly typed `NetCoreDbgSession.GetThreadsAsync` returning an immutable internal operation-result type; only `DebugSessionRegistry` consumes it. It emits only DAP `threads`, validates correlated response command/type/body before normalization, and exposes no generic request API. Its reader also notifies that registry when reader failure occurs without an active request, so the registry can close the owning session slot.
3. The registry owns one `SessionSlot` per live opaque token. Every DAP operation acquires that slot's lease before DAP I/O. Cleanup from an admitted operation records its cleanup cause, releases its own lease, and only then invokes close-and-drain, preventing self-deadlock. Close-and-drain first closes admission, waits remaining admitted leases, removes the token, then elects exactly one winner to call `StopAsync` and dispose. Explicit stop, host shutdown, unusable-session eviction, reader failure, timeout, and protocol-error cleanup all use this path.
4. A DAP success response yields a bounded normalized thread list while the session remains live. A correlated DAP `success:false` yields `{kind:"dap_threads_refused",error:"DAP_THREADS_REFUSED"}` and leaves the session usable. Wrong-command, malformed, timeout, or bound-exceeding responses yield `{kind:"dap_threads_protocol_error",error:"DAP_THREADS_PROTOCOL_ERROR"}`, expose no adapter body, remove the native token, and trigger bounded slot cleanup. Invalid MCP input is rejected before DAP I/O.
5. Preserve the native lifecycle tools and six Native Scene tool contracts. Preserve the Python package, selected public catalog, CLI, relay, and consumer selection unchanged.

## Alternatives
| Alternative | Decision | Reason |
|---|---|---|
| Expose generic DAP request forwarding | Rejected | Makes adapter variance and arbitrary command/input public authority. |
| Keep inspection only in Python | Rejected | Prevents the next typed native stateful walking skeleton. |
| Add threads and call stack together | Deferred | Stack introduces frame identity/paging; threads alone is independently useful. |

## Security and rollback
The opaque-token→DAP request→normalized-result path is S2: validate closed MCP input, retain correlation and timeouts, bound thread count/name/result bytes, redact adapter error bodies, serialize inspection against stop/cleanup through registry-owned `SessionSlot` leases, and review .NET input/output handling using the installed platform source `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md`. The durable S2 review receipt target is `.agent/runs/b1-native-thread-inspection/security-review.md` in the primary repository; this planning slice does not create it. Rollback is non-selection: the native executable has no default selector, so the exact installed Python `PRODUCT_WORKS` journey recorded by `specs/006-a1-local-preview/quickstart.md` remains unchanged; no session is redirected to Python.

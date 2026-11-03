# Native Expression Evaluation

Status: source candidate implemented and exercised with a controlled DAP adapter; integration, a real debugger, and installed delivery remain unproven. This additive slice starts from native step-pair tree `ad77f0f689319171e6f0bc1cbeb1464ee853b525`. Python remains the installed default and rollback. It does not establish full Python parity or complete the migration.

## Consumer outcome

A native MCP client stopped in a managed frame can request an expression evaluation, receive its result and optional type, and inspect structured children with the returned variables reference and the same current inspection context. Expressions can execute application code or otherwise change the debuggee: `watch` is a DAP context, not a read-only guarantee.

## Boundary

- Add `evaluate_expression(debugSessionId, inspectionContextId, frameId, expression)` to the existing stateful host. All four inputs are explicit. The inspection context is the stopped-epoch token from `get_call_stack(includeInspectionContext=true)`; as with `get_scopes`, it does not prove the numeric frame ID was issued.
- Send one DAP `evaluate` with the exact expression, explicit frame ID and `context="watch"`. Do not infer a frame, forward arbitrary DAP parameters, retry automatically, or claim Python's optional-frame and result-envelope parity. Do not advertise the operation as read-only or idempotent.
- On a current successful response, return a typed result string, nullable type, nonnegative variables reference and the same inspection context. Apply the existing serialized inspection-response bound and text/structured-content parity. A positive variables reference may be passed to the existing `get_variables` while that context remains valid.
- A missing or stale context detected before the write is refused without DAP I/O. A negative DAP response, a stale response after a write, or a malformed, timed-out or oversized response must not claim that no expression effect occurred. Return a redacted failure or unknown-outcome classification as appropriate; never retry. Protocol failures and uncertain in-flight operations use the existing session close-and-drain owner.
- Cancellation before writer handoff preserves a usable session and sends no request. Cancellation after a write starts or commits invalidates the inspection context and drains the session before cleanup finishes; no result is invented and expression side effects remain unknown. Preserve existing inspection, stepping, continuation and stop behavior.
- Do not change the Python package, default selector, preview publication, UI automation or release state. `quick_evaluate` and Python's implicit frame selection remain separate migration work.

## Integration and proof

Use the existing session request/inspection owner in `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs`, the MCP routing and schema owner in `host/NetCoreDbg.Mcp.Stateless/Program.cs` and `specs/001-mcp-stateless-strangler/contracts/modern-front-door.schema.json`, and the existing controlled DAP fixture and inspection contract tests. Cover discovery, exact request/result, invalid or stale admission without I/O, DAP failure, stale in-flight response, and cancellation before and after write without creating a separate verification harness. Root must observe the test-only RED before product changes, then the same checks GREEN and a real MCP consumer path. Real netcoredbg behavior and installed delivery remain separate proof obligations.

Before product changes, the targeted catalog test ran on the test-only candidate and failed because no `evaluate_expression` tool existed (1 failed, 0 passed). This establishes the missing public route, not real-debugger behavior.

The combined controlled MCP/DAP and lifecycle selection passed 38/38 cases on the candidate. It includes the evaluation-specific pre-header and one-byte partial-header cancellation cases, the exact 256 KiB and one-byte-over serialized response cases (including escaped and non-ASCII text), and an explicit-stop race with a held evaluation. The pre-header case confirms the same context can be used by a new explicit request; the partial-write and held-stop cases retire the session. An independent source review found no material defect in the evaluation dispatch and session implementation. These results do not establish real-debugger behavior, installed delivery, or completion of the migration.

Protocol basis: the repository's `docs/dap-protocol/specification.md` Evaluate request/response. Legacy comparison: `src/netcoredbg_mcp/tools/inspection.py` and `session/manager.py`.

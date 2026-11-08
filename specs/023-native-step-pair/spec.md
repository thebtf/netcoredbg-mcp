# Native Step Into and Step Out

Status: source integrated via PR #388 at `891f1deb68c942a48f327f6e7eefae7efb7d803f`; installed delivery remains pending. This source slice was built on the native pause base `3384ffa318cdbf0406c8df0f0eec3bf9986702c8`. Python remains the installed default and rollback. Completing this slice does not complete the Python-to-.NET migration or prove legacy parity; full compatibility remains open.

## Consumer outcome

A native MCP client can stop at a managed call, use `step_into` on the observed stopped thread, inspect the resulting managed frame, then use `step_out` and inspect the caller. A step acknowledgement is not itself a completed step. DAP may step over when it cannot enter a call; only the actual observed stack can establish that a particular callee was entered.

## Boundary

- Add `step_into(debugSessionId, threadId)` and `step_out(debugSessionId, threadId)` beside native `step_over`. Both arguments are required; apply the existing closed schema, opaque-session handling, Int32 validation and zero-I/O invalid-argument rules of `step_over`, with each new tool's own name in errors.
- Send exactly one typed DAP `stepIn` or `stepOut` with the explicit `threadId`. Do not send `targetId`, `singleThread`, or `granularity`, infer a target, or forward arbitrary DAP requests. Target selection and `get_step_in_targets`, legacy optional-thread selection, legacy result shape and full parity remain open capabilities of the parent migration.
- Use the existing session-owned execution admission, stopped-thread authority, correlation, event ordering, inspection invalidation, deadline and cleanup rules of the accepted `step_over` contract in `specs/009-native-dap-step-over/spec.md`. Preserve its existing results and behavior. Each new step excludes the other step, existing step-over, continue and pause. Preserve pause's existing allowed overlap with an already committed continue; do not create a second unsynchronized stepping path.
- Each tool returns its own typed success, terminal, timeout, refusal and redacted protocol-error outcomes, with the existing native text/structured-content mirror. Success requires the correlated positive acknowledgement followed by the existing exact `stopped(reason=step, threadId)` observation. A pre-ack, different-thread, missing-thread or non-step event does not become success. One 30-second deadline starts after the complete write and covers acknowledgement plus observation. An observed terminal event may settle before or after acknowledgement. Terminal, timeout and protocol failure use the existing close-and-drain owner; a negative acknowledgement retains the usable session. Cancellation completed before writer handoff retains the session without restoring old inspection authority; possible or uncertain writes are drained without retry.
- Do not change the Python package, public default selector, existing preview channel, bridge/UI behavior, or publication state.

## Integration and proof

The owners are `host/NetCoreDbg.Mcp.Stateless/Program.cs`, its `DebugAdapter/NetCoreDbgSession.cs` lifecycle owner, and existing controlled-adapter and modern MCP contract tests. Reuse the current fixture and response/event coordination rather than a generic DAP relay. Preserve the pause candidate's admission and close/drain invariants.

Root observed on the source candidate: new pair plus parser selection 128/128, existing step-over 100/100 with separately bounded startup, and pause contract/behavior/lifecycle 112/112. An independent source review of the step changes found no material defect. These scoped results do not establish all migration gates green or installed delivery.

The direct consumer oracle uses a source-built native host, fresh MCP client, real netcoredbg and an owned managed fixture with symbols: observe a breakpoint on a call, step into the actual callee, verify its top frame, step out, and verify the caller. Root observed the real source-host/netcoredbg journey entering `Middle` and returning to `Outer`, with public stop and retained Job drain. This source proof does not establish absence of transient focus, installed delivery, target selection, or full migration.

Protocol basis: the repository's `docs/dap-protocol/specification.md`, StepIn and StepOut requests, specifies the thread argument, acknowledgement, later step stop and the allowed step-in-to-next behavior. Legacy comparison is `src/netcoredbg_mcp/tools/debug.py` and its session execution wrapper.

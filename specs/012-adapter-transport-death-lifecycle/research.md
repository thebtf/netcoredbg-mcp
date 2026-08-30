# Research: Adapter Transport-Death Lifecycle

**Scope:** Wave 1 D1 transport/session lifecycle only.  
**Source base:** `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`.  
**Issue:** Engram #450.  
**Primary root-cause record:** `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md`.  
**External research source:** `agent://GitHubDapLifecycle`, retrieved 2026-08-30.

## Decision record

| Topic | Decision | Evidence and rationale |
|---|---|---|
| Root cause owned by this child | Add a client-to-manager transport-terminal handoff. | **OBSERVED:** current `_read_loop` exits on EOF, faults requests, and makes no manager callback. **OBSERVED:** manager state changes only through parsed DAP events. The investigation establishes the zero-assumption chain to stale public `RUNNING`. |
| Terminal fact model | Use one immutable bounded record for one client run. | **INFERRED:** no inspected project provides the complete required fact set as a reusable unit. A frozen snapshot prevents a late observer from silently rewriting the reason previously published to the manager. |
| Observer model | Start stdout reader, stderr drainer, and adapter-process waiter independently. | **ADOPT:** MIEngine's stream/process joining and debugpy's output/process ordering show that stdout alone cannot explain a process lifetime or retain diagnostic output. |
| Finalization model | Route terminal triggers through one guarded client-owned finalizer. | **ADOPT:** VS Code's `inShutdown` and `firedAdapterExitEvent` pattern prevents duplicate cleanup and owner-visible terminal publication. |
| DAP semantics | Keep `exited`, `terminated`, adapter process exit, and EOF distinct. | **ADOPT:** the official Debug Adapter Protocol defines `exited` as debuggee exit and `terminated` as debugging termination; neither equates to the adapter process outcome. |
| Bounded joining | Give process completion and stderr drain a documented bounded opportunity before freezing diagnostics. | **ADAPT:** MIEngine deliberately bounds its reader/process joins because inherited pipe handles can prevent EOF indefinitely. The child copies the bounded property, not upstream timing constants. |
| Public state outcome | Use the existing terminal session state and resource path once, while retaining an explicit diagnostic distinction. | **INFERRED:** this directly prevents current `SessionState.to_dict` from deriving a false `debuggeeAlive` value without adding a second public state machine. |
| Historical producer cause | Do not claim one. | **OBSERVED:** the incident lacks exit code, stderr, last event, and reliable disappearance ordering. EOF can result from multiple causes. |
| Pre-build cleanup | Exclude it. | **OBSERVED:** global image-name cleanup is a real separate risk, but no incident correlation was found. Governor assigns it to Wave 2. |

## Current-source facts

| Class | Fact | Source | Consequence for Wave 1 |
|---|---|---|---|
| OBSERVED | `DAPClient.start()` launches the adapter with stdout and stderr pipes and starts only `_read_loop`. | `src/netcoredbg_mcp/dap/client.py:165-179` | The client already owns process and stream integration; add observers here rather than creating another lifecycle service. |
| OBSERVED | A falsey stdout read logs closure and exits the reader. Its `finally` faults pending requests and best-effort calls `terminate()` when return code is `None`. | `src/netcoredbg_mcp/dap/client.py:270-325` | EOF is terminal to client work but has no manager handoff, process wait, stderr drain, or bounded terminal record. |
| OBSERVED | Parsed DAP events are dispatched through registered handlers. | `src/netcoredbg_mcp/dap/client.py:326-355` | The last DAP event can be captured before handlers without changing wire parsing. |
| OBSERVED | The manager registers DAP event handlers, including `terminated` and `exited`, but no transport-terminal handler. | `src/netcoredbg_mcp/session/manager.py:847-869` | Raw EOF cannot mutate manager state. |
| OBSERVED | `_on_terminated` sets `TERMINATED`; `_on_exited` retains only the DAP exit code and wakes execution waiters. | `src/netcoredbg_mcp/session/manager.py:1043-1059` | Existing code already distinguishes protocol events; migration must preserve that distinction. |
| OBSERVED | `SessionState.to_dict()` derives `debuggeeAlive` from a retained PID and a nonterminal session state. | `src/netcoredbg_mcp/session/state.py:534-554` | A missed state transition becomes a false live-debuggee claim. |
| OBSERVED | `get_debug_state` returns the current state serialization without an OS or client liveness probe. | `src/netcoredbg_mcp/tools/debug.py:545-565` | The correction belongs at lifecycle state, not as a liveness-probe patch at the public tool. |
| OBSERVED | Existing client tests do not drive raw `_read_loop` EOF. | `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md:144-156`; `tests/test_client.py` | T001 must add the deterministic fake-subprocess RED before implementation. |
| OBSERVED | Existing liveness and resource tests already own the relevant public behavior seams. | `tests/test_debuggee_liveness.py:102-147`; `tests/test_resource_updates.py:82-141` | Extend current focused tests rather than inventing a parallel test convention. |
| UNKNOWN | The historical adapter producer cause and original disappearance order are unrecoverable. | Investigation record, sections 1, 2, and 6 | Store facts for a later incident; do not select a crash, kill, or pipe cause. |

## Primary-source lifecycle research

### Microsoft MIEngine — bounded stream/process fan-in

**Source:** [microsoft/MIEngine `PipeTransport.cs` at `8ffc66d42067463d6582c8b1e3911a89b2069297`](https://github.com/microsoft/MIEngine/blob/8ffc66d42067463d6582c8b1e3911a89b2069297/src/MICore/Transports/PipeTransport.cs#L225-L344).

**OBSERVED:** `OnReadStreamAborted`, `AsyncReadFromStdError`, `DecrementReaders`, and `OnProcessExit` model stream completion and process completion separately. The stdout-abort path gives process exit a bounded opportunity to supply an exit fact; stderr drains independently; process exit waits only a bounded time for readers before it publishes.

**ADAPT:** retain independent stderr drain and bounded stream/process joining. Do not copy MIEngine's callback literal because it can publish a null exit fact from stream closure and is not the one-shot terminal publication contract required by this child.

### Microsoft debugpy — process exit, output drain, then protocol lifecycle

**Sources:** [debugpy `debuggee.py` at `e5743d3a00c6dee7d8140275c7df7e719ebb132f`](https://github.com/microsoft/debugpy/blob/e5743d3a00c6dee7d8140275c7df7e719ebb132f/src/debugpy/launcher/debuggee.py#L177-L227) and [debugpy `output.py`](https://github.com/microsoft/debugpy/blob/e5743d3a00c6dee7d8140275c7df7e719ebb132f/src/debugpy/launcher/output.py#L62-L112).

**OBSERVED:** the process waiter gets the process return code, waits for remaining captured output, then emits DAP `exited` and `terminated`; `kill()` checks whether the process is already complete before a second kill.

**ADOPT:** observe process exit before taking a final snapshot and preserve an already-exited guard. **ADAPT:** use bounded asyncio drains because a retained inherited pipe must not stall the client forever. Do not adopt debugpy's producer-side event order as a fabricated outcome for an already closed netcoredbg transport.

### Microsoft VS Code — one guarded shutdown owner

**Source:** [VS Code `rawDebugSession.ts` at `004a1fbb1658e61048b29d76e2ce380adfa18680`](https://github.com/microsoft/vscode/blob/004a1fbb1658e61048b29d76e2ce380adfa18680/src/vs/workbench/contrib/debug/browser/rawDebugSession.ts#L47-L107) and [its shutdown/final event code](https://github.com/microsoft/vscode/blob/004a1fbb1658e61048b29d76e2ce380adfa18680/src/vs/workbench/contrib/debug/browser/rawDebugSession.ts#L592-L645).

**OBSERVED:** adapter errors and adapter exit enter `shutdown`; `inShutdown` prevents duplicate cleanup; `firedAdapterExitEvent` prevents duplicate owner-visible adapter-exit notification. DAP `terminated` remains semantically distinct from adapter exit.

**ADOPT:** one finalizer guard and one manager callback. This is the direct protection against protocol/EOF/process/stop races.

### Official Debug Adapter Protocol — exited versus terminated

**Source:** [microsoft/debug-adapter-protocol specification at `bf8a5d27e8040044b84b863f90916e08925ee811`](https://github.com/microsoft/debug-adapter-protocol/blob/bf8a5d27e8040044b84b863f90916e08925ee811/specification.md#L339-L364).

**OBSERVED:** `exited` indicates that the debuggee exited and carries an exit code. `terminated` indicates that debugging has terminated and does not mean the debuggee exited.

**ADOPT:** do not use an adapter return code as a debuggee exit code, do not synthesize DAP `exited` from EOF, and do not let `exited` alone become a session-terminal trigger.

### vscode-go — a warning, not a template

**Source:** [golang/vscode-go `goDebug.ts` at `46048018519b6f727e920f5f5a4335acc436bdd3`](https://github.com/golang/vscode-go/blob/46048018519b6f727e920f5f5a4335acc436bdd3/extension/src/debugAdapter/goDebug.ts#L730-L771) and [terminal callback](https://github.com/golang/vscode-go/blob/46048018519b6f727e920f5f5a4335acc436bdd3/extension/src/debugAdapter/goDebug.ts#L2089-L2096).

**OBSERVED:** socket close and child-process close can feed a common callback without a shared one-shot guard.

**REJECT as a complete pattern:** it demonstrates why the child cannot publish a manager transition from each observer independently.

## Derived invariants

The following are **INFERRED** from the observed current source and primary-source patterns. They are the implementation contract, not claims about the historical incident's producer cause.

1. One client process run has at most one finalization task and exactly one published immutable terminal snapshot.
2. An observer may append facts to the private collection only before the finalizer freezes the snapshot. No observer may directly call manager state mutation, `terminate`, `kill`, or a resource notification.
3. The first terminal trigger identifies why finalization began. It does not erase an adapter return code, stderr tail, DAP event, or process completion observed during bounded joining.
4. Pending DAP requests terminalize once, before or with the single manager callback. They do not remain live behind an ordinary request timeout after the client has become terminal.
5. The finalizer is the only component allowed to choose the existing bounded graceful/forced adapter cleanup sequence. A process already observed as exited is never killed again.
6. For unrequested transport death, a transport-terminal callback turns an active manager session terminal exactly once and uses the existing state/thread resource path. For explicit manager stop, the callback records facts while the existing reset-to-idle path supplies the one public outcome. A repeated signal produces neither a second terminal transition nor a second reset publication.
7. A debuggee PID may remain historical diagnostic data. The terminal state and its public summary prevent that PID from becoming a false `debuggeeAlive` claim.
8. `exited`, `terminated`, adapter process exit, and stdout EOF remain separate stored facts even when they arrive in the same race.

## Rejected approaches

| Approach | Rejection reason |
|---|---|
| Make `get_debug_state` poll the PID or `DAPClient.is_running`. | It hides the missing lifecycle state transition, races identity reuse, and cannot wake waiting operations or publish resource state. |
| Send DAP `terminated` directly to the old manager handler and also call a terminal callback. | It admits two manager transitions under DAP/process/EOF races. The direct path must migrate to the one callback owner. |
| Treat stderr EOF as the terminal trigger. | stderr is diagnostic output; it can close before or after stdout/process lifecycle without establishing a usable DAP transport outcome. |
| Store whole stderr or whole DAP message bodies. | It turns an incident aid into an unbounded memory and disclosure surface. |
| Translate EOF into a DAP `exited` or assert a crash/foreign kill. | Neither is supported by the recorded incident or DAP semantics. |
| Couple this change to the global `taskkill` remediation. | The risk is real but separately caused and assigned to Wave 2. Coupling would blur the accepted root cause and delay the user-visible repair. |
| Change Sonar policy, coverage, default selection, or public routes to make the candidate easier to accept. | Parent PRG-007 and PRG-010 plus the Governor decision explicitly prohibit this scope expansion. |

## Research debt discharged

The Governor handed this child a specific research debt: validate bounded stream/process joining, single final callback, and guarded shutdown against GitHub-primary sources before designing Wave 1. The MIEngine, debugpy, VS Code, DAP specification, and vscode-go sources above discharge that debt. No unresolved external API or protocol fact blocks the D1 implementation shape.

## Limits preserved intentionally

- This document does not specify a constant number of bytes, lines, or milliseconds for terminal diagnostics. The implementation must choose named bounded budgets at the client boundary, document them in code, and prove them with tests. A fixed implementation detail is not needed to freeze the D1 contract.
- The document does not promise to detect the original adapter producer cause, Windows crash reason, debuggee/HWND disappearance order, or a foreign-process kill. It requires the next occurrence to retain bounded facts.
- The document does not supply an acceptance receipt, a passing test result, a review verdict, a commit SHA, or release evidence. Those facts do not exist at packet-authoring time.

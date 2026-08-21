# Plan — Native DAP thread inspection

## Architecture

```mermaid
sequenceDiagram
  participant C as Modern MCP client
  participant H as DebugSessionRegistry
  participant L as Registry-owned SessionSlot
  participant S as NetCoreDbgSession
  participant D as netcoredbg DAP
  C->>H: get_threads(debugSessionId)
  H->>H: validate exact closed input; resolve opaque token
  H->>L: acquire operation lease
  L->>S: GetThreadsAsync(lease abort token)
  S->>D: threads
  D-->>S: correlated success or failure
  S-->>L: typed operation result
  L-->>H: release lease; cleanup handoff if needed
  opt cleanup cause
    H->>L: CloseAndDrainAsync(cause)
    L->>L: close admission; remove exact token
    L->>L: drain admitted leases to shared deadline
    opt deadline elapsed
      L->>L: cancel lease abort token
    end
    L->>S: StopAsync after zero leases (forced if elapsed)
    L->>S: DisposeAsync in finally
  end
  H-->>C: threads_success or typed refusal
```

`Program.DebugSessionRegistry` owns closed MCP input, token lookup, result envelopes, and a registry-owned `SessionSlot` for each live token. The `get_threads` schema publishes `debugSessionId` as `string` with `minLength: 1`; registry validation then requires `Trim().Length > 0`, so every short or non-base64url non-whitespace value stays an opaque lookup. `SessionSlot` is not a DAP forwarding layer: it serializes admission, counts leases, and owns their abort token, while only the internal typed `NetCoreDbgSession.GetThreadsAsync` sends `threads`. A lease spans all DAP write/wait work and releases in `finally`. A cleanup-triggering admitted call records its reason, releases its own lease, then joins `CloseAndDrainAsync`; the first closer closes admission and removes the exact token atomically, making every later lookup unavailable and making all other cleanup triggers join the same task. The winner starts one configured `StopTimeout` drain deadline, first waits existing leases without touching session transport, and on expiry cancels the slot abort token linked to each typed operation. It waits those operations' `finally` releases before it calls the existing session lifecycle owner: `StopAsync` receives the forced token after expiry, otherwise its normal token. The winner always calls `DisposeAsync` in `finally`. This follows the current session contract: `NetCoreDbgSession.StopAsync` joins one `EnsureCleanupAsync` task; cancellation selects `_forceCleanup`; bounded cleanup uses its request/stop timeouts; and `CleanupAsync` disposes owned resources in `finally`. Thus a delayed reader, hung request, or stop failure cannot make cleanup self-wait, double-dispose, or dispose a transport still usable by an admitted operation.
The registry rejects a missing arguments object or omitted `debugSessionId` as `invalid_tool_arguments` before token lookup or DAP I/O; it does not treat an absent required field as an unavailable opaque token.
For every `get_threads` application outcome, the native result builder returns `tools/call` `resultType:"complete"` with the normalized result object in `structuredContent`, exactly one `content` text item containing that same object’s JSON, and `isError` set by the existing application-error convention. `threads_success` is non-error; invalid input, unavailable token, DAP refusal, and DAP protocol error are errors. The B1 object contains only its documented `kind`/success fields or `kind`/`error` fields: it has neither Python `data`/`state` envelope fields nor any raw DAP envelope, `body`, or adapter-error field. `ModernMcp/*Threads*Tests.cs` owns the process-wire assertions, including asserting that `content[0].text` is exactly the native serialization of `structuredContent`, `isError` parity, and the final normalized-`structuredContent` serializer limit—not the duplicated text or outer MCP response—at exactly 262,144 (success) and 262,145 (redacted protocol error) UTF-8 bytes.


## Alternatives
ADR-005 selects an internal typed `GetThreadsAsync` seam. A generic public DAP method and a Python relay are rejected; a combined stack slice is deferred.

## Migration
Parallel additive change: the native catalog gains one tool and changes `ModernProtocolContractTests` from the current nine-tool baseline to the approved ten-tool ordering. There is no native default selector to remove. Existing Python consumers are not selected, modified, or migrated; T05 replays the installed Python journey documented in `specs/006-a1-local-preview/quickstart.md` as non-selection compatibility.

## Requirements-to-files
| Requirement | Work | Files |
|---|---|---|
| B1-REQ-001/002/004/005 | MCP dispatch, exact opaque-token policy, catalog, normalized `tools/call` result wire, and final structured-content serialization boundary | `host/NetCoreDbg.Mcp.Stateless/Program.cs`; `ModernMcp/ModernProtocolContractTests.cs`; new `ModernMcp/*Threads*Tests.cs` (the process-wire and 262,144/262,145 boundary-test owner) |
| B1-REQ-003/004/005/006 | Typed DAP seam, registry-owned `SessionSlot`, cleanup handoff, and reader-failure notification/races | `DebugAdapter/NetCoreDbgSession.cs`; `DebugAdapter/NetCoreDbgSessionContractDriver.cs`; new `DebugAdapter/*Threads*Tests.cs`; new `DebugAdapter/*SessionSlot*Tests.cs` |
| B1-REQ-003/005 | Controlled response modes | `host/NetCoreDbg.Mcp.Stateless.Tests/DebugAdapter/NetCoreDbgSessionContractDriver.cs` (`FixtureConfiguration` and `FixtureProcess.Create` serialize `CONTROLLED_DAP_OPTIONS`); `host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/ModernMcpProcessDriver.cs` (`ModernMcpStartOptions.FixtureConfiguration` is the modern-process test entry); `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs` (`AdapterOptions.Parse` and `ControlledDapAdapter.HandleRequestAsync`) |
| B1-REQ-007 | S2 review source and durable receipt target (not created by this planning slice) | installed source `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md`; primary-repository target `.agent/runs/b1-native-thread-inspection/security-review.md` |

## FULL challenge
The architect FULL challenge was rerun after callable-seam, error, bound, non-selection, catalog, token, and cleanup-order repairs. Final acceptance requires a clean recheck; the reviewer must inspect exact closed-input rejection, unavailable opaque tokens, release-before-close-and-drain handoff, one cleanup winner, atomic token removal/admission closure, the shared drain deadline and forced lease-abort path, and `DisposeAsync`-in-finally. It must inspect explicit stop, host disposal, unusable-session eviction, reader failure (no-inflight, in-flight, and delayed), timeout, protocol error, a hung admitted `get_threads` at drain deadline, and a faulting `StopAsync` as separate cleanup paths; each must prove no transport disposal while a lease is admitted and no Python/cutover surface.

## Milestone
**M1 — native thread inspection:** real modern stdio client starts a controlled session, obtains bounded normalized threads, verifies no raw DAP/error leakage, observes the session still live, stops it, and separately replays retained Python. This is internally shippable but has no release or selector change.

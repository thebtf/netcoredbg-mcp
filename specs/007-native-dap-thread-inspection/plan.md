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
  L->>S: GetThreadsAsync()
  S->>D: threads
  D-->>S: correlated success or failure
  S-->>L: typed operation result
  L-->>H: release lease; cleanup handoff if needed
  H-->>C: threads_success or typed refusal
```

`Program.DebugSessionRegistry` owns closed MCP input, token lookup, result envelopes, and a registry-owned `SessionSlot` for each live token. The `get_threads` schema publishes `debugSessionId` as `string` with `minLength: 1`; registry validation then requires `Trim().Length > 0`, so every short or non-base64url non-whitespace value stays an opaque lookup. An inspection call acquires the slot lease before DAP I/O. If an admitted call detects timeout or protocol error, it records the cause and releases its own lease before it invokes the slot's close-and-drain path; this prevents that call from waiting on itself. Close-and-drain first closes admission, then waits remaining admitted leases, removes the token, and elects one cleanup winner to call `StopAsync` and dispose. Explicit stop, host shutdown, unusable-session eviction, reader failure, timeout, and protocol error use it. `NetCoreDbgSession` notifies the registry on a reader failure even when no request is active; the registry resolves the owning slot and starts the same close-and-drain path. `NetCoreDbgSession` exposes an `internal` immutable typed thread-operation result only to that registry; it is not a generic DAP API. The controlled adapter adds deterministic success, refusal, malformed, wrong-command, timeout, and 256/257-thread, 1,024/1,025-byte-name, and 262,144-byte-success-result boundary modes. `ModernMcpProcessDriver` and `ModernProtocolContractTests` own observable catalog/order and MCP envelopes; `SessionSlot` lifecycle tests own self-handoff and reader-failure notification proof; the controlled adapter and lifecycle tests own correlation/no-generic-surface proof. Python test owners remain unchanged.
The registry rejects a missing arguments object or omitted `debugSessionId` as `invalid_tool_arguments` before token lookup or DAP I/O; it does not treat an absent required field as an unavailable opaque token.

## Alternatives
ADR-005 selects an internal typed `GetThreadsAsync` seam. A generic public DAP method and a Python relay are rejected; a combined stack slice is deferred.

## Migration
Parallel additive change: the native catalog gains one tool and changes `ModernProtocolContractTests` from the current nine-tool baseline to the approved ten-tool ordering. There is no native default selector to remove. Existing Python consumers are not selected, modified, or migrated; T05 replays the installed Python journey documented in `specs/006-a1-local-preview/quickstart.md` as non-selection compatibility.

## Requirements-to-files
| Requirement | Work | Files |
|---|---|---|
| B1-REQ-001/002/004 | MCP dispatch, exact opaque-token policy, catalog | `host/NetCoreDbg.Mcp.Stateless/Program.cs`; `ModernMcp/ModernProtocolContractTests.cs`; new `ModernMcp/*Threads*Tests.cs` |
| B1-REQ-003/004/005/006 | Typed DAP seam, registry-owned `SessionSlot`, cleanup handoff, and reader-failure notification/races | `DebugAdapter/NetCoreDbgSession.cs`; `DebugAdapter/NetCoreDbgSessionContractDriver.cs`; new `DebugAdapter/*Threads*Tests.cs`; new `DebugAdapter/*SessionSlot*Tests.cs` |
| B1-REQ-003/005 | Controlled response modes | `Fixtures/ControlledDapAdapter/{Program.cs,FixtureConfiguration.cs}` |
| B1-REQ-007 | S2 review source and durable receipt target (not created by this planning slice) | installed source `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md`; primary-repository target `.agent/runs/b1-native-thread-inspection/security-review.md` |

## FULL challenge
The architect FULL challenge was rerun after callable-seam, error, bound, non-selection, catalog, token, and cleanup-order repairs. Final acceptance requires a clean recheck; the reviewer must inspect exact closed-input rejection, unavailable opaque tokens, a cleanup-triggering call's release-before-close-and-drain handoff, and stop, host-disposal, unusable-session, reader-failure (no-inflight and in-flight), timeout, and protocol-error races as separate cleanup admission paths.

## Milestone
**M1 — native thread inspection:** real modern stdio client starts a controlled session, obtains bounded normalized threads, verifies no raw DAP/error leakage, observes the session still live, stops it, and separately replays retained Python. This is internally shippable but has no release or selector change.

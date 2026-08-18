# Architecture — M1 Stateless .NET Strangler Decision Record

## Design-depth decision

**D2 parent; D1 amendment.** M1 remains a bounded internal MCP candidate. This
amendment owns one reversible internal lifecycle boundary; it neither redesigns
the legacy relay nor expands M1 into a public migration program.

## Context and disposition

The frozen repository has no native C# DAP lifecycle owner. The existing
`host/NetCoreDbg.Mcp.Host` is an MCP 1.4.1 relay that starts Python and remains
unchanged. It is not the attach point for M1's modern candidate.

| Concern | Disposition | Reason |
|---|---|---|
| Modern MCP front door | **ADOPT** `ModelContextProtocol` v2.1.0 in the new internal candidate | The official SDK owns standard MCP wire, discovery, cache/result, version, and MRTR semantics. |
| Native debugger lifecycle | **OWN** a narrow internal DAP component with BCL `Process`, redirected streams, and `System.Text.Json` | No repository seam owns process lifetime, DAP framing, correlation, and atomic teardown. OmniSharp.Extensions.DebugAdapter.Client 0.19.9 is stale/DI-heavy and lacks that ownership; StreamJsonRpc still leaves DAP framing, DTO, and lifecycle ownership to M1. |
| Legacy Python relay | **RETAIN** unchanged | It remains the published consumer path and comparative parity evidence. |

This is the parent ADOPT-vs-OWN disposition recorded by [ADR-001](../../docs/adr/ADR-001-stateless-dotnet-strangler.md). It replaced the failed premise that an existing native seam must be found.

## D1 amendment packet — owned native DAP lifecycle

**Status:** Implemented on PR #242. Independent native/source review remains useful evidence, not an integration or development gate; the paths below are current ownership, not proposed target paths.

### Boundary contract

| Boundary | Contract |
|---|---|
| Inputs | `netcoredbg` executable path, launch program path, caller cancellation, and bounded initialize/response/stop time limits. |
| Output | One owned `NetCoreDbgSession` with an event-backed coarse state snapshot and async idempotent `StopAsync`/`DisposeAsync`. It exposes no raw adapter stream or generic request surface. |
| Attach point | New executable `host/NetCoreDbg.Mcp.Stateless/` in namespace `NetCoreDbg.Mcp.Stateless`; `host/NetCoreDbg.Mcp.Host` is not referenced, upgraded, or selected. |
| Narrow ownership | `DebugAdapter/NetCoreDbgSession.cs` owns child process, stdin/stdout, framing, outbound sequence allocation, pending-request correlation, state observation, and one shared cleanup task. `DebugAdapter/DapSessionState.cs` owns the coarse-state value. `Program.cs` composes the later MCP candidate with this internal boundary only. |
| Test ownership | T-008 creates sibling `host/NetCoreDbg.Mcp.Stateless.Tests/`, its controlled executable DAP adapter fixture project, and the complete test-side reflection/process contract driver; lifecycle cases live in `DebugAdapter/NetCoreDbgSessionTests.cs`. The driver builds and runs without a production project/reference, launches the fixture, and treats absent future internal assembly/type behavior as runtime assertion failures. T-009 supplies the discoverable production assembly/reference wiring but does not own that suite. Independent T-009 acceptance hardened incomplete discriminators, proved the corrected cases RED against frozen production, then the same final 11-case suite went GREEN. T-003/T-004 own later modern MCP RED tests, and T-005 owns the front door and final command materialization. |
| Does not touch | Python package/entrypoint, `uv.lock`, public selection, legacy host, public protocol contract, attach mode, breakpoints, stacks, evaluate, persistence, auth, generic DAP framework, or a new third-party DAP/JSON-RPC package. |

`NetCoreDbgSession` starts `netcoredbg --interpreter=vscode` as its owned child
process. It writes and reads DAP messages as `Content-Length` frames whose JSON
body is UTF-8; `Content-Length` is the UTF-8 byte count. A single reader parses
frames and completes only the pending request keyed by response `request_seq`.
It must accept an adapter `capabilities` event before the `initialize` response,
record that observation, and still gate launch on the correlated successful
`initialize` response.

The lifecycle is deliberately only: `initialize` response gate → `initialized`
event → `launch` and `configurationDone`, then event-backed coarse states.
`stopped`, `continued`, `exited`, and `terminated` events update the snapshot;
`exited` retains its reported code when present. No breakpoint/configuration,
stack, evaluate, or attach operation is added.

`StopAsync` and `DisposeAsync` share one asynchronous cleanup operation. It
uses the configured bounds to try DAP `terminate`, then `disconnect`, waits for
process exit, and kills the owned process tree only if graceful cleanup exceeds
its bound. Concurrent callers await the same result; the session never starts a
second cleanup or leaves an owned child process intentionally alive.

### Integration points

1. `Program.cs` in the new candidate composes the internal session; it does not
   modify the legacy `host/NetCoreDbg.Mcp.Host/Program.cs` relay composition.
2. The later `start_debug` handler creates and receives a
   `NetCoreDbgSession`; the process-local capability registry owns that
   reference after complete start.
3. The later `get_debug_state` handler reads the session's coarse snapshot;
   `stop_debug` atomically removes its capability before awaiting the shared
   session cleanup.
4. T-009 records only the lifecycle project build/test, controlled-adapter
   readiness, and cleanup receipt after it creates the executable. T-005
   mechanically verifies and materializes the actual candidate launch, C# v2.1.0
   client, environment, cleanup, and `PRODUCT_WORKS` commands after its front door
   is real; no candidate command is inferable from this contract.

### Named test plan and checker commitment

T-002 records this ownership packet. T-008 creates the sibling test project, a controlled
executable DAP adapter fixture project, and complete reflection/process contract driver,
then adds lifecycle RED cases in `DebugAdapter/NetCoreDbgSessionTests.cs`: UTF-8
`Content-Length` byte framing; `request_seq` correlation; a `capabilities` event before
the initialize response with launch still gated on that response;
initialize/initialized/launch/configurationDone ordering; event-backed
stopped/continued/exited/terminated state; and concurrent `StopAsync`/`DisposeAsync`
terminate–disconnect–process-tree-kill fallback. Its `dotnet test
host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj` command builds
and runs every case before production exists: the driver reflects and process-exercises
the future internal assembly/type, so absence is a behavioral contract assertion failure,
never missing project/reference/type compilation. T-009 creates only the production
executable/component and discoverable production assembly/reference wiring. Independent
T-009 acceptance hardened incomplete discriminators, proved the corrected cases RED
against frozen production, then the same final 11-case T-008 suite went GREEN with a
lifecycle-only receipt. T-003 and T-004 add modern MCP RED cases after completed T-009;
T-005 materializes final candidate commands.

**Checker record:** T-006 historically re-derived this amended boundary packet, every `Blocked by` relation, and its Mermaid edge after the maker's work. T-007 consumed that evidence alongside T-001; any subsequent external review is nonblocking evidence and does not block continued development, merge, or an otherwise consumer-proven release. Non-critical later findings are scheduled to a named next patch.
### D1 challenger LITE record

**GO.** M1 needs this boundary because neither legacy relay process ownership
nor MCP SDK adoption supplies DAP framing, correlation, and cleanup ownership.

| Alternative | M1 fit | Dependency and maintenance weight | Process ownership | Framing | Event state | Atomic teardown | Deletion/replacement cost |
|---|---|---|---|---|---|---|---|
| Adopt OmniSharp.Extensions.DebugAdapter.Client 0.19.9 | Does not close M1's owned lifecycle boundary. | Stale/DI-heavy added dependency. | No verified first-class external process/tree owner. | Typed client patterns do not reduce M1 framing work. | Does not supply M1's coarse-state owner. | No unified at-most-once terminate/disconnect/tree cleanup. | Package and composition removal work remains. |
| Adopt StreamJsonRpc | Generic transport, not a DAP lifecycle fit. | Maintained added dependency plus M1-owned DAP work. | Generic transport only. | No DAP `Content-Length` contract. | No DAP event-state contract. | Disposal only; M1 still owns cleanup. | A second future migration removes it. |
| **OWN `NetCoreDbgSession`** | Exact narrow internal M1 boundary. | BCL/`System.Text.Json`; no new package. | Owns `ProcessStartInfo`, streams, descendants. | Owns UTF-8 frames and `request_seq` correlation. | Owns only required coarse events. | One terminate→disconnect→bounded wait→tree-kill task. | Removing candidate removes its bounded component. |

The first two leave material ownership absent or add a dependency without
shrinking it. The narrow BCL component is the smallest shape and excludes
generic DAP functionality, breakpoints, stacks, evaluate, attach, auth, and
persistence.

## Existing M1 decisions retained

M1's MCP wire remains a local stdio candidate using official C#
`ModelContextProtocol` v2.1.0. It implements only `server/discover`,
`tools/list`, and `tools/call`; debugger actions are the ordered cataloged tool
names `start_debug`, `get_debug_state`, and `stop_debug`. Discovery is
server-mandatory but client-optional because request metadata is local to each
request. Discover/list may use official cache fields; ordinary tool results do
not. Runtime validation happens before MRTR or native side effects.

A complete start mints an opaque process-local `debugSessionId`. It is not
connection-bound, listed, persisted, or a current-session inference. State and
stop require it explicitly. `stop_debug` atomically removes the token; one
winner owns the session stop and every concurrent/later caller gets the same
not-found result. This application rule is separate from the session's own
idempotent internal cleanup.

```mermaid
sequenceDiagram
    participant M as M1 MCP handler
    participant S as NetCoreDbgSession
    participant D as netcoredbg --interpreter=vscode

    M->>S: StartAsync(path, program, bounds, cancellation)
    S->>D: spawn; initialize (UTF-8 Content-Length)
    D-->>S: capabilities event (may precede response)
    D-->>S: initialize response(request_seq)
    D-->>S: initialized event
    S->>D: launch; configurationDone
    D-->>S: stopped / continued / exited / terminated
    M->>S: StopAsync or DisposeAsync
    S->>D: terminate; disconnect; bounded exit; kill tree fallback
```

## Scope and rollback

The candidate is selected only for internal M1 evidence. The published Python
console script remains selected for existing consumers. Rollback is
non-selection/removal of the new executable, followed by the unchanged Python
consumer journey. There is no package publication, entrypoint replacement,
persisted state, or client configuration reversal in M1.

The frozen anchors remain `host/NetCoreDbg.Mcp.Host/Program.cs`,
`RelayComposition.cs`, and its test project for legacy-relay evidence only;
`src/netcoredbg_mcp/tools/debug.py` remains Python-only comparative evidence.
Neither is a native C# anchor. T-008 creates the M1 test project, fixture, and
test-side driver; T-009 subsequently creates the candidate source. Independent T-009
acceptance hardened incomplete discriminators, proved the corrected cases RED against
frozen production, then the same final 11-case T-008 lifecycle suite went GREEN.
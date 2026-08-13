# Implementation Plan — Internal Milestone M1 Stateless .NET Strangler

## Decision summary

M1 adds a new internal local stdio candidate using official C#
`ModelContextProtocol` v2.1.0. It does not hand-write standard MCP framing or
upgrade Python's published public path. The only M1 methods are
`server/discover`, `tools/list`, and `tools/call`; debugger actions are
cataloged tool names. Server discovery is mandatory, but client discovery is
optional: valid list/call may be first because metadata is request-local.

The repository has no native C# DAP lifecycle seam. M1 therefore owns a narrow
BCL-only `NetCoreDbgSession` in the new internal candidate, not an existing
relay extension and not a generic DAP framework. The component owns
`netcoredbg --interpreter=vscode`, UTF-8 Content-Length frames, `request_seq`
correlation, lifecycle events, and bounded idempotent cleanup. T-008 owns the
runnable 11-case sibling test project, controlled executable adapter fixture, and
complete reflection/process contract driver that makes its boundary RED; T-009
creates the production component. Independent T-009 acceptance hardened incomplete
discriminators, proved the corrected cases RED against frozen production, then the
same final 11-case T-008 suite went GREEN with a lifecycle-only receipt. T-003/T-004
then create modern RED suites; T-005 materializes verified
candidate commands after it implements the complete MCP front door.

## M1 boundary

| Included | Excluded |
|---|---|
| New internal executable `host/NetCoreDbg.Mcp.Stateless/`; internal `DebugAdapter/NetCoreDbgSession`; sibling test project; discovery/list/call; list/call-first proof; three-tool catalog and schemas; request-local metadata; exact version failure; cache behavior; complete tool envelopes; MRTR elicitation/retry; runtime validation/no-side-effect proof; process-local capability and atomic stop; owned DAP process lifecycle; T-005 command materialization; modern/legacy consumer proof; rollback rehearsal. | Public package release/publication; Python entrypoint/package changes; legacy relay changes/upgrades; remote HTTP; auth/multitenancy; durable recovery; subscriptions; catalog/resource/prompt migration beyond three names; all-tool migration; Python deletion; at… |

## Native lifecycle contract

The authorized production ownership is `host/NetCoreDbg.Mcp.Stateless/`, namespace
`NetCoreDbg.Mcp.Stateless`, under `DebugAdapter/NetCoreDbgSession.cs` and
`DebugAdapter/DapSessionState.cs`. T-008 owns the sibling
`host/NetCoreDbg.Mcp.Stateless.Tests/`, including its controlled executable DAP adapter
fixture project, complete reflection/process contract driver, and
`DebugAdapter/NetCoreDbgSessionTests.cs`; those tests compile independently of the absent
production project. T-009 owns the production project and only the discovery/reference
wiring which lets T-008's lifecycle tests reflect the real internal type; it does not
own that test suite. These are future paths, not existing source claims. The legacy `NetCoreDbg.Mcp.Host` MCP 1.4.1 Python relay
stays unchanged.

`NetCoreDbgSession` accepts debugger/program paths, cancellation, and explicit
time bounds. It starts an owned `netcoredbg --interpreter=vscode` child with
redirected stdio. It reads/writes DAP ASCII `Content-Length` headers and UTF-8
JSON bodies, where length is byte count; assigns outbound `seq`; correlates
responses by `request_seq`; tolerates netcoredbg's `capabilities` event before
the initialize response but does not launch until that successful response.
The launch path is initialize → initialized → launch and, when advertised,
configurationDone. Its coarse state is event-backed by stopped, continued,
exited, and terminated. `StopAsync`/`DisposeAsync` share one asynchronous,
idempotent cleanup: capability-gated terminate, disconnect, bounded exit wait,
then kill the owned process tree only if still necessary.
## TDD and exploration order


1. Characterize unchanged Python parity/rollback behavior and materialize its
   commands (T-001), independently.
2. Establish the D1 owned-lifecycle boundary and record that candidate commands
   are not yet claimable (T-002).
3. Create the runnable lifecycle RED harness: its sibling test project, controlled
   executable adapter fixture, complete reflection/process driver, and lifecycle cases
   (T-008). `dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj`
   runs all cases and reports absent future assembly/type behavior as contract failures,
   not compilation failures.
4. Create the owned narrow production DAP lifecycle component and its minimal
   discovery/reference wiring. Independent T-009 acceptance hardens incomplete
   discriminators, proves the corrected cases RED against frozen production, then the
   same final 11-case T-008 suite goes GREEN; record the lifecycle-only project
   build/test, readiness, and cleanup receipt (T-009).
5. Write modern front-door and capability RED tests (T-003/T-004).
6. Implement the one complete M1 candidate only after both modern RED suites,
   then mechanically verify and materialize its launch, C# v2.1.0 client,
   environment, cleanup, and seven-step `PRODUCT_WORKS` commands (T-005).
7. Run the one independent re-derivation and consumer/rollback evidence
   (T-006/T-007).

T-002 never invents command lines. T-008 owns its executable RED test command;
T-009 owns the post-seam lifecycle build/test receipt only. T-003/T-004's sole
`Blocked by: T-009` relation has no hidden modern-command prerequisite; T-005
owns candidate-command verification and materialization.

## Requirements-to-file map

| Requirement | Tasks | Verified anchor or constrained location |
|---|---|---|
| FR-001–FR-007, NFR-001 | T-003, T-005, T-006 | Authorized future candidate `host/NetCoreDbg.Mcp.Stateless/` and sibling tests; exact front-door files and commands after T-005; legacy host is not modified. |
| FR-008–FR-010, FR-014, NFR-003 | T-003, T-004, T-005, T-006 | Future candidate/capability registry and sibling tests after lifecycle Code; T-005 owns final candidate commands. |
| FR-011, NFR-005 | T-002, T-008, T-009, T-006 | Authorized future `DebugAdapter/NetCoreDbgSession.cs`, `DapSessionState.cs`, and `NetCoreDbgSessionTests.cs`; DAP protocol sources document framing/lifecycle facts. |
| FR-012–FR-013, NFR-002, NFR-004 | T-001, T-005, T-006, T-007 | `quickstart.md` receipt blocks; retained `tests/test_host_proxy.py`, `tests/critical/test_host_proxy_critical.py`, `tests/test_mcp_compliance.py`, `pyproject.toml`, and `uv.lock` remain unmodified. |

## Milestone map

| Milestone | Tasks | Internal shipping sentence | Binding constraints |
|---|---|---|---|
| M1 — Modern stateless walking skeleton | T-001 through T-009 | The current .NET path could not perform a conforming discover/tools/MRTR/native start-state-stop journey; the mergeable internal candidate can, while installed Python remains intact. | No publication/entrypoint/package/legacy-relay change; lifecycle is owned narrowly with BCL; public route/catalog migration is absent. |

M1 is not a public release. A later separately authorized release-prep slice
owns packaging, public installed-surface changes, and publication.

## Additive strangler and rollback

The new candidate owns only the three M1 MCP methods and its corresponding
three-tool catalog. Python owns all public legacy behavior. The legacy C# host
continues to proxy Python and is not a candidate dependency.

Rollback is executable:

1. Stop selecting or remove the M1 candidate.
2. Confirm M1 created no durable state, package publication, entrypoint change,
   or client configuration dependency.
3. Replay the established installed Python consumer journey.
4. Record `PRODUCT_WORKS` as the rollback receipt.

Any proposed change to `pyproject.toml`, `uv.lock`, the Python script, the
legacy relay, or consumer configuration exceeds M1 and returns to architecture.

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
The lifecycle and modern front-door sequence below has been implemented. Recorded source-tree receipts cover the 49-case lifecycle suite and 33-case modern suite; retained-Python and rollback receipts remain separate. Independent native/source review of PR #242 head `2ef00bf0d49a067a35dc301729890a06c56260f7` may continue in parallel, but review availability and non-critical later findings do not block continued development, merge, or an otherwise consumer-proven release; deferred findings belong to a named next patch. These receipts do not themselves extend M1 to publication or public cutover.

## M1 boundary

| Included | Excluded |
|---|---|
| New internal executable `host/NetCoreDbg.Mcp.Stateless/`; internal `DebugAdapter/NetCoreDbgSession`; sibling test project; discovery/list/call; list/call-first proof; three-tool catalog and schemas; request-local metadata; exact version failure; cache behavior; complete tool envelopes; MRTR elicitation/retry; runtime validation/no-side-effect proof; process-local capability and atomic stop; owned DAP process lifecycle; T-005 command materialization; modern/legacy consumer proof; rollback rehearsal. | Public package release/publication; Python entrypoint/package changes; legacy relay changes/upgrades; remote HTTP; auth/multitenancy; durable recovery; subscriptions; catalog/resource/prompt migration beyond three names; all-tool migration; attach; breakpoints; stacks; evaluate; persistence; generic DAP framework; new third-party DAP/JSON-RPC dependency; Python deletion. |

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
## Historical implementation order and current gate

1. T-001 characterized unchanged Python parity/rollback behavior and materialized its commands.
2. T-002 established the owned-lifecycle boundary and T-008 created the runnable lifecycle RED harness.
3. T-009 created the owned lifecycle component; the lifecycle suite then went GREEN.
4. T-003/T-004 created the modern RED suites and T-005 materialized the complete M1 candidate.
5. Recorded re-derivation and consumer/rollback evidence completed the historical T-006/T-007 sequence.
6. Independent native/source review of the exact current PR head may continue in parallel. Its availability is nonblocking; non-critical later findings are scheduled to a named next patch, and no release-prep work is part of M1.

The historical task dependencies remain traceability evidence. They are not instructions to recreate already-implemented work.

## Requirements-to-file map

| Requirement | Tasks | Verified anchor or constrained location |
|---|---|---|
| FR-001–FR-007, NFR-001 | T-003, T-005, T-006 | Implemented candidate `host/NetCoreDbg.Mcp.Stateless/` and sibling tests; external exact-head review is nonblocking evidence, and legacy host is unmodified. |
| FR-008–FR-010, FR-014, NFR-003 | T-003, T-004, T-005, T-006 | Implemented capability registry and sibling tests; external review findings are scheduled to a named next patch when non-critical. |
| FR-011, NFR-005 | T-002, T-008, T-009, T-006 | Implemented `DebugAdapter/NetCoreDbgSession.cs`, `DapSessionState.cs`, and `NetCoreDbgSessionTests.cs`; DAP protocol sources document framing/lifecycle facts. |
| FR-012–FR-013, NFR-002, NFR-004 | T-001, T-005, T-006, T-007 | `quickstart.md` receipt blocks; retained `tests/test_host_proxy.py`, `tests/critical/test_host_proxy_critical.py`, `tests/test_mcp_compliance.py`, `pyproject.toml`, and `uv.lock` remain unmodified. |

## Milestone map

| Milestone | Tasks | Internal shipping sentence | Current delivery state | Binding constraints |
|---|---|---|---|---|
| M1 — Modern stateless walking skeleton | T-001 through T-009 | The current .NET path could not perform a conforming discover/tools/MRTR/native start-state-stop journey; the internal candidate can, while installed Python remains intact. | Implemented on PR #242. External review runs as nonblocking evidence; any non-critical later finding belongs to a named next patch. | No publication/entrypoint/package/legacy-relay change; lifecycle is owned narrowly with BCL; public route/catalog migration is absent. |

M1 is not a public release. A later separately authorized release-prep slice owns packaging, public installed-surface changes, and publication.

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

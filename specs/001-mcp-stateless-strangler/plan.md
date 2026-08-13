# Implementation Plan — Internal Milestone M1 Stateless .NET Strangler

## Decision summary

M1 adds an internal local stdio candidate using official C#
`ModelContextProtocol` v2.1.0. It does not hand-write standard MCP framing or
upgrade Python's published public path. The only M1 methods are
`server/discover`, `tools/list`, and `tools/call`; the debugger actions are
cataloged tool names. Server discovery is mandatory, but client discovery is
optional: valid list/call may be first because metadata is request-local.
`start_debug` demonstrates complete and MRTR input-required paths. Runtime
validation precedes every native side effect. Implementation is blocked until
Explore/Design proves a bounded native C# lifecycle seam and materializes the
candidate command block.

## M1 boundary

| Included | Excluded |
|---|---|
| Discovery/list/call; list/call-first proof; three-tool catalog and schemas; request-local metadata; exact version failure; cache behavior for discover/list; complete tool envelopes; one MRTR elicitation/retry path; runtime validation/no-side-effect proof; process-local capability and atomic stop; command-materialization owners; modern/legacy consumer proof; rollback rehearsal. | Public package release or publication; Python entrypoint/package changes; remote HTTP; auth/multitenancy; durable recovery; subscriptions; catalog/resource/prompt migration beyond the three names; all-tool migration; Python deletion. |

## TDD and exploration order

Project `AGENTS.md` requires observable RED before production edits. The absent
`.agent/guides/TESTING_GUIDELINES.md` is not used as a source. M1 uses this
strict sequence:

1. Characterize unchanged Python parity/rollback behavior and materialize its commands.
2. Prove/reject the bounded native C# lifecycle seam and materialize candidate commands.
3. Write modern first-request/protocol/tool/MRTR/validation RED tests using the accepted seam contract.
4. Write capability/native lifecycle/validation RED tests using that same seam contract.
5. Implement the one complete M1 candidate only after both RED suites.
6. Independently review command-block freshness, then run candidate/legacy consumer evidence and additive rollback rehearsal.

If exploration finds no bounded native C# seam, it returns to architecture.
It does not expand a Code task, create a fake handler, or use Python as a
native substitute.

## Requirements-to-file map

This map has only observed frozen-head anchors. New implementation files are
chosen only after T-002’s accepted seam receipt and C# v2.1.0 API confirmation.

| Requirement | Task | Verified anchor or constrained location |
|---|---|---|
| FR-001–FR-007, NFR-001 | T-003, T-005, T-006 | `host/NetCoreDbg.Mcp.Host/Program.cs`; `RelayComposition.cs`; `NetCoreDbg.Mcp.Host.csproj`; `host/NetCoreDbg.Mcp.Host.Tests/ProductionCompositionTests.cs`; `FakePythonServer.cs`; `DuplexChannel.cs` |
| FR-008–FR-010, FR-014, NFR-003 | T-003, T-004, T-005, T-006 | `host/NetCoreDbg.Mcp.Host/`; `host/NetCoreDbg.Mcp.Host.Tests/`; exact native files only from accepted T-002 receipt |
| FR-011, NFR-004 | T-002; T-001, T-006, T-007 | Repository/vendored C# DAP/process sources plus `quickstart.md` blocks materialized by accepted receipts; `src/netcoredbg_mcp/tools/debug.py` is Python-only comparative evidence |
| FR-012–FR-013, NFR-002 | T-001, T-005, T-006, T-007 | `tests/test_host_proxy.py`; `tests/critical/test_host_proxy_critical.py`; `tests/test_mcp_compliance.py`; `pyproject.toml`; `uv.lock` (retained, not modified in M1) |

## Milestone map

| Milestone | Tasks | Internal shipping sentence | Binding constraints |
|---|---|---|---|
| M1 — Modern stateless walking skeleton | T-001 through T-007 | The current .NET path could not perform a conforming discover/tools/MRTR/native start-state-stop journey; the mergeable internal candidate can, while installed Python remains intact. | No publication/entrypoint/package change; native seam must be proven; public route/catalog migration is absent. |

M1 is not a public release. A later separately authorized release-prep slice
owns packaging, public installed-surface changes, and publication.

## Additive strangler and rollback

The candidate is selected only for its internal M1 journey. The published Python
console script remains selected for existing consumers. .NET owns the three M1
MCP methods and corresponding three-tool catalog; Python owns all public legacy
behavior.

Rollback is executable:

1. Stop selecting the M1 candidate.
2. Confirm M1 created no durable state, package publication, entrypoint change,
   or client configuration dependency.
3. Replay the established installed Python consumer journey.
4. Record `PRODUCT_WORKS` as the rollback receipt.

Any proposed change to `pyproject.toml`, `uv.lock`, the Python script, or
consumer configuration exceeds M1 and returns to architecture.
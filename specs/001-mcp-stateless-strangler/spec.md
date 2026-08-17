---
feature_id: 001
slug: mcp-stateless-strangler
title: "MCP 2026-07-28 Stateless .NET Strangler Milestone M1"
status: IMPLEMENTED
created: 2026-08-13
baseline: main@f74a8439a58ab65c69b947e97efb36716d54ab24
design_rung: D2
release_intent: none
---

# Feature Specification: MCP 2026-07-28 Stateless .NET Strangler Milestone M1

## Purpose and bounded outcome

Milestone M1 authorizes an executable, mergeable internal .NET candidate—not
public package publication. It establishes a conforming MCP 2026-07-28 path
through `server/discover`, `tools/list`, and `tools/call`, with exactly three
native debugger tool names: `start_debug`, `get_debug_state`, and `stop_debug`.
The public Python entrypoint and every other route remain unchanged.

The frozen baseline is `main@f74a8439a58ab65c69b947e97efb36716d54ab24`; its
audit shows the current Python-backed path and installed SDKs are legacy-era.
M1 closes the precise gap: **the current .NET path cannot perform a conforming
discover/tools/MRTR/native start-state-stop journey.**

The candidate implementation is present on PR #242 at `2ef00bf0d49a067a35dc301729890a06c56260f7`. Its recorded source-tree and retained-Python receipts describe the M1 evidence boundary. External PR review may proceed independently, but its availability, delay, or non-critical later findings do not block continued development, merge, or an otherwise consumer-proven release; such findings belong to a named next patch.


## Native lifecycle ownership

There is no existing native C# debugger seam to adopt. T-009 owns the narrow
internal session in proposed future project `host/NetCoreDbg.Mcp.Stateless/`,
namespace `NetCoreDbg.Mcp.Stateless`, under `DebugAdapter/NetCoreDbgSession.cs`
and `DebugAdapter/DapSessionState.cs`. T-008 owns sibling future tests in
`host/NetCoreDbg.Mcp.Stateless.Tests/`, its controlled executable DAP adapter
fixture project, and a complete reflection/process contract driver with cases in
`DebugAdapter/NetCoreDbgSessionTests.cs`. The T-008 suite has no production
project/reference/type compile dependency: its driver launches the fixture and
reflects the future internal assembly/type, making absence a runtime contract
failure. T-009 supplies the real internal type and minimal discovery/reference
wiring without changing the test source/assertions. These are ownership contracts,
not claims that the paths already exist. `host/NetCoreDbg.Mcp.Host` remains the
unmodified legacy MCP 1.4.1 Python relay.

`NetCoreDbgSession` consumes a netcoredbg executable path, program path,
cancellation, and bounded time limits; it returns one owned session with coarse
event-backed state and asynchronous idempotent StopAsync/DisposeAsync. It owns
an external `netcoredbg --interpreter=vscode` process, redirected stdio,
ASCII-header/UTF-8 JSON `Content-Length` framing, outbound sequence and
`request_seq` response correlation, and bounded terminate/disconnect/
process-tree cleanup. The body length is UTF-8 byte length. It must tolerate
netcoredbg's `capabilities` event before the initialize response but launch only
after the correlated successful response. The permitted path is initialize →
initialized → launch → configurationDone when the capability advertises it;
stopped, continued, exited, and terminated events update coarse state. M1 does
not add attach, breakpoints, stacks, evaluate, auth, persistence, generic DAP
framework, or a DAP/JSON-RPC third-party dependency.

T-008 first creates and runs this full test-side RED harness with `dotnet test
host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj`; every named
lifecycle case is collected and fails behaviorally against absent future assembly/type
behavior, never at project/reference/type compilation. T-009 creates the production
project/component, wires discovery/reference without changing test source/assertions,
and makes the lifecycle suite GREEN. T-003/T-004 then add modern RED tests; T-005
implements the MCP front door and materializes candidate commands.

## Wire boundary

| MCP method | M1 behavior |
|---|---|
| `server/discover` | The server MUST implement it, declare tools, and return a cacheable result. A client MAY call it. |
| `tools/list` | Cacheable deterministic catalog with exactly the three debugger tools in listed order. |
| `tools/call` | Invokes one cataloged tool through `params.name` and `params.arguments`; no tool action is an MCP method. |

Every request carries its own protocol version and client capabilities in
`_meta`; the server retains no ordering/session prerequisite. A client MAY send
any valid supported modern RPC as its first request. The M1 stdio acceptance
journey deliberately sends `server/discover` first only as a compatibility and
proof probe. Separate RED/GREEN cases prove valid `tools/list` and valid
`tools/call` each succeed as a fresh candidate process's first request without
prior discovery. `server/discover` and `tools/list` use official cacheable
result behavior with correct `ttlMs` and `cacheScope`; ordinary `tools/call`
results do not acquire cache fields.

## User scenarios and testing

### User Story 1 — Use a modern RPC without a handshake prerequisite (Priority: P1)

A current client may call discovery, list, or a valid tool call first against a
fresh local stdio candidate. It is not required to call discovery before list
or call because each request supplies its own metadata.

**Independent test:** Real C# v2.1.0 SDK process exchanges prove the server
implements discovery and fresh processes succeed when discovery, list, or call
is first.

### User Story 2 — Start through a supplied program or MRTR input (Priority: P1)

A client invokes `tools/call` with `name: "start_debug"`. A supplied valid
`program` can complete native start. With no program and supported form
elicitation, it receives official `InputRequiredResult` containing an
`elicitation/create` program request. It retries in a **new** `tools/call`
request with a new JSON-RPC id, repeated arguments, and official
`inputResponses`; M1 emits no `requestState`.

**Independent test:** A real SDK client proves complete and input-required
branches, new-id retry, no `requestState`, and no server-initiated request.

### User Story 3 — Reject invalid tool inputs before native action (Priority: P1)

The advertised application input schemas are runtime-enforced before native
launch, state read, or native stop—not merely published metadata.

**Independent test:** A process-level tool-call test inspects exact public
error classes and observable native-action evidence.

### User Story 4 — Use a local explicit debugger capability safely (Priority: P1)

After complete start, state and stop are `tools/call` operations that pass
`debugSessionId` in `params.arguments`.

**Independent test:** Process-level tests prove explicit token use,
interleaving during one live candidate process, no creator/client/connection
ownership, atomic concurrent stop, and uniform unusable token behavior.
Client-close behavior is verified separately as stdio candidate shutdown, not
debugger-capability survival.

### User Story 5 — Retain the installed Python consumer (Priority: P1)

The existing consumer continues to install and launch `netcoredbg-mcp` while
M1's internal candidate is developed and removable.

**Independent test:** The retained installed Python journey and M1 candidate
journey separately reach `PRODUCT_WORKS`; removing candidate selection then
replays the exact retained Python command.

## Tool inputs and results

| Tool | Runtime-enforced `params.arguments` | Result rule |
|---|---|---|
| `start_debug` | Object: optional non-empty `program`; no additional fields. Retry uses official request-level `inputResponses`. | Complete success has `structuredContent.kind: start_debug_success`; invalid input is `invalid_tool_arguments`; absent program/no elicitation is deterministic application error. |
| `get_debug_state` | Object: required `debugSessionId` with minimum length; no additional fields. | Complete success has `kind: debug_state_success`; missing/short/malformed and unusable handles yield uniform not-found. |
| `stop_debug` | Object: required `debugSessionId` with minimum length; no additional fields. | Complete success has `kind: stop_debug_success`; invalid extra fields are invalid arguments; bad/unusable handles yield uniform not-found. |

Complete tool calls are official `CallToolResult` values with `resultType:
complete`, content, structured content, and applicable `isError`. Application
schemas remain in [`contracts/modern-front-door.schema.json`](./contracts/modern-front-door.schema.json).

## Requirements

- **FR-001 Discovery availability:** The server MUST implement cacheable
  `server/discover` and declare tools. It MUST NOT require discovery or retain
  a request-order/session prerequisite; valid list or tool calls may be first.
- **FR-002 Request locality:** Each request's `_meta` alone supplies version and
  capabilities; no handshake, connection, or prior request is context.
- **FR-003 Version error:** Unsupported version returns `-32022` with exactly
  `requested` and `supported` typed data.
- **FR-004 Catalog:** Cacheable `tools/list` contains exactly three ordered M1
  names and declared input schemas.
- **FR-005 Tool envelope:** Complete tool calls use official `CallToolResult`
  content, structured content, applicable `isError`, and no cache fields.
- **FR-006 MRTR start:** Absent program plus form elicitation yields official
  `InputRequiredResult` with program elicitation, no `requestState`, and a
  new-id retry using repeated arguments plus `inputResponses`.
- **FR-007 Missing capability:** Absent program without form elicitation yields
  deterministic complete application error, never a server request/invented
  protocol error.
- **FR-008 Explicit capability:** Complete start mints opaque process-local
  `debugSessionId`; state/stop receive it explicitly. No listing, current
  session, connection ownership, or existence oracle exists.
- **FR-009 Capability lifetime/races:** Token lifetime is bounded by its live
  debugger and candidate host process. While that process is live, the registry
  MUST NOT record or require creator, client, or connection identity; every
  later request resolves only its explicit token. Under the stdio transport,
  closing the client ends the single-session candidate host, so no token
  survival or transfer after client close is required or observable. Atomic
  removal permits one native stop and one success; concurrent/later callers
  receive not-found.
- **FR-010 Uniform handle failure:** Random/malformed, stopped/closed,
  native-unavailable, and prior-process handles are the same complete
  `DEBUG_SESSION_NOT_FOUND` application error.
- **FR-011 Owned native lifecycle:** M1 MUST own the bounded internal
  `NetCoreDbgSession` contract above; no missing component may be described as
  an existing seam. It owns process/streams/framing/correlation/event state and
  time-bounded idempotent cleanup, while excluding generic DAP functionality.
- **FR-012 Legacy isolation:** Python script, package constraint, legacy relay,
  and non-M1 routes stay unchanged and Python-owned.
- **FR-013 Parity/rollback:** Candidate and installed Python journeys separately
  reach `PRODUCT_WORKS`; candidate removal replays Python without reversal.
- **FR-014 Runtime input validation:** Before native side effects, runtime MUST
  reject empty/extra tool arguments as complete `invalid_tool_arguments` with
  `isError: true`; missing/short/malformed handle remains uniform not-found.

## Non-functional requirements

- **NFR-001:** stdout contains only MCP frames; diagnostics use stderr.
- **NFR-002:** M1 has no remote listener, auth, tenancy, durable recovery,
  package publication, or public entrypoint cutover.
- **NFR-003:** Interleaving reveals no connection ownership and native stop is
  at most once.
- **NFR-004:** T-009 records its lifecycle-only project build/test, readiness,
  and cleanup receipt; T-005 materializes the exact candidate command block and
  T-001 the retained-Python block before T-007. Stale or absent final receipts
  prevent consumer evidence.
- **NFR-005:** The internal DAP component has UTF-8 byte-correct
  `Content-Length` framing, correlated response gating, event-backed coarse
  state, and one bounded async cleanup owner for terminate/disconnect/process
  tree fallback.

## Success criteria

- **SC-001:** Real SDK process tests succeed when discovery, list, and valid
  call are separately the first fresh-process request.
- **SC-002:** T-008 RED precedes T-009 seam Code; T-003/T-004 modern RED precede
  T-005 MCP implementation.
- **SC-003:** Cache tests distinguish discovery/list from ordinary tool calls.
- **SC-004:** Invalid argument and uniform not-found results are exact and prove
  zero prohibited native side effects.
- **SC-005:** Lifecycle tests prove byte framing, response gating, state events,
  and exactly one bounded cleanup owner.
- **SC-006:** Both consumer journeys reach `PRODUCT_WORKS`, candidate-removal
  rollback succeeds, and T-005's exact modern command block exists.

## Exclusions and uncertainty

M1 excludes remote HTTP, auth/multitenancy, durable recovery, subscriptions,
resource/prompt/catalog migration beyond the three tools, all-tool migration,
Python deletion, legacy-relay changes, package cutover, publication, attach,
breakpoints, stacks, evaluate, persistence, generic DAP framework, and a new
third-party DAP/JSON-RPC dependency. Python v2.0.0 is comparative evidence;
M1 adopts C# v2.1.0 without upgrading Python. T-009 must not claim a candidate
MCP launch or client command; T-005 materializes those post-front-door outputs.

See [`checklists/requirements.md`](./checklists/requirements.md) for
requirement-to-acceptance-to-task mapping.

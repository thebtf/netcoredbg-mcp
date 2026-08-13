---
feature_id: 001
slug: mcp-stateless-strangler
title: "MCP 2026-07-28 Stateless .NET Strangler Milestone M1"
status: READY_FOR_INDEPENDENT_RECHECK
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
proof probe. Separate RED/GREEN cases must prove valid `tools/list` and valid
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
implements discovery and that fresh processes succeed when their first valid
request is discovery, list, or call, respectively.

**Acceptance scenarios:**

1. **Given** supported metadata on `server/discover`, **when** a client chooses
   it as its first request, **then** discovery declares tools and has correct
   cacheable result fields.
2. **Given** supported metadata on a fresh process, **when** `tools/list` is
   the first request, **then** it succeeds without prior discovery and yields
   the deterministic cached three-tool catalog.
3. **Given** supported metadata and valid `start_debug` arguments on a fresh
   process, **when** `tools/call` is the first request, **then** it succeeds
   without prior discovery and returns the applicable official tool result.
4. **Given** an unsupported protocol version, **when** any M1 method is called,
   **then** it receives JSON-RPC `-32022` data with exact `requested` and
   `supported` fields.

### User Story 2 — Start through a supplied program or MRTR input (Priority: P1)

A client invokes `tools/call` with `name: "start_debug"`. A supplied valid
`program` can complete native start. With no program and supported form
elicitation, it receives official `InputRequiredResult` containing an
`elicitation/create` program request. It retries in a **new** `tools/call`
request with a new JSON-RPC id, repeated arguments, and official
`inputResponses`; M1 emits no `requestState`.

**Independent test:** A real SDK client proves complete and input-required
branches, new-id retry, no `requestState`, and no server-initiated request.

**Acceptance scenarios:**

1. **Given** valid `program` in `params.arguments`, **when** start is called,
   **then** it returns complete `CallToolResult` content, structured content,
   and applicable `isError`.
2. **Given** no program and form elicitation capability, **when** start is
   called, **then** it returns official `resultType: input_required` with an
   `elicitation/create` input request and no `requestState`.
3. **Given** that input-required result, **when** a new request repeats the
   arguments and supplies `inputResponses`, **then** start completes without a
   server-initiated request.
4. **Given** no program and no form elicitation capability, **when** start is
   called, **then** it returns a deterministic complete application error.

### User Story 3 — Reject invalid tool inputs before native action (Priority: P1)

The advertised application input schemas are runtime-enforced before native
launch, state read, or native stop—not merely published metadata.

**Independent test:** A process-level tool-call test inspects exact public
error classes and an observable native-action counter/fixture state.

**Acceptance scenarios:**

1. **Given** `start_debug` arguments containing `program: ""` or an unknown
   field, **when** called, **then** it returns complete `CallToolResult` with
   `isError: true` and `structuredContent.kind: invalid_tool_arguments`, with
   neither native launch nor MRTR.
2. **Given** `get_debug_state` or `stop_debug` arguments containing an unknown
   field, **when** called, **then** each returns the same invalid-arguments
   class and performs no native action.
3. **Given** missing, short, or malformed `debugSessionId`, **when** state or
   stop is called, **then** each returns uniform `DEBUG_SESSION_NOT_FOUND` and
   performs no native state/stop action.

### User Story 4 — Use a local explicit debugger capability safely (Priority: P1)

After complete start, state and stop are `tools/call` operations that pass
`debugSessionId` in `params.arguments`.

**Independent test:** Process-level tests prove explicit token use,
interleaving, disconnect survival, atomic concurrent stop, and uniform unusable
token behavior.

**Acceptance scenarios:**

1. **Given** a valid token, **when** independent/interleaved state calls supply
   it, **then** they read the same live debugger without connection affinity.
2. **Given** concurrent stop calls for that live token, **when** they race,
   **then** exactly one removes it, stops native debugger once, and succeeds;
   every loser returns uniform not-found.
3. **Given** random/malformed, stopped/closed, native-unavailable, or
   prior-process token, **when** state/stop is called, **then** each yields the
   identical complete `DEBUG_SESSION_NOT_FOUND` application error.

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
schemas are in [`contracts/modern-front-door.schema.json`](./contracts/modern-front-door.schema.json).
Official SDK/schema types own `InputRequiredResult`, `inputResponses`,
`elicitation/create`, cache fields, and `-32022`.

## Requirements

- **FR-001 Discovery availability:** The server MUST implement cacheable
  `server/discover` and declare tools. It MUST NOT require discovery or retain
  a request-order/session prerequisite; valid list or tool calls may be first.
- **FR-002 Request locality:** Each request's `_meta` alone supplies version and
  capabilities; no handshake, connection, or prior request is context.
- **FR-003 Version error:** Unsupported version returns `-32022` with exactly
  `requested` and `supported` typed data.
- **FR-004 Catalog:** Cacheable `tools/list` contains exactly three ordered M1
  names and the declared input schemas.
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
  debugger and host process. Creator disconnect does not stop it. Atomic removal
  permits one native stop and one success; concurrent/later callers not-found.
- **FR-010 Uniform handle failure:** Random/malformed, stopped/closed,
  native-unavailable, and prior-process handles are the same complete
  `DEBUG_SESSION_NOT_FOUND` application error.
- **FR-011 Native seam gate:** Native Code remains blocked until an
  Explore/Design receipt proves a bounded C# lifecycle seam; Python `debug.py`
  is not a native anchor.
- **FR-012 Legacy isolation:** Python script, package constraint, and non-M1
  routes stay Python-owned and unchanged.
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
- **NFR-004:** Exact candidate and retained-Python command blocks are
  materialized by accepted T-002/T-001 receipts before T-007; stale or absent
  blocks prevent consumer evidence.

## Success criteria

- **SC-001:** Real SDK process tests succeed when discovery, list, and valid
  call are each separately the first fresh-process request.
- **SC-002:** RED tests precede code for wire/MRTR, input validation, and
  capability lifecycle behavior.
- **SC-003:** Cache tests distinguish discovery/list from ordinary tool calls.
- **SC-004:** Invalid argument and uniform not-found results are exact and prove
  zero prohibited native side effects.
- **SC-005:** Both consumer journeys reach `PRODUCT_WORKS`, candidate-removal
  rollback succeeds, and exact T-001/T-002 materialized command blocks exist.

## Exclusions and uncertainty

M1 excludes remote HTTP, auth/multitenancy, durable recovery, subscriptions,
resource/prompt/catalog migration beyond the three tools, all-tool migration,
Python deletion, package cutover, and publication. Python v2.0.0 is comparative
evidence; M1 adopts C# v2.1.0 without upgrading Python. The native C# lifecycle
seam and exact candidate command remain T-002 exploration outputs, not current
implementation assumptions.

See [`checklists/requirements.md`](./checklists/requirements.md) for
requirement-to-acceptance-to-task mapping.
# Feature Specification: Safe Read-Only Stateless Preview

**Feature Branch:** `work/stateless-preview-a1`

**Status:** Proposed

**Parent:** `.agent/runs/python-removal-strangler-program-v1/stateless-convergence-program-v4.md`

## User Scenarios & Testing

### User Story 1 — Search a chosen local project safely (Priority: P1)

An opt-in developer needs a self-contained preview that can discover one modern
MCP tool and find a C# symbol inside the project they explicitly selected,
without replacing their existing Python command.

**Independent test:** Download the source-pinned preview artifact, verify its
manifest, launch it with a valid `--project`, call discover/list/call, obtain a
deterministic root-relative symbol result, close stdin, and replay Python after
removing preview selection.

### User Story 2 — Receive deterministic refusals at the containment boundary (Priority: P2)

A security-conscious developer needs malformed roots, path escapes, invalid
arguments, resource exhaustion, and excluded routes to fail predictably without
reading an unintended file or emitting partial search output.

**Independent test:** Execute every launch/configuration/fixture/tool/resource/
protocol row in the required negative matrix and observe only its named exit,
JSON-RPC, or complete application-error outcome.

### User Story 3 — Promote precisely the reviewed artifact (Priority: P3)

A release owner needs to publish only the source-run bytes already proven and
approved, then recover safely from an interrupted release without mutating a
tag or overwriting an asset.

**Independent test:** Bind S4 to a T07 run/commit/hashes, promote its matching
artifact, download remote bytes to prove identity, and exercise each legal
state-machine recovery state with a non-destructive fixture or API seam.

---

## Goal

Deliver an opt-in, consumer-installable Windows x64 artifact proving that the
selected Stateless host family safely serves one root-constrained native route.
Python `netcoredbg-mcp` remains the default route and rollback oracle.

## Process and distribution contract

- Preview versions are exactly `<major>.<minor>.<patch>-preview.<n>`, with
  non-negative decimal major/minor/patch and positive decimal `n`.
- Preview tag is exactly `stateless-preview-v<version>`; release assets are
  `netcoredbg-mcp-stateless-preview-win-x64-<version>.zip` and sibling
  `netcoredbg-mcp-stateless-preview-win-x64-<version>.manifest.json`.
- A dedicated `stateless-preview.yml` has two manual modes. **Build** pins an
  exact source SHA and stores archive+manifest as a retained Actions artifact.
  The downloaded run artifact—not a local rebuild—is the only candidate used
  for full client/matrix/rollback proof and S2/S3 review.
- S4 approval binds the already-proven build run ID, commit, preview tag,
  archive SHA-256, manifest SHA-256, executable SHA-256, and prerelease
  destination. Promotion downloads and verifies exactly those retained bytes;
  it never calls `dotnet publish` or substitutes a new artifact.
- First promotion proves tag/release absence, pushes an annotated tag at the
  approved commit, creates a draft prerelease, uploads the two exact assets,
  verifies their remote bytes, and publishes. The persisted remote state
  machine in `contracts/promotion-state-machine.md` distinguishes first use,
  partial recovery, and complete success.
- A recovery may proceed only if an existing annotated tag resolves to the
  approved commit and any release is the approved draft or complete prerelease
  with matching asset metadata/bytes. It uploads only missing exact assets;
  mismatched target/tag/release/asset, expired source artifact, or an incomplete
  non-draft release is a hard refusal. It never overwrites/deletes/moves a tag.
- The workflow never calls Python `publish.yml`, produces a wheel, calls PyPI,
  or modifies the default selector. A GitHub release asset is consumer durable;
  an Actions artifact is only build-to-promotion transport.

## Launch authority

The executable requires exactly one `--project <path>` and no other root source.
It rejects omitted/multiple/relative/missing/non-directory/UNC/network/device/
extended/volume-GUID/reparse roots before MCP starts: exit `64`, write exactly
`PREVIEW_ROOT_INVALID\n` to stderr, and write zero bytes to stdout. A selected
ordinary worktree root is valid. Environment variables, client roots, CWD,
home expansion, URI authority, debugger, bridge, artifact, Python, setup,
downloader, mux, HTTP, and remote configuration cannot alter it.

The canonical root must be local drive-letter, absolute, and non-reparse.
Every entered directory, root `.gitignore`, and opened `.cs` file is regular,
non-reparse, lexically beneath that root, and has a final target beneath it.
A contained entry escaping to a sibling worktree is refused before its contents
are read. Hostile concurrent mutation of a trusted operator-owned tree is out
of A1 scope until handle/file-ID verification exists; observable attribute,
identity, open, or read failure fails the call without a partial result.

## Modern MCP contract

Every request contains `_meta` with exactly these required MCP fields:

```json
{
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientInfo": {"name": "non-empty", "version": "non-empty"},
  "io.modelcontextprotocol/clientCapabilities": {}
}
```

The server reads that request-local metadata only; it retains none. A fresh
process accepts `server/discover`, `tools/list`, or valid `tools/call` first.
Unsupported versions return JSON-RPC error `-32022`, message `Unsupported
protocol version`, and data exactly `{ "requested": <request value>,
"supported":["2026-07-28"] }`. The legacy `initialize` method is not
implemented.

`tools/list` is deterministic and returns exactly one definition:
`find_code_symbol`, annotations `readOnlyHint:true`, `idempotentHint:true`,
`openWorldHint:false`, input schema below, `resultType:"complete"`, no cursor,
`ttlMs:300000`, and `cacheScope:"public"`. No DAP, Native Scene, prompt,
resource, artifact, bridge, Python, mux, HTTP, or configuration tool is
registered/dispatchable.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["name"],
  "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 256},
    "kind": {"type": ["string", "null"], "enum": ["class", "method", "property", "field", null]}
  }
}
```

Whitespace-only `name` is invalid despite JSON Schema `minLength`. Unknown
`tools/call` names produce only the modern text error `Unknown tool: <name>`;
excluded protocol methods return JSON-RPC method-not-found and have no side
effect.

## Exact tool result contract

The sole text block is the compact UTF-8 JSON serialization of
`structuredContent`, without leading/trailing whitespace and with properties in
the declared order. A success is exactly this closed structure:

```json
{
  "kind": "find_code_symbol_success",
  "results": [
    {"file":"relative/forward-slash.cs","line":1,"name":"exact input","kind":"class","context":"trimmed source line"}
  ]
}
```

`file` is root-relative forward slash path; `line` is a positive integer;
`kind` is the matched enum; `context` is the trimmed source line truncated at
512 Unicode scalar values without splitting a surrogate pair or appending an
ellipsis. `results` is sorted ordinally by `file`, then `line`, then `kind`.

All application errors have `resultType:"complete"`, `isError:true`, one text
block equal to `structuredContent`, and exactly `{kind,error,tool}` in that
order, with `tool:"find_code_symbol"`:

| `kind` | `error` | Trigger |
|---|---|---|
| `invalid_tool_arguments` | `INVALID_TOOL_ARGUMENTS` | Schema, type, whitespace, or enum violation. |
| `preview_path_refused` | `PREVIEW_PATH_REFUSED` | Reparse, outside-root, or sibling-worktree escape discovered during call. |
| `preview_search_unreadable` | `PREVIEW_SEARCH_UNREADABLE` | Attribute/identity/open/read failure after launch validation. |
| `preview_search_budget_exceeded` | `PREVIEW_SEARCH_BUDGET_EXCEEDED` | Any defined resource ceiling or server deadline. |

No application error contains a root path, environment value, filesystem
exception, partial result, or budget counter.

## Search budget semantics

A `tools/call` validates arguments before any filesystem access. The server then
allows at most 2,048 directories entered (root counts as one); 20,000
non-directory entries examined before extension/ignore filtering; 1 MiB per
opened `.cs` or root `.gitignore`; 16 MiB aggregate bytes opened; 128 matching
results; and a 256 KiB UTF-8 complete JSON-RPC response frame including text
and structured content. A count reaches its limit before the next unit of work;
a 129th match is an error, never a partial/truncated success. The five-second
server deadline starts with the first filesystem operation after validation;
it maps to `PREVIEW_SEARCH_BUDGET_EXCEEDED`. Client cancellation propagates as
MCP cancellation and returns no tool result. Any path or budget refusal clears
accumulated results before response construction.

## Required negative/containment matrix

| Surface | Case | Observable outcome |
|---|---|---|
| Launch CLI | omitted/multiple/relative/missing/non-directory/UNC/device/reparse root | Exit 64, exact stderr, zero stdout, no root content read. |
| Launch configuration | hostile CWD/env/client roots/URI | Selected `--project` only; no alternate root read. |
| Contained fixture | reparse/outside-root/sibling-worktree target | `PREVIEW_PATH_REFUSED`, no target content/output partial. |
| Tool input | missing/null/non-string/whitespace/257-unit name; extra property; invalid kind | `INVALID_TOOL_ARGUMENTS` closed error. |
| File system | inaccessible/changed attribute/identity/open/read path | `PREVIEW_SEARCH_UNREADABLE`, no partial. |
| Resources | every count/size/result/response/deadline ceiling | `PREVIEW_SEARCH_BUDGET_EXCEEDED`, no partial. |
| Protocol/catalog | legacy initialize, forbidden method, forbidden tool/config name | Method-not-found or text-only unknown-tool; no process/file side effect. |
| Transport | EOF/cancellation | Bounded exit / no cancellation result; no retained state. |
| Valid journey | contained `.cs` symbol | Exact schema/result, stdout-only JSON-RPC, deterministic order. |
| Rollback | remove preview opt-in | Unchanged installed Python journey is `PRODUCT_WORKS`. |

## Functional Requirements

| ID | Requirement |
|---|---|
| A1-REQ-001 | Separate preview artifact/workflow/tag/release asset namespace is mechanically disjoint from Python/PyPI/default selection. |
| A1-REQ-002 | Exactly one modern read-only tool and compile-time closed catalog. |
| A1-REQ-003 | Explicit strict launch root and final-target containment before filesystem reads. |
| A1-REQ-004 | Shared traversal/matching engine with explicit legacy/preview policy seam. |
| A1-REQ-005 | Exact bounded schema, response/error, ordering, cache, and refusal contract. |
| A1-REQ-006 | The exact source-run archive is proved before S4, then its promoted remote copy proves byte identity, consumer install, shutdown, and Python rollback. |
| A1-REQ-007 | S2/S3 evidence is on the exact source-run bytes before S4; S4 binds and promotes only those bytes through the resumable immutable state machine. |

## Success Criteria

- **SC-001:** A valid source-run artifact completes User Story 1 with exactly
  one listed tool, a valid result, stdout-only JSON-RPC, and bounded EOF exit.
- **SC-002:** Every required negative-matrix trial produces its one named
  refusal with zero unintended file/process effect and no partial result.
- **SC-003:** The five named compatibility-host/Python parity owners remain
  green after the traversal-core extraction.
- **SC-004:** Before S4, T07/T08 evidence names the same build run, source SHA,
  archive, manifest, and executable hashes that the approval record binds.
- **SC-005:** A published preview's remote archive/manifest bytes equal the
  T07 evidence bytes; Python rollback remains `PRODUCT_WORKS`.

## Non-goals

PyPI/default selector changes; Python retirement; DAP, Native Scene, UI, bridge,
artifacts, prompts, resources, subscriptions, mux, HTTP, remote transport,
non-Windows assets, signing/SBOM, setup, and downloader.

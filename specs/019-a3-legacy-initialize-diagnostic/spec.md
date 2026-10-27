# A3 — Actionable Modern-Only Initialize Diagnostic

Status: source implemented and locally verified. Original preview run passed 170/170; one near-cap case was replaced by two focused exact-boundary passes, giving a disjoint 171-case coverage set, not a full 171 rerun. All 129 artifact semantic cases, the real new runner on verified extracted bytes and installed-Python rollback passed. Independent error-to-wire assurance verified 8/8 clauses after its test-only F1 correction. Canonical/public 18-case delivery remains pending.
Baseline: `371b58201cad388eab79586dc40ac6c389d4fdac` (documentation-only successor to delivered preview.2 source `cf39c85cfd99cc2a51f67b7d2686f29e3e8080c0`).
Parent: Program A/A3 modern compatibility policy. Python remains default and rollback.

## Caller outcome

A legacy stdio client sends `initialize` as its first request without modern metadata. The modern-only preview refuses with correlated `-32601`, and the error message names its sole supported protocol version `2026-07-28` and directs the caller to `server/discover` plus per-request metadata. This is actionable rejection, not a legacy handshake or automatic migration. A corrected modern call succeeds in the same process; clean EOF still exits.

## Evidence and decision

Root exercised the actual hash-verified published preview.2 executable through the existing `JsonRpcProcess` and `PreviewSearchApp`: first legacy initialize returned `-32601 Method not found`; modern discovery succeeded; subsequent initialize was rejected by SDK 2.3 with a message naming `2026-07-28`; a corrected symbol call succeeded and EOF was clean. Evidence: `.agent/runs/a3-required-metadata-version-20261027/legacy-diagnostic-qualification.json`. The SDK prepends its metadata filter before application filters; the initial and established-modern paths must not be conflated.

D1: one reversible error-diagnostic boundary inside the existing modern-only process; no subsystem, transport, dependency or authority change. Current official versioning policy says modern-only initialize errors SHOULD name supported versions; exact error code is implementation-defined. We retain existing `-32601` rather than introduce legacy support or replace SDK errors.

## Contract

- For an initial valid JSON-RPC `initialize` request without modern metadata, return the original numeric/string ID, code `-32601`, and message `Only MCP 2026-07-28 is supported. Use server/discover and per-request metadata instead.` No success result or session-handshake state is created.
- The local initial diagnostic and its allocation-bounded size preflight must describe the same complete serialized JSON response. Preserve the 256 KiB JSON-byte limit excluding LF; an unrepresentable correlated error closes through the existing owner without oversized or partial output.
- Keep the SDK-owned established-modern initialize diagnostic unchanged. It must continue to name `2026-07-28`; do not intercept, translate or fabricate its metadata context.
- Initial diagnostic is followed by valid discovery and exact `PreviewMarker` tool success in the same process. A further initialize still refuses through the SDK; another valid tool call and clean EOF prove continued usability.
- Excluded non-initialize method errors retain their current `-32601 Method not found` code/message and exact-frame behavior. The shared estimator must not accidentally change those consumers.
- Public policy remains modern-only: modern and dual-era clients using modern requests work; legacy-only clients receive actionable refusal, not fallback. Discovery continues to advertise exactly `2026-07-28`, tools-only capabilities with `listChanged:false`, three ordered read-only tools and no logging capability. Client capabilities/roots do not expand the launch-pinned filesystem root. These are existing fixed boundaries, not new full-schema validation.

## Artifact and delivery

Add `legacy-initialize-supported-version-diagnostic` immediately before `rollback-python-default` to the immutable required-metadata successor's 17 ordered scenarios. The new fixed generation is `a3-preview-legacy-diagnostic-v1` with 18 scenarios and canonical SHA-256 `31589f89efc7c0453357bd3cee7216448ec1332e57d135d555b2c897a0bc855d`, computed through the existing serializer over the ordered descriptors including final LF. Preserve every historical A1/A2/optional-A3/required-metadata-A3 definition/hash and the default historical A1 catalog API. Current producers require the new complete generation; a valid old17 proof cannot satisfy a new candidate.

Use the existing publisher and schema versions, canonical exact-source build, retained18/Python rollback, candidate-bound review, explicit Root S4, same-byte publication, fresh public18/Python rollback and R0-R3 closeout. No unknown candidate/tag is preapproved. Prior preview.2 remains delivered and immutable.

## Explicit non-goals

No legacy handshake support, version normalization, multi-version negotiation, full capability-schema or malformed-envelope/parse-error policy, new methods/tools, HTTP/mux/session manager, runtime servicing, Python/default switch, Program B/C or Python retirement. `runtime8-security-servicing` and broader A3 remain open.

Primary source, read 2026-10-27: [modern-only compatibility and initialize diagnostics](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning.md). Exact selected SDK source: `modelcontextprotocol/csharp-sdk@9a327829ef066c44a0d737c58bf3c7617d7c701c`, `McpServerImpl.cs` metadata-filter ordering and `ValidateInitializeRequestBoundary`.

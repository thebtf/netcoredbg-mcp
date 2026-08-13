# Quickstart — Milestone M1 Acceptance Journeys

This is an execution contract for a future internal candidate, not a public
release claim. Exact commands are intentionally absent until accepted receipts
materialize them: T-002 owns the candidate block; T-001 owns retained-Python
and rollback blocks. T-007 MUST refuse consumer evidence if any block below is
absent, stale, or not traceable to its accepted receipt.

## 1. Candidate command block — owned by T-002

T-002 updates this section before T-003–T-005 are claimable. Its accepted seam
receipt MUST record the exact candidate project/invocation boundary, build
command, candidate launch command, C# v2.1.0 client/fixture command, required
environment, and cleanup. No command is guessed in this package.

Once materialized, the candidate journey must:

1. Deliberately send `server/discover` first as the compatibility/proof probe;
   assert tools capability and correct cacheable `ttlMs`/`cacheScope`.
2. Separately start fresh candidate processes where valid `tools/list` and valid
   `tools/call(start_debug, valid program)` are first requests. Both must
   succeed without prior discovery.
3. Assert ordered tool catalog/schema and complete start result content,
   structured content, and applicable `isError`.
4. Exercise MRTR: no-program plus form elicitation returns input-required
   elicitation with no requestState; a new-id retry with repeated arguments and
   inputResponses completes. No-program without capability is a deterministic
   complete application error.
5. Exercise runtime validation before native actions: empty program or extra
   tool fields returns complete `invalid_tool_arguments`, `isError: true`, and
   zero prohibited native side effects. Missing/short/malformed handles return
   uniform `DEBUG_SESSION_NOT_FOUND`, also with zero native state/stop action.
6. While token is live, run state and independent/interleaved state. Race the
   first two stops, then verify post-race state and a prior-process token.
7. Verify stdout frame purity and record `PRODUCT_WORKS`.

## 2. Retained Python command block — owned by T-001

T-001 updates this section before T-007. Its accepted receipt MUST record exact
setup/install/invocation commands, fixture/program, expected denominator,
required environment/cleanup, and the retained-Python `PRODUCT_WORKS` outcome.
The current document does not guess those commands.

## 3. Rollback command block — owned by T-001

T-001 updates this section before T-007 with the exact candidate-removal and
retained-Python replay command. It must show that no package publication,
console-script, persisted-data, or client-configuration reversal is necessary.

## Failure interpretation

- Unsupported version is official `-32022` data `requested`/`supported`, not an
  application tool result.
- Input-required is official MRTR behavior, not a server-initiated request.
- Invalid advertised arguments are complete `CallToolResult` application errors
  with `structuredContent.kind: invalid_tool_arguments` and `isError: true`.
- A missing/short/malformed handle is uniform `DEBUG_SESSION_NOT_FOUND`, not an
  invalid-argument or existence-oracle result.
- A green test suite without materialized commands, both consumer receipts, and
  rollback replay is insufficient for M1 acceptance.

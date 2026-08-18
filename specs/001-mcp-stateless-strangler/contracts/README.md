# Milestone M1 Contracts

`modern-front-door.schema.json` is application-scoped JSON Schema for tool
inputs and kind-discriminated `structuredContent`. Its variants are exclusive;
it is not a duplicate of the official MCP wire schema.

## Official MCP contract

C# v2.1.0/MCP 2026-07-28 own envelopes, discovery/list/call, request `_meta`,
cacheable `ttlMs`/`cacheScope`, `CallToolResult`, `InputRequiredResult`,
`inputResponses`, and `-32022`.

- Server MUST implement `server/discover`; client MAY send it, list, or a valid
  call first. No discovery/order state is retained.
- Discover/list are cacheable; ordinary complete calls are not cacheable merely
  because their catalog is.
- Complete tool result has `resultType: complete`, content, structured content,
  and applicable `isError`.
- No-program start uses official input-required only with supported form
  elicitation; it has no requestState and uses a new-id retry/inputResponses.

## Application runtime validation

`start_debug` accepts optional non-empty `program` and no extras. For state and
stop, required/minimum-length schema constraints advertise a valid
capability-bearing call. At direct runtime, a missing `debugSessionId` or
non-empty short/malformed token string intentionally maps to the uniform
not-found result; empty/whitespace, non-string, and extra-field shapes remain
invalid arguments. Runtime, not generated schema alone, validates before any native
side effect:

```json
{"kind":"invalid_tool_arguments","error":"INVALID_TOOL_ARGUMENTS","tool":"start_debug"}
```

This is a complete `CallToolResult` application error with `isError: true`.
Missing/short/malformed handles use:

```json
{"kind":"debug_session_not_found","error":"DEBUG_SESSION_NOT_FOUND"}
```

Official unsupported-version error data contains exactly `requested` and
`supported`.
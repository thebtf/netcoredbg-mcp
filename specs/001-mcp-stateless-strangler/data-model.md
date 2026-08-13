# Data Model — Milestone M1

## Modern request context

| Field | Source | Rule |
|---|---|---|
| `protocolVersion` | Current request `_meta` | Validated only for the current request. |
| `clientCapabilities` | Current request `_meta` | Governs current behavior, including form elicitation. |
| `inputResponses` | Current MRTR retry request | Combined with repeated arguments; M1 emits no `requestState`. |
| JSON-RPC id | Current envelope | Correlation only; MRTR retry uses a new id. |

The server MUST implement discovery, but client request order is not retained.
A valid list or tool call may be the first modern request because each carries
its own metadata.

## DebugSession capability

| Field | Meaning | Visibility/lifetime |
|---|---|---|
| `debugSessionId` | Server-minted opaque capability. | Returned by complete start; supplied in later tool arguments; process-local. |
| `nativeDebugHandle` | Accepted native C# lifecycle seam reference. | Never serialized; exact form waits on T-002. |
| `lifecycleState` | Native state projection for M1. | Returned only after valid capability resolution. |
| registry membership | Atomic token-to-live-debugger association. | Removed once by winning stop/native-unavailable transition. |

Capability lifetime is only live native debugger plus host process. There is no
elapsed-time or durable policy; host restart drops registry entries.

## Runtime input validation boundary

Validate application arguments before MRTR or native action:

| Tool/input condition | Complete application result | Native side effect |
|---|---|---|
| `start_debug.program` empty, or any extra field | `invalid_tool_arguments` / `INVALID_TOOL_ARGUMENTS`, `isError: true` | No launch; no MRTR |
| `get_debug_state` / `stop_debug` extra field | Same invalid-arguments class | No native state/stop |
| Missing, short, or malformed `debugSessionId` | Uniform `debug_session_not_found` / `DEBUG_SESSION_NOT_FOUND` | No native state/stop |
| Well-formed but unusable token | Same uniform not-found class | No native state/stop |

Published JSON Schema is not sufficient evidence; process tests must assert
these runtime outcomes and observable zero native actions.

## MRTR start input

A valid no-program start with current form-elicitation capability returns
official `InputRequiredResult` with `elicitation/create`, `resultType:
input_required`, and no `requestState`. The client sends a new-id `tools/call`
with repeated arguments plus official `inputResponses`. A valid no-program call
without that capability returns a deterministic complete application error. M1
never sends a server request.

## Capability invariants

1. Tokens are cryptographically strong, opaque, non-enumerable, and not logged.
2. There is no current-session or connection-token map; creator disconnect does
   not stop a live debugger.
3. Independent/interleaved requests may resolve one live token.
4. Stop atomically removes the token before native stop. One winner succeeds;
   all concurrent/later losers receive uniform not-found and cause no stop.
5. Random/malformed, stopped/closed, native-unavailable, and prior-process
   tokens are externally indistinguishable.

Unsupported protocol version remains official `-32022` with `requested` and
`supported`, never an application payload.
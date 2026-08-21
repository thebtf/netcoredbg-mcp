# Local Modern Preview Contract

This child adopts the exact behavioral values in parent
`specs/005-stateless-preview/spec.md` and confines them to local source-run
implementation.

## Process

- Exact CLI: one `--project <path>`; invalid startup exits `64`, writes exactly
  `PREVIEW_ROOT_INVALID\n` to stderr, and writes no stdout.
- One fresh local stdio process; logging/diagnostics never go to stdout.
- No environment, client-root, CWD, home, URI, debug, bridge, artifact, Python,
  mux, HTTP, or remote authority changes the root.

## Modern requests

Every request uses `2026-07-28` request-local metadata:
`io.modelcontextprotocol/protocolVersion`,
`io.modelcontextprotocol/clientInfo`, and
`io.modelcontextprotocol/clientCapabilities`.

Fresh `server/discover`, `tools/list`, and valid `tools/call` requests succeed.
Unsupported version returns JSON-RPC `-32022` with only requested/supported
version data. The catalog contains exactly `find_code_symbol`, with read-only
and idempotent annotations, `ttlMs:300000`, and `cacheScope:"public"`.

## Tool input and output

Input is a closed object: required `name` string length 1–256 (not whitespace
only) and optional nullable `kind` of class/method/property/field. Success is
compact text JSON equal to structured content:

```json
{"kind":"find_code_symbol_success","results":[{"file":"relative.cs","line":1,"name":"Name","kind":"class","context":"source"}]}
```

Errors are closed `{kind,error,tool}` triples for invalid arguments, path
refusal, unreadable search, or budget/deadline exhaustion. Unknown tool calls
remain text-only; excluded methods are method-not-found. No failure returns
partial data.

For every emitted unknown-tool or `-32022` unsupported-version response,
preserve the exact requested payload and same request ID whenever the complete
same-ID response fits the 256 KiB cap. If a request cannot be represented with
that exact payload under the cap, refuse or close it before dispatch with no
response; never emit a truncated, partial, sentinel, or otherwise falsified
requested value.

## Search boundary

Preview validates regular/non-reparse lexical and final-under-root paths before
reading entered directories, root `.gitignore`, and `.cs` files. It applies the
parent bounds: 2,048 entered directories, 20,000 non-directory entries, 1 MiB
per opened file, 16 MiB aggregate, 128 matches, 256 KiB full response, and five
seconds after first filesystem work. Cancellation returns no tool result.

Static hard-link provenance is not a child guarantee; tests and claims cover
reparse/final-path authority only.

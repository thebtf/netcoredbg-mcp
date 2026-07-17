# netcoredbg-mcp v0.23.2

Released: 2026-07-17

## Summary

`v0.23.2` is a PATCH release that hardens the published Python MCP entrypoint's
project-root discovery, keeps managed FlaUI bridge fallback ahead of uncontrolled
PATH binaries, adds live subscriptions for its four public `debug://` resources,
and advances the source/developer .NET compatibility host to complete front-door
parity with the Python server. A client that advertises MCP roots but never
answers `roots/list` can no longer hold launch-compatibility inspection and
cleanup indefinitely.

The Python package and `netcoredbg-mcp` console script remain the only published
entrypoint. The .NET host is still a source-only compatibility preview and does
not replace, package, or retire the Python runtime.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   authoritative and backward compatible.
2. An installed wheel starts through the public CLI, initializes over MCP, and
   exposes the complete public catalog of 135 tools, eight prompts, and four
   resources.
3. The installed Python server advertises `resources.subscribe=true`, accepts
   idempotent subscribe/unsubscribe requests for all four public resources, and
   emits ordered, coalesced `notifications/resources/updated` events when their
   backing state changes.
4. Project-root resolution preserves explicit operator scope, accepts only valid
   local client roots, rejects network/UNC roots, and falls back to startup CWD
   after a two-second unanswered `roots/list` deadline.
5. If another live session prevents rebuilding the managed FlaUI bridge, the
   resolver retains the last successful managed binary before considering PATH,
   avoiding a silent downgrade to an incompatible bridge protocol.
6. The .NET 8 compatibility host can be built from source and exercises tools,
   native prompts, roots, resources/subscriptions, progress/logging, and
   allowlisted `x-mux` metadata against the Python execution authority. It is not
   installed by the wheel and is not the default or published entrypoint.

## Highlights

### Bounded project-root discovery

The provider-to-client `roots/list` request now has a two-second deadline.
Explicit environment or `--project` scope remains authoritative; a valid local
client root still takes precedence over startup CWD when no operator pin exists.
Network and UNC authorities remain rejected. If a roots-capable client accepts
but never answers the request, the server continues through the existing
startup-CWD fallback instead of hanging the read-only compatibility preflight and
subsequent cleanup path (Engram #380, PR #236).

### Safe managed FlaUI bridge fallback

The bridge resolver now keeps the last successfully managed executable ahead of
uncontrolled PATH candidates when a source rebuild cannot replace an executable
held by another live MCP session. This preserves the managed trust order and
prevents an older PATH bridge from returning protocol-incompatible error
responses without request IDs.

### Live resource subscriptions

The published Python server now supports subscribe/unsubscribe for:

- `debug://state`
- `debug://breakpoints`
- `debug://output`
- `debug://threads`

Notifications are serialized, deduplicated by current resource revision, and
coalesced so a blocked subscriber retains at most one live delivery task per URI
plus one latest-revision catch-up. Unsubscribing is ordered after any in-flight
send, and unknown resource URIs fail with `InvalidParams` (Engram #393, PR #235).

### Complete source-only .NET front-door preview

The compatibility host's single bidirectional relay session now covers the full
accepted front-door wave: tools, native prompts, downstream roots, resources and
resource updates, progress/logging notifications, cancellation correlation,
request-ID fencing, and allowlisted `x-mux` ownership metadata. Python remains
the execution authority. The source-only host preserves response/update wire
ordering and fails closed on correlation reuse, cancellation races, and
unsupported capability combinations (PRs #233–#235).

## Compatibility Boundary

> The Python package and `netcoredbg-mcp` console script remain the only
> published production path. `host/NetCoreDbg.Mcp.Host` is built from a source
> checkout and launches an installed Python backend. It is absent from the wheel,
> is not registered as a console script, and does not authorize entrypoint
> cutover, Python retirement, or MCP Tasks advertisement.

## Explicit No-Cutover Statement

`v0.23.2` performs no .NET published-entrypoint cutover. Existing MCP client
configurations continue to launch `netcoredbg-mcp` exactly as before. The .NET
host remains a developer preview for source builds and compatibility testing;
rolling back from that preview requires only returning the client configuration
to the unchanged Python console entrypoint.

## Upgrade Notes

There is no intentional breaking change to the published Python API or CLI.
Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.2
# or
pipx upgrade netcoredbg-mcp
```

Install on a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.2
netcoredbg-mcp --setup
```

## Known Residual Scope

- The .NET compatibility host is not included in the Python wheel and is not a
  published entrypoint. Packaging and any future cutover remain separate,
  explicitly gated work.
- Python remains the execution authority behind the .NET preview; native .NET
  tool-family migration and Python runtime retirement are not part of this
  release.
- MCP Tasks remain deliberately unadvertised and unsupported until the Python
  authority negotiates one exact protocol dialect.

## Release Gates

- Integration base: merged PRs `#233`, `#234`, `#235`, and `#236` on
  `main@cdea10ba9834ec70c247f1b8ea9713655a15d716`.
- Merged integration evidence: .NET Host tests `131/131`; native prompt tests
  `75/75`; critical suite `25/25`; self-contained `win-x64` host journey
  `PRODUCT_WORKS` with exact direct/host catalog parity, eight prompts, four
  resources, subscription/state read, and clean Python rollback.
- Engram #380 repair evidence: focused project-root/preflight suite `94 passed`;
  full Python suite `2089 passed, 3 skipped` with one known mocked-coroutine
  warning; exact NovaScript M1 public-MCP replay returned
  `blocked_no_matching_shim` in 2.058 seconds, ignored one intentionally
  unanswered roots request, performed no mutation, and completed responsive
  cleanup on the same connection.
- Fresh `v0.23.2` release-candidate evidence is recorded in
  `.agent/specs/release-v0.23.2/evidence/pre-pr-gates.json`. Publication remains
  blocked until the installed-consumer `PRODUCT_WORKS` gate and every mandatory
  supporting row in `docs/RELEASE-PROTOCOL.md` pass.

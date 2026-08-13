# Research — Modern Stateless MCP Front Door for Milestone M1

## Decision inputs

| Question | Finding | Consequence |
|---|---|---|
| Which protocol baseline governs M1? | MCP 2026-07-28. The frozen audit records `BLOCK_MODERN_MCP_COMPLIANCE` for the existing published path. | Do not extend the initialize/paired-session architecture for modern traffic. |
| What is the modern M1 wire boundary? | Server implements `server/discover`, `tools/list`, and `tools/call`; debugger actions are cataloged tool names. Clients MAY call any valid supported RPC first because each request has `_meta`. | Do not model tool actions as MCP methods or retain discovery/order state; acceptance probes discover-first and separately list/call-first. |
| What result behavior is mandatory? | Discover/list are cacheable; complete tool calls use `CallToolResult`; M1 demonstrates MRTR `InputRequiredResult` for valid omitted-program start when form elicitation is supported; invalid inputs are rejected before native work. | Cache only discover/list; test complete/input-required/invalid-argument branches and zero prohibited side effects. |
| Can application state exist? | Yes, when every stateful call supplies an explicit identifier. | M1 uses process-local `debugSessionId`, never a connection session. |
| Can installed SDKs implement the front door? | No. Python `mcp` 1.25.0 and C# `ModelContextProtocol` 1.4.1 are legacy-only for these surfaces. | M1 adopts current upstream C# `ModelContextProtocol` v2.1.0. Python remains unchanged. |
| Is bespoke MCP wire required? | No. C# v2.1.0 evidence covers discover, request meta, result/cache, version error, tools, and MRTR. | Adopt the official SDK; do not create custom standard MCP framing/DTOs. |
| Is there an existing native C# lifecycle seam to adopt? | No. Frozen evidence finds no native .NET tool owner for DAP process lifecycle, framing, correlation, coarse state, and atomic teardown. | OWN a narrow BCL internal session; do not claim an existing seam. |
| Should M1 adopt OmniSharp.Extensions.DebugAdapter.Client 0.19.9? | No. It is a stale/DI-heavy typed client with no verified first-class external process lifecycle or unified process-tree cleanup ownership. | Do not add it as an M1 dependency; source patterns are comparative evidence only. |
| Should M1 adopt StreamJsonRpc? | No. It is generic JSON-RPC transport/invocation, not DAP framing, schemas, or debugger lifecycle ownership. | Do not add it; owning the remaining DAP work would not reduce the boundary. |

## Owned native lifecycle facts

The authorized future ownership is new internal executable
`host/NetCoreDbg.Mcp.Stateless/`, namespace `NetCoreDbg.Mcp.Stateless`, narrow
`DebugAdapter/NetCoreDbgSession.cs` and `DebugAdapter/DapSessionState.cs`, with
sibling tests in
`host/NetCoreDbg.Mcp.Stateless.Tests/DebugAdapter/NetCoreDbgSessionTests.cs`.
These are target paths, not existing-file claims. The legacy
`host/NetCoreDbg.Mcp.Host` MCP 1.4.1 relay remains unchanged.

The session owns `netcoredbg --interpreter=vscode` via BCL `Process` with
redirected stdin/stdout/stderr. DAP frames are ASCII `Content-Length: N\r\n\r\n`
headers followed by UTF-8 JSON; `N` is exact UTF-8 byte count. It allocates
outbound request `seq` values and completes pending requests from response
`request_seq`. It accepts netcoredbg's nonstandard `capabilities` event before
the initialize response, records it, and gates launch strictly on the correlated
successful response. The M1 sequence is initialize → initialized → launch →
configurationDone only when advertised. The event-backed state is deliberately
coarse: stopped, continued, exited, terminated; other transition labels are
host inference, not a public DAP state.

For a launched target, cleanup is one asynchronous idempotent owner: send
`terminate` only when supported, use bounded grace, send `disconnect`, await
owned process exit, then kill its process tree only if it remains. Cancellation
or timeout must converge to that cleanup. M1 excludes attach, breakpoints,
stacks, evaluate, persistence, auth, generic DAP framework, and any new
third-party DAP/JSON-RPC dependency.

## Official evidence

- MCP 2026-07-28 statelessness and result contract:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/index>
- MCP versioning and `server/discover`:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning>
- MCP schema: <https://modelcontextprotocol.io/specification/2026-07-28/schema>
- MCP stdio transport:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio>
- C# SDK v2.1.0 release/NuGet:
  <https://github.com/modelcontextprotocol/csharp-sdk/releases/tag/v2.1.0>,
  <https://www.nuget.org/packages/ModelContextProtocol/2.1.0>
- Local DAP base framing, request sequencing, initialization, and cleanup:
  `docs/dap-protocol/overview.md`, `specification.md`, and
  `debugAdapterProtocol.json`.
- Local capability evidence: `agent://SdkCapabilityEvidence`.
- Package disposition evidence: `agent://CSharpDapPackageResearch`.
- Netcoredbg lifecycle evidence: `agent://NetCoreDbgLifecycleResearch`.
- Frozen migration audit:
  `.agent/reports/2026-08-13-dotnet-migration-mcp-2026-07-28-audit.json`.

## Dispositions binding this slice

| Legacy assumption | Disposition | M1 treatment |
|---|---|---|
| `initialize` plus paired relay session establishes context. | REPLACE | M1 validates request-local `_meta` on every supported RPC; discovery is server-mandatory, client-optional. |
| Start/state/stop are direct MCP methods. | REPLACE | They are exactly three `tools/list` entries invoked through `tools/call`. |
| Client must discover before list/call. | DISCARD | Fresh-process list/call-first acceptance proves no ordering state. |
| Published schema alone enforces safe arguments. | DISCARD | Runtime validation returns exact application errors before native side effects. |
| Connection, process, or mux identity stands in for debugger ownership. | REPLACE | Explicit process-local `debugSessionId` is supplied in tool arguments; it is not authentication. |
| A native C# lifecycle seam exists in the relay or dependencies. | DISCARD | New internal `NetCoreDbgSession` owns the minimal DAP lifecycle using BCL and `System.Text.Json`. |
| A generic DAP/JSON-RPC package supplies process-to-cleanup ownership. | DISCARD | No new dependency; M1 owns only the named bounded lifecycle. |

## Handle-security conclusion

A `debugSessionId` is a high-entropy local capability, not user authentication.
It is not enumerable, connection-bound, persisted, or an existence oracle. Its
lifetime is only the live native debugger and host process—there is no elapsed
expiry policy. Random/malformed, stopped/closed, native-unavailable, and
prior-process candidates are indistinguishable externally. This conclusion does
not claim remote safety; remote transport and authentication are excluded.

## Factual uncertainty

T-009's lifecycle receipt is limited to the project build, the final 11-case
T-008-owned suite after independent discriminator hardening/frozen-production
RED proof, controlled-adapter readiness, and cleanup. Candidate launch, official
C# v2.1.0 client, environment, cleanup, and `PRODUCT_WORKS` command lines remain
unmaterialized until T-005 implements the full M1 front door after T-003/T-004
modern RED suites. The DAP launch argument schema is adapter-specific, so this
package claims only the process invocation and lifecycle—not guessed launch
arguments or a public state vocabulary.

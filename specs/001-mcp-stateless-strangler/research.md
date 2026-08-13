# Research — Modern Stateless MCP Front Door for Milestone M1

## Decision inputs

| Question | Finding | Consequence |
|---|---|---|
| Which protocol baseline governs M1? | MCP 2026-07-28. The frozen audit records `BLOCK_MODERN_MCP_COMPLIANCE` for the existing published path. | Do not extend the initialize/paired-session architecture for modern traffic. |
| What is the modern M1 wire boundary? | Server implements `server/discover`, `tools/list`, and `tools/call`; debugger actions are cataloged tool names. Clients MAY call any valid supported RPC first because each request has `_meta`. | Do not model tool actions as MCP methods or retain discovery/order state; acceptance probes discover-first and separately list/call-first. |
| What result behavior is mandatory? | Discover/list are cacheable; complete tool calls use `CallToolResult`; M1 demonstrates MRTR `InputRequiredResult` for valid omitted-program start when form elicitation is supported; invalid inputs are rejected before native work. | Cache only discover/list; test complete/input-required/invalid-argument branches and zero prohibited side effects. |
| Can application state exist? | Yes, when every stateful call supplies an explicit identifier. | M1 uses process-local `debugSessionId`, never a connection session. |
| Can installed SDKs implement this? | No. Python `mcp` 1.25.0 and C# `ModelContextProtocol` 1.4.1 are legacy-only for these surfaces. | M1 adopts current upstream C# `ModelContextProtocol` v2.1.0. Python remains unchanged. |
| Is a bespoke wire protocol required? | No. C# v2.1.0 evidence covers discover, request meta, result/cache, version error, tools, and MRTR. | Adopt the official SDK; do not create custom standard MCP framing/DTOs. |
| Is a native C# lifecycle seam already verified? | No. Frozen evidence finds no native .NET tools or verified C# debugger/DAP lifecycle seam. | M1 is exploration-ready only; T-002 must prove the seam before Code work. |

## Official evidence

- MCP 2026-07-28 statelessness and result contract:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/index>
- MCP versioning and `server/discover`:
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning>
- MCP schema, including request metadata, result envelope, and unsupported
  protocol version error: <https://modelcontextprotocol.io/specification/2026-07-28/schema>
- MCP stdio transport: <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio>
- Python SDK v2.0.0 release/documentation:
  <https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0>,
  <https://py.sdk.modelcontextprotocol.io/whats-new/>
- C# SDK v2.1.0 release/NuGet:
  <https://github.com/modelcontextprotocol/csharp-sdk/releases/tag/v2.1.0>,
  <https://www.nuget.org/packages/ModelContextProtocol/2.1.0>
- Local capability evidence: `agent://SdkCapabilityEvidence`.
- Frozen migration audit:
  `.agent/reports/2026-08-13-dotnet-migration-mcp-2026-07-28-audit.json`.
- Legacy roadmap disposition: `agent://LegacyRoadmapDisposition`.

## Dispositions binding this slice

| Legacy assumption | Disposition | M1 treatment |
|---|---|---|
| `initialize` plus paired relay session establishes context. | REPLACE | M1 validates request-local `_meta` on every supported RPC; discovery is server-mandatory, client-optional. |
| Start/state/stop are direct MCP methods. | REPLACE | They are exactly three `tools/list` entries invoked through `tools/call`. |
| Client must discover before list/call. | DISCARD | Fresh-process list/call-first acceptance proves no ordering state. |
| Published schema alone enforces safe arguments. | DISCARD | Runtime validation must return exact application errors before native side effects. |
| Connection, process, or mux identity stands in for debugger ownership. | REPLACE | Explicit process-local `debugSessionId` is supplied in tool arguments; it is not authentication. |
| Resources subscriptions are M1 scope. | DISCARD | Modern subscriptions are future work, outside M1. |

## Handle-security conclusion

A `debugSessionId` is a high-entropy local capability, not user authentication.
It is not enumerable, connection-bound, persisted, or an existence oracle.
Its lifetime is only the live native debugger and host process—there is no
elapsed expiry policy. Random/malformed, stopped/closed, native-unavailable,
and prior-process candidates are indistinguishable externally. This conclusion
does not claim remote safety; remote transport and authentication are excluded.

## Factual uncertainty

Current upstream source and its tests demonstrate C# v2.1.0 capability support,
but this planning pass did not independently run that SDK. Exact package API
names may be reconciled against the installed package documentation before code
is written; the protocol requirements and decision to adopt the official SDK do
not depend on an unverified local implementation detail.

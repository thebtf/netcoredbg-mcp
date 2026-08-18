# Research — Native scene probe (Phase 0/1)

## Decision summary

This research resolves the implementation inputs for the six additive M0/M1 primitives. It authorizes **planning only**. T001 may authorize only M0-G0 contract/parity work T002–T007; M0 implementation remains blocked until GREEN T007. The checked-in schemas remain design candidates until the operator accepts the M0-G0 gate; this document does not authorize product code or package edits.

| Question | Finding | Adopt / reject disposition |
|---|---|---|
| Who owns the new route? | ADR-003 makes native scene observation the evidence boundary. The host owns local debug-session binding, observer/probe lifecycle, stability/atomicity evidence, lossless capture, artifact storage, opaque artifact reads, and typed uncertainty. The Design Contract Factory remains external and owns DTCG Format/Resolver, comparison, root causes, and repair planning. | **ADOPT:** pure C#/.NET native route. **REJECT:** Python calls, Python workers/pythonnet, an in-host DTCG resolver/comparator, and `check_element_tokens`. |
| What is the framework split? | The host stays `net8.0` cross-platform; Windows UI observation/probes are optional `net8.0-windows` components. Existing `FlaUI.UIA3` and `System.Drawing.Common` are already isolated in `bridge/FlaUIBridge.csproj`. | **ADOPT:** existing bridge dependency and a C# probe boundary. **REJECT:** moving Windows-only dependencies into the cross-platform host. |
| What freezes the wire? | Spec 003 and ADR-003 call the examples semantic sketches. T001 operator approval authorizes only M0-G0 T002–T007; before any M0 implementation T008+, T007 must accept one exact Draft-7 schema artifact, runtime/schema parity tests, and negative wire tests. | **ADOPT:** Draft 7/common keywords only; closed primitive roots and `sceneRequest`; runtime and schema must accept/reject the same C001–C024 corpus. **GATE:** operator approval after this planning packet, then GREEN T007 before M0 implementation. |
| How are local calls transported? | Microsoft’s `NamedPipeServerStream` is a BCL stream for local IPC. ADR-003 requires one synchronous bridge connection, one in-flight request, correlation, bounded connect/write/read/response limits, and no network listener. | **ADOPT:** `System.IO.Pipes`/BCL framing and bounded request lifecycle. **REJECT:** StreamJsonRpc, MessagePack/protobuf/gRPC, remote endpoints, arbitrary URI/path retrieval. |
| How is time made deterministic? | .NET 8 includes `System.TimeProvider`; Microsoft documents it as a testable time abstraction. Microsoft’s `Microsoft.Extensions.TimeProvider.Testing` supplies `FakeTimeProvider` for deterministic time-dependent tests. | **ADOPT:** inject `TimeProvider` in production; use `Microsoft.Extensions.TimeProvider.Testing` **test-only**, version 10.9.0. **REJECT:** sleeps or wall-clock assertions as the correctness mechanism. |
| How are artifacts stored and verified? | ADR-003 requires per-session server-owned roots, staged then atomic writes, immutable committed artifacts, opaque session/capture-bound IDs, SHA-256, and read-time identity/length/hash checks. BCL `FileStream`, `SHA256`, and `RandomNumberGenerator` cover storage, integrity, and opaque IDs. | **ADOPT:** BCL file I/O, cryptographic hash, CSPRNG capability IDs, bounded chunk reads. **REJECT:** caller-selected destinations, absolute-root disclosure, path reads, mutable artifact replacement. |
| What captures are authoritative? | Only an opt-in in-process framework probe may report `COMPLETE`, using one dispatcher-affine, non-yielding immutable-DTO transaction with equal probe-owned before/after revisions. UIA/FlaUI is guarded fallback evidence only; raster timing is independent. | **ADOPT:** `COMPLETE` only with probe authority and equal revisions; `uia_guarded` `PARTIAL`/`UNOBSERVABLE` otherwise. **REJECT:** inferring atomicity from delay, screenshot, prior stabilization, or independent UIA reads. |

## Production ownership and BCL evidence

| Surface | Primary-source finding | Planning consequence |
|---|---|---|
| JSON serialization | Microsoft describes `System.Text.Json` as the .NET JSON serializer/deserializer and DOM, with immutable/thread-safe `JsonDocument` use in .NET 8+ once created. | Use `System.Text.Json`; keep schema validation separate from serialization. No Newtonsoft.Json dependency. |
| Local IPC | [`NamedPipeServerStream`](https://learn.microsoft.com/en-us/dotnet/api/system.io.pipes.namedpipeserverstream) is a `Stream`/`PipeStream` server type in `System.IO.Pipes`; this is sufficient for a local bridge without adding a transport package. | Use BCL pipes with explicit framing, correlation, size, timeout, cancellation, and one in-flight request. |
| Artifact bytes | [`FileStream`](https://learn.microsoft.com/en-us/dotnet/api/system.io.filestream) provides file-backed stream reads/writes and async operations; `FlushAsync` writes buffered data to the device. | Stage to a server-owned file, flush/close, then atomically commit; never expose the path. |
| Integrity and IDs | [`SHA256`](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.sha256) supports hashing streams; [`RandomNumberGenerator`](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.randomnumbergenerator) provides cryptographically strong random values. | Hash the complete committed artifact and generate opaque, unguessable capability IDs with CSPRNG bytes. Hash/identity/length mismatches return `ARTIFACT_INTEGRITY_FAILED` and no bytes. |
| Time | Microsoft’s [TimeProvider overview](https://learn.microsoft.com/en-us/dotnet/standard/datetime/timeprovider-overview) states that `TimeProvider` makes time-dependent code testable/predictable, is included in .NET 8, and supports `FakeTimeProvider` through the testing package. | Production uses injected `TimeProvider`; tests advance fake time to prove 4-hour retention and settle deadlines without sleeps. |
| Serialization concurrency | Microsoft documents `SemaphoreSlim` as a lightweight in-process semaphore with cancellation support and no named-semaphore/wait-handle requirement. | Use `SemaphoreSlim` only for in-process per-session/bridge serialization; it does not replace transport ownership or process identity. |

## JSON Schema decision

### Frozen contract rules

Spec 003/ADR-003 are authoritative for semantics and explicitly require the first M0 gate to freeze exact schemas. The feature-004 schema author must therefore encode the following without inventing a second protocol:

- protocol `native-scene-probe/1`, explicit schema version, closed request roots, and closed `sceneRequest`;
- malformed protocol/schema-version syntax, type, bound, or closed-object violations → `INVALID_TOOL_ARGUMENTS`;
- syntactically valid known-but-unsupported protocol/schema versions → `UNSUPPORTED_PROTOCOL`;
- JSON Schema Draft 7 **common keywords only** (`type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `oneOf`/`anyOf`, `allOf`/`contains`, numeric/string/array bounds, and descriptions where useful); no vendor vocabulary or Draft 2020-12-only features;
- schema limits: artifact read bytes `1..65536`; lossless artifact `<=64 MiB`; scene artifact `<=16 MiB`; manifest/structured response `<=256 KiB`; `selectedState` and `currentState` each `<=262,144` serialized UTF-8 bytes before DTO materialization; graph `<=4096` nodes; issues `<=256`; artifact references `<=4`; settle timeout `<=30s`; retention `4h` or session stop; generally strings `1..256`, prose/error text `<=1024`; byte offsets bounded by `9007199254740991`;
- six primitives only: M0 `get_ui_probe_capabilities`, `capture_visual_evidence`, `read_capture_artifact`; M1 `wait_for_ui_stable`, `capture_element_snapshot`, `capture_native_scene`; the declaration's array has `minItems: 6`, `maxItems: 6`, and an `allOf`/`contains` constraint for each name, with a runtime exact-set/no-duplicates assertion; no `check_element_tokens`;
- each declared settle condition—`dispatcherIdle`, `stableLayout`, `animationState`, `windowGeometry`, `contextMaterialization`, and `asyncLoadSettled`—has an explicit capability state, and negotiated `sampleCount` declares inclusive 2–16 bounds;
- every element snapshot and native-scene evidence-returning or evidence-committing branch has capture-time `revalidatedByCapture: true`, including `PARTIAL` and `UNOBSERVABLE`; only a standalone wait receipt has `false`;
- unknown, foreign, expired, deleted, and unavailable artifact IDs use exactly `{kind: "tool_error", tool: "read_capture_artifact", code: "ARTIFACT_NOT_FOUND", message: "Artifact is not available."}` with no additional member, artifact metadata, or free-text variation;
- schemas are checked-in **design candidates** until T001 approval. M0-G0 parity and negative wire tests T002–T007 must be GREEN before M0 implementation.

### Validator disposition

| Candidate | Evidence | Disposition |
|---|---|---|
| **NJsonSchema 11.6.1** | [NuGet metadata](https://www.nuget.org/packages/NJsonSchema/11.6.1) describes reading, generating, and validating JSON Schema Draft v4+; it targets .NET 8 and is MIT-licensed. Its package metadata also lists Newtonsoft.Json and Namotion.Reflection dependencies. | **ADOPT test-only candidate** for Draft-7/common-keyword schema parity. Do not add it to production; production validation must use the smallest approved mechanism and preserve the BCL-only production dependency budget. If used in tests, keep Newtonsoft transitively confined to the test graph. |
| **JsonSchema.Net** | [NuGet metadata](https://www.nuget.org/packages/JsonSchema.Net) and the [maintainer repository](https://github.com/json-everything/json-everything) describe JSON Schema support, but the current binary distribution includes an EULA/Open Source Maintenance Fee requiring qualifying revenue users to pay a fee. | **REJECT** for production and tests in this feature despite MIT source metadata; the current binary/EULA terms violate the project dependency decision. |
| Hand-written validation only | `System.Text.Json` supplies parsing/serialization, not Draft-7 schema evaluation. A hand-written validator risks runtime/schema drift against the mandatory M0-G0 parity requirement. | **REJECT as the sole parity oracle.** It may implement the approved production checks only when parity tests prove exact agreement with the frozen artifact. |

## Existing Windows/raster dependencies

`bridge/FlaUIBridge.csproj` currently targets `net8.0-windows`, uses WPF, and already references `FlaUI.UIA3` 5.0.0 and `System.Drawing.Common` 8.0.10. `bridge/Commands/ScreenshotCommands.cs` uses FlaUI capture plus `System.Drawing`/GDI (`PrintWindow`, `BitBlt`, PNG encoding) and therefore establishes existing raster evidence—not a reason to add ImageSharp.

| Existing package | Primary-source finding | Disposition |
|---|---|---|
| `FlaUI.UIA3` 5.0.0 | [NuGet metadata](https://www.nuget.org/packages/FlaUI.UIA3/5.0.0) describes UIA3 usage, targets .NET 6+/Windows-compatible TFMs, and depends on `FlaUI.Core`/UI Automation interop. | **ADOPT existing dependency only** in the Windows bridge. Treat UIA as identity/geometry/accessibility/raw evidence; never as computed style, token provenance, or `COMPLETE` authority. |
| `System.Drawing.Common` 8.0.10 | [NuGet metadata](https://www.nuget.org/packages/System.Drawing.Common/8.0.10) describes GDI+ graphics access and lists the existing package/version. | **ADOPT existing dependency only** for lossless PNG/raster capture in the Windows bridge. Keep raster timing and artifact integrity independent from scene atomicity. |
| ImageSharp | Official [ImageSharp licensing documentation](https://docs.sixlabors.com/articles/imagesharp/index.html) states that ImageSharp 4.x uses the Six Labors Split License and requires a valid license at build time for direct dependencies. | **REJECT:** no new image stack; existing GDI/System.Drawing path already covers this slice and avoids a new licensing/runtime surface. |

## Excluded transport/format libraries

| Library | Primary-source fact | Explicit disposition |
|---|---|---|
| StreamJsonRpc | [NuGet metadata](https://www.nuget.org/packages/StreamJsonRpc) describes generic JSON-RPC over streams/WebSocket and lists a dependency graph including MessagePack, Nerdbank.Streams, Visual Studio threading/validation, and JSON libraries. | **REJECT:** the feature needs a bounded local probe protocol and ownership, not a generic RPC abstraction; adding it increases dependencies without removing DAP/probe lifecycle work. |
| MessagePack | [NuGet metadata](https://www.nuget.org/packages/MessagePack) describes a binary serializer and lists analyzer/annotation/runtime dependencies. | **REJECT:** the contract is JSON Schema/Draft-7 and bounded JSON artifacts; binary encoding would create a second wire format and does not solve local ownership. |
| protobuf/gRPC | Official [gRPC for .NET documentation](https://learn.microsoft.com/en-us/aspnet/core/grpc/) describes RPC over HTTP/2. | **REJECT:** no network listener or remote endpoint is permitted by Spec 003; local BCL pipes are the smaller fit. |
| Python interop | ADR-003’s ownership split and the additive strangler architecture retain Python unchanged for rollback/parity; the native route is C#/.NET. | **REJECT:** no Python worker, pythonnet, subprocess-to-Python, or new Python product code. |
| DTCG package/resolver | ADR-003 assigns DTCG Format/Resolver, comparison, root causes, and repair planning to the external Design Contract Factory. | **REJECT:** no DTCG package, token resolver, comparator, or conformance verdict in this repository. |

## Artifact, retention, and integrity decisions

1. Create a per-session server-owned artifact root; caller input may contain only opaque `artifactId`, never a path or destination.
2. Write to a staging file, flush/close it, then atomically commit. Once committed, the artifact is immutable.
3. Manifest references include media type, byte length, SHA-256, schema version, capture ID, and retention policy. Artifact references are capped at four; manifests/structured responses at 256 KiB.
4. On first successful read, verify the complete committed file against the manifest hash and record file identity/length. Every later read rechecks identity/length before returning a bounded raw-byte chunk as padded standard base64.
5. `read_capture_artifact` accepts only `debugSessionId`, `protocolVersion`, `artifactId`, zero-based offset, and `maxBytes`; it returns at most 65,536 bytes. Unknown, foreign, expired, deleted, and unavailable IDs all return exactly `{kind: "tool_error", tool: "read_capture_artifact", code: "ARTIFACT_NOT_FOUND", message: "Artifact is not available."}` with no additional member, artifact metadata, or free-text variance; any integrity mismatch returns `ARTIFACT_INTEGRITY_FAILED` and no bytes.
6. Retain artifacts for at most four hours or until session stop, whichever occurs first; use injected `TimeProvider` so expiry tests are deterministic.

## Sources

### Repository authority

- [ADR-003 — Native scene observation](https://github.com/thebtf/netcoredbg-mcp/blob/main/docs/adr/ADR-003-native-scene-observation.md) — accepted architecture and ownership split; schema gate; external Design Contract Factory; artifact and atomicity rules.
- [Spec 003 — Native scene observation](https://github.com/thebtf/netcoredbg-mcp/blob/main/specs/003-native-scene-observation/spec.md) — semantic contract, typed outcomes, limits, M0-G0, and requirements-to-owner map.
- `host/NetCoreDbg.Mcp.Stateless/NetCoreDbg.Mcp.Stateless.csproj` — current cross-platform `net8.0` host and existing `ModelContextProtocol`/hosting references.
- `bridge/FlaUIBridge.csproj` — current Windows bridge and existing FlaUI/System.Drawing versions.
- `bridge/Commands/ScreenshotCommands.cs` — current PNG/raster path using FlaUI, System.Drawing, PrintWindow, and BitBlt.
- `host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj` — current test stack and no production schema/time package.

### Official Microsoft documentation

- [System.Text.Json overview](https://learn.microsoft.com/en-us/dotnet/standard/serialization/system-text-json/overview)
- [NamedPipeServerStream](https://learn.microsoft.com/en-us/dotnet/api/system.io.pipes.namedpipeserverstream)
- [FileStream](https://learn.microsoft.com/en-us/dotnet/api/system.io.filestream)
- [SHA256](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.sha256)
- [RandomNumberGenerator](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.randomnumbergenerator)
- [TimeProvider overview](https://learn.microsoft.com/en-us/dotnet/standard/datetime/timeprovider-overview)
- [SemaphoreSlim guidance](https://learn.microsoft.com/en-us/dotnet/standard/threading/semaphore-and-semaphoreslim)
- [gRPC for .NET](https://learn.microsoft.com/en-us/aspnet/core/grpc/)

### Package metadata and licensing

- [NJsonSchema 11.6.1 — NuGet](https://www.nuget.org/packages/NJsonSchema/11.6.1) — Draft v4+ validation, .NET 8 target, MIT metadata, Newtonsoft dependency.
- [Microsoft.Extensions.TimeProvider.Testing 10.9.0 — NuGet](https://www.nuget.org/packages/Microsoft.Extensions.TimeProvider.Testing/10.9.0) — `FakeTimeProvider`, MIT, no net8.0 dependencies.
- [FlaUI.UIA3 5.0.0 — NuGet](https://www.nuget.org/packages/FlaUI.UIA3/5.0.0)
- [System.Drawing.Common 8.0.10 — NuGet](https://www.nuget.org/packages/System.Drawing.Common/8.0.10)
- [JsonSchema.Net — NuGet](https://www.nuget.org/packages/JsonSchema.Net) and [maintainer EULA/maintenance-fee statement](https://github.com/json-everything/json-everything)
- [StreamJsonRpc — NuGet](https://www.nuget.org/packages/StreamJsonRpc)
- [MessagePack — NuGet](https://www.nuget.org/packages/MessagePack)
- [ImageSharp licensing](https://docs.sixlabors.com/articles/imagesharp/index.html)
- [DTCG official site](https://www.designtokens.org/) — the standard is external; no DTCG package is adopted here.

## Approval gate

**Publishing or merging this planning packet is allowed and does not constitute T001 approval. T001 operator approval applies to the exact merged candidate bytes and authorizes only M0-G0 T002–T007.** T007 must turn the approved Draft-7 schema artifact, its production validation approach, runtime/schema parity tests, and negative C001–C024 wire tests GREEN before T008+ M0 implementation. Until then, no product code, test-project edit, package change, implementation command, build, test run, formatter, implementation PR, product publication, or external agent CLI is in scope.

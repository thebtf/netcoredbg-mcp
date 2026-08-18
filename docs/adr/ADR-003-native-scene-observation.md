# ADR-003: Make native scene observation the evidence boundary; defer DTCG conformance to the Design Contract Factory

## Status

Accepted. Supersedes ADR-002 as the implementation authority before any DTCG M1 code exists.

## Context

`netcoredbg-mcp` can operate a debug session and has Windows UIA/FlaUI evidence seams, but those seams do not make a design-conformance engine. The current native candidate owns its MCP front door and debug-session registry in `host/NetCoreDbg.Mcp.Stateless/Program.cs`; its `ToolCatalog` currently exposes only `start_debug`, `get_debug_state`, and `stop_debug`. Native DAP lifecycle and state are owned by `DebugAdapter/NetCoreDbgSession.cs` and `DebugAdapter/DapSessionState.cs`. There is no native DesignTokens client, native UI client, scene-capture tool, artifact-read primitive, or in-process framework probe in this repository.

The existing Windows bridge owns UIA/FlaUI interaction in `bridge/JsonRpcHandler.cs`, `bridge/Commands/ElementCommands.cs`, and `bridge/Commands/ScreenshotCommands.cs`. The retained Python route owns its UI tool registration, bridge client, and session artifacts in `src/netcoredbg_mcp/tools/ui.py`, `src/netcoredbg_mcp/ui/flaui_client.py`, and `src/netcoredbg_mcp/ui/temp_manager.py`. Those are current evidence sources and ownership references, not implementation authorization for a new Python feature.

UIA is useful fallback evidence, not computed-style or atomic-graph authority. Microsoft documents `AutomationElement.BoundingRectangle` as physical screen coordinates; it can be empty for a non-displayed item and can include non-clickable points. A UIA/FlaUI multi-element traversal consists of independently timed reads, so matching before/after window, client, DPI, or visual-tree fingerprint guards can only bound its uncertainty; they cannot prove one atomic graph. Raster has its own capture timing and cannot silently share a scene epoch. [UIA BoundingRectangle](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.boundingrectangleproperty?view=windowsdesktop-10.0), [WPF `GetValueSource`](https://learn.microsoft.com/en-us/dotnet/api/system.windows.dependencypropertyhelper.getvaluesource?view=windowsdesktop-10.0).

The stable [DTCG Format Module 2025.10](https://www.designtokens.org/TR/2025.10/format/) defines token exchange data, including the `group.$root` token form (sections 6.2, 6.7.2, and 7.1.1). It does not assign a token to a live framework property or diagnose why an implementation differs from a contract. That comparison and diagnosis belong to the external **Design Contract Factory**. Neither it nor a Gallery implementation exists in this repository.

The JSON examples in Spec 003 are normative semantic sketches, not a frozen wire schema. An exact JSON Schema artifact, runtime/schema parity tests, and negative wire tests must be completed as the first M0 design gate before any M0 primitive is implemented. This ADR does not create that schema artifact.

## Decision

ADR-003 replaces the private DTCG resolver/comparator proposed by ADR-002 with a canonical **native-scene observation** boundary:

1. `netcoredbg-mcp` observes and transports bounded, attributable evidence for an explicit local debug session. It never emits a conformance `PASS`/`FAIL`, a root cause, repair advice, or inferred DTCG token provenance.
2. The Design Contract Factory, as an external required integration, resolves DTCG Format/Resolver inputs, owns component/layout/state contracts, compares scene facts, minimizes root causes, and plans repair. It retrieves artifacts only through the server's `read_capture_artifact` primitive; the Factory cannot read a server-owned artifact root or treat any path as retrieval authority. It is not implemented or assumed present in this repository.
3. `check_element_tokens` is deferred. If a shared Factory comparator later exists, it may be an optional facade over the canonical snapshot artifact. It is not a foundational MCP interface and must not create a second resolver, comparator, or diagnosis engine in `netcoredbg-mcp`.
4. All captures are bound to one explicit `debugSessionId`, positive local process identity, a negotiated protocol/capability set, and a candidate identity. Windows observers and framework probes are optional components so the native host remains `net8.0` cross-platform.

### Ownership split

| Owner | Owns | Must not claim |
|---|---|---|
| `netcoredbg-mcp` | Debug-session authority; local PID/session/process binding; observer/probe lifecycle; capture orchestration; stability and atomicity evidence; capability negotiation; candidate/runtime provenance; lossless raster capture; server-owned artifact storage and capability reads; typed uncertainty. | DTCG resolution, token-to-property mapping, conformance verdicts, root causes, repair planning, or a filesystem/absolute-root retrieval surface. |
| Design Contract Factory (external required integration) | DTCG Format/Resolver semantics; component/layout contracts; statecharts; scene comparison; root-cause minimization; repair planning; client use of opaque artifact capabilities. | Debug-session authority, process discovery, artifact-root access, or server artifact ownership. |
| UIA/FlaUI fallback | Process/window identity, physical geometry, accessibility, interaction, raw raster, and guarded best-effort traversal evidence. | Computed property values, resource/template/value-source provenance, token provenance, conformance, or `COMPLETE` atomic-scene authority. |
| `DesignProbe.Wpf` / `DesignProbe.Avalonia` (future opt-in adapters) | Test- or Gallery-only local IPC evidence for visual tree, effective properties, value source/resource/template/text/state data. An in-process framework probe may author a `COMPLETE` atomic scene under the transaction rule below. | A production listener, remote access path, weaker debug-session locality authority, or DTCG comparison. |

### Canonical public primitives

The primitives share a versioned native-probe envelope. The shapes in Spec 003 are normative semantic sketches pending the mandatory frozen-schema M0 gate; they are not a claim that a closed wire schema already exists.

| Primitive | Milestone | Responsibility |
|---|---|---|
| `get_ui_probe_capabilities` | M0 | Declare supported protocol/schema versions, observer/probe capabilities, request-context support, negotiated limits, and candidate identity. |
| `read_capture_artifact` | M0 | Return a bounded base64 chunk from one server-owned immutable artifact named only by an opaque session/capture-bound capability ID. |
| `wait_for_ui_stable` | M1 | Produce a bounded stability receipt for an explicit scene context. |
| `capture_element_snapshot` | M1 | Capture one uniquely resolved element's observed facts and relation metadata. |
| `capture_native_scene` | M1 | Capture a bounded graph with an explicitly stated atomicity authority and artifact capabilities. |
| `capture_visual_evidence` | M0 | Capture lossless raster evidence and its manifest; an optional preview is non-authoritative. |

A completed `wait_for_ui_stable` never authorizes a later capture. `capture_element_snapshot` and `capture_native_scene` perform their own stabilization or revalidate it immediately before committing the evidence. Their response records that capture-time receipt and epoch/sequence. This forbids a prior standalone wait result from creating a time-of-check/time-of-use assumption.

### Native-probe transport and lifecycle

The transport is session-scoped, local-only, and bounded. A connection must positively bind the debug session, the local debuggee PID, and process identity before accepting evidence requests. It uses a server-generated nonce/capability authorization, negotiated protocol and schema versions, request correlation, and exactly one in-flight request per synchronous bridge connection. Connect, write, read, response-byte, graph-size, artifact-size, artifact-chunk, and total-settle limits are negotiated and bounded.

Cancellation cancels the request and triggers bounded process-tree cleanup for server-owned observer/probe components. Cleanup is idempotent. Malformed, oversized, mismatched, timed-out, or disconnected transport is a typed observer/protocol failure, never partial success. The design introduces no remote listener, caller-selected artifact path, arbitrary URI dereference, process scan, or absolute server artifact root. Platform-specific bridge/adapters remain optional subprocesses or opt-in debuggee components under the cross-platform `net8.0` host.

### Snapshot authority, identity, and observed facts

Design identity is `contractId` plus optional `instanceKey` and `templatePart`, constrained by story scope and component slot. `automationId` is fallback/accessibility identity, never canonical design identity.

Only an opt-in **in-process framework probe** can return `COMPLETE` for `capture_native_scene`. It must run one dispatcher-affine, non-yielding transaction; materialize the entire bounded graph into an immutable DTO during that transaction; and bind a probe-owned monotonic layout/state revision sampled before and after materialization. `COMPLETE` requires a declared probe authority and equal valid before/after revisions. The artifact records the authority and both revision values; it is not an accumulation of independently timed reads.

A UIA/FlaUI-only scene traversal is explicitly `uia_guarded` best-effort. It may record before/after window, client, DPI, and fingerprint guards, but `capture_native_scene` returns `PARTIAL` with `ATOMICITY_UNPROVEN_UIA_GUARDED`, or `UNOBSERVABLE` when traversal or guards cannot supply usable evidence. It never returns `COMPLETE` for an atomic-scene claim. A raster is adjacent corroboration with independent capture ID and timestamp; it must not be labeled as the same epoch.

The scene artifact contains observed facts only: element graph; parent/child and slot relations; logical and physical `x`, `y`, `width`, and `height`; DPI; transforms; clip; accessibility; adapter namespaces; and dependency metadata adequate for an external comparator. It may contain adapter-reported effective values or value-source categories only with the adapter namespace and evidence authority that produced them. Unknown semantic adapter data is preserved as typed opaque evidence or reported unsupported; it is never generically interpreted.

### Stabilization

A settle policy is explicit and bounded. Where an adapter declares support, it may require dispatcher idle, stable layout samples, `stableForMs`, disabled-or-finished animations, stable window/client geometry, materialized theme/fixture/focus/selection/scroll state, and a pending-async-load status. Every required scene-context field is either supplied as a constrained value or explicitly `null` to mean “not constrained”; the observer must declare `supported`, `unsupported`, or `unobservable` rather than substitute a meaning-changing default.

The stability receipt names requested and observed conditions, timeout/partial evidence, capture epoch, and sequence. Unsupported required conditions yield `PARTIAL` or `UNOBSERVABLE` observation; stability is never inferred from a screenshot, an elapsed delay, or a previous request.

### Artifacts, retrieval, and provenance

MCP capture responses return a small manifest, never full scene JSON or PNG payload. The server creates a per-session artifact root; writes stage then atomically commit beneath that root. Each manifest reference contains an opaque, unguessable session- and capture-bound `artifactId`, media type, byte length, SHA-256, schema version, capture ID, and retention policy. A relative provenance label may be retained for diagnostics, but is not accepted by any retrieval operation and never identifies the server root. Scene JSON, lossless raw PNG, and previews have separate capabilities. A preview is marked `preview_only` and cannot be observation or comparison authority.

`read_capture_artifact` accepts only `debugSessionId`, `protocolVersion`, `artifactId`, zero-based `offset`, and `maxBytes`; `maxBytes` must be positive and no greater than the negotiated raw-byte chunk limit. It returns at most `maxBytes` raw bytes encoded as padded standard base64, the returned byte count, the requested offset, end-of-artifact state, and the manifest-bound metadata. It never returns an artifact as a capture response, resolves paths, or reveals an absolute root.

Artifacts become immutable once committed. On the first successful read, the server verifies the complete committed file against its manifest SHA-256 and records immutable file identity and length. Each later read verifies that identity and length before emitting a chunk; a mismatch yields `ARTIFACT_INTEGRITY_FAILED` and no bytes. A valid session requesting an unknown, foreign-session, foreign-capture, expired, deleted, or otherwise unavailable capability receives the indistinguishable `ARTIFACT_NOT_FOUND` result with no existence, ownership, retention, size, hash, or path disclosure. Integrity failure is separate because it concerns an already authorized owned artifact and must trigger containment rather than masquerade as missing data.

Every manifest carries candidate/runtime provenance: process ID, HWND when applicable, executable/binary SHA-256, assembly and probe versions, observer versions, contract-set and story hashes, capture timestamp, epoch, sequence, and atomicity authority. Repository/worktree/branch/HEAD/tree facts may be reported only from an explicit launch/probe manifest and must name their source and verification status. They must never be inferred from a working directory or a process scan.

### Outcomes and wire classification

Observation completeness is `COMPLETE`, `PARTIAL`, or `UNOBSERVABLE`; it is not conformance. `COMPLETE` on an atomic scene has only the in-process framework-probe authority described above. Typed errors distinguish invalid arguments, unavailable debug session, unavailable observer, unsupported protocol/capability, missing/ambiguous scene or element, UI not stable, artifact write failure, artifact not found, artifact integrity failure, and candidate mismatch. A raster may corroborate an external comparison but cannot determine a verdict.

Before implementation, the M0 schema gate freezes precise syntax. Its semantic classification is already fixed: syntactically malformed protocol or schema-version input is `INVALID_TOOL_ARGUMENTS`; syntactically valid, known protocol/schema input that the negotiated server does not support is `UNSUPPORTED_PROTOCOL`. The same distinction is exercised by negative wire tests and must not be inferred from an example shape.

## Alternatives considered

### Retain ADR-002's in-host DTCG resolver and `check_element_tokens`

Rejected. It duplicates the future Factory comparison boundary, makes an incomplete geometry observer appear to be a token-provenance authority, and creates an incompatible root-cause surface before the canonical scene artifact exists.

### Treat guarded UIA traversal as atomic

Rejected. Before/after guards can disclose useful instability evidence but cannot convert independently timed multi-element reads into a dispatcher-consistent graph. Only the bounded in-process framework-probe transaction is atomic-scene authority.

### Make screenshots the visual-conformance authority

Rejected. Raster output is sensitive to capture path, DPI, animation, font rasterization, antialiasing, and color management. It also cannot prove property-system or DTCG provenance, and its independently timed capture cannot silently share a scene epoch.

### Expose server-relative or absolute artifact paths to the Factory

Rejected. A path is ambient authority and cannot express session/capture binding, expiry, bounded transport, or indistinguishable foreign-artifact denial. Retrieval must use the opaque capability through `read_capture_artifact`.

### Embed framework/property-system access in the cross-platform host

Rejected. WPF and Avalonia are framework-specific evidence authorities. Direct embedding would weaken optional-platform isolation and still would not establish DTCG token identity without the external Factory.

### Use standalone stabilization as capture authorization

Rejected. UI state can change after a wait receipt. Capture must itself settle or revalidate immediately before it commits evidence.

## Consequences

- The first native work proves an evidence transport, bounded artifact-read capability, and explicitly qualified scene boundary rather than a misleading token-check tool.
- M0 cannot begin primitive implementation until its exact schema artifact, runtime/schema parity, and negative wire tests are accepted.
- M1 depends on an opt-in in-process framework snapshot authority for any `COMPLETE` atomic scene; UIA/FlaUI remains useful but honest guarded fallback evidence.
- Factory integration remains explicit and external; its absence does not block M0/M1 capture implementation, but it can retrieve artifacts only through the primitive.
- Existing Python UI ownership is retained and unmodified. Any future native reuse is a separately scoped implementation decision, not implied by this ADR.
- Framework adapters can add high-authority facts without changing the canonical artifact or claiming a comparison verdict.
- Consumers receive small stable manifests and bounded artifact chunks, limiting MCP transport size and eliminating artifact-path authority.

## Rollback

M0/M1 are additive and have no data migration or public-route cutover. Disable selection of the native observer/probe capability, remove the additive primitive registrations including `read_capture_artifact` and their server-owned artifact implementation, and retain the existing three native tools and unchanged Python route. Per-session artifacts follow their declared retention/cleanup policy. The rollback must not delete or reinterpret retained evidence, alter a Factory-owned contract, reintroduce path retrieval, or introduce a fallback that calls the deferred facade.

## Related records and evidence

- `specs/003-native-scene-observation/spec.md`
- `docs/adr/ADR-001-stateless-dotnet-strangler.md`
- `docs/adr/ADR-002-dotnet-dtcg-element-conformance.md` (historical; superseded)
- [DTCG Format Module 2025.10](https://www.designtokens.org/TR/2025.10/format/) — token exchange format and `group.$root` semantics.
- [DTCG Resolver Module 2025.10](https://www.designtokens.org/TR/2025.10/resolver/) — external Factory concern, not a host implementation commitment.
- [UIA `BoundingRectangle`](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.boundingrectangleproperty?view=windowsdesktop-10.0) — physical geometry fallback evidence.
- [WPF dependency-property value precedence](https://learn.microsoft.com/en-us/dotnet/desktop/wpf/properties/dependency-property-value-precedence) and [`DependencyPropertyHelper.GetValueSource`](https://learn.microsoft.com/en-us/dotnet/api/system.windows.dependencypropertyhelper.getvaluesource?view=windowsdesktop-10.0) — future WPF adapter evidence categories.
- [Avalonia property system](https://docs.avaloniaui.net/docs/guides/property-system) — future Avalonia adapter research reference; integration must re-verify the current official path and contract before implementation.

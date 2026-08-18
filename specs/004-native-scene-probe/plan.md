# Implementation Plan: Native Scene Probe

**Branch**: `work/native-scene-speckit` | **Date**: 2026-08-18 | **Spec**: [spec.md](spec.md)

**Input**: [Feature specification](spec.md), [requirements checklist](checklists/requirements.md), [research](research.md), [data model](data-model.md), [wire schema candidate](contracts/native-scene-probe.schema.json), [artifact schema candidate](contracts/native-scene-artifact.schema.json), [parity corpus](contracts/parity-corpus.json), [ADR-003](../../docs/adr/ADR-003-native-scene-observation.md), and [Spec 003](../003-native-scene-observation/spec.md).

## Summary

Deliver the remaining Phase 1 design artifacts for a native, read-only scene-observation boundary. The planned M0/M1 implementation is pure C#/.NET: a cross-platform `net8.0` MCP host retains debug-session and artifact authority, while the existing Windows C# bridge and a new opt-in `net8.0-windows` WPF probe provide explicitly limited observation evidence.

The route exposes exactly six additive primitives: M0 `get_ui_probe_capabilities`, `capture_visual_evidence`, and `read_capture_artifact`; M1 `wait_for_ui_stable`, `capture_element_snapshot`, and `capture_native_scene`. It returns observed facts, completeness, provenance, and typed uncertainty—not DTCG resolution, token mapping, comparison, a verdict, diagnosis, or repair advice. The external Design Contract Factory remains the only owner of those latter concerns.

This is a planning-only delivery. Publishing or merging this planning packet is allowed and does not constitute T001 approval. T001 records operator approval of the exact merged candidate bytes and authorizes **only** M0-G0 tasks T002–T007; no M0 primitive implementation is authorized until T007 has produced GREEN contract/runtime-validator evidence. T007 proves exact-byte loading, validator parity on concrete fixtures, corpus syntax/reference integrity, classification vocabulary, and negative structural/version cases—not observer, artifact, stability, atomicity, or complete C001–C024 behavior. T032/T034 own the full behavioral C001–C024 execution after M0/M1 implementation. This packet authorizes no product source/test/package change, build, formatter, SpecKit workflow execution, implementation-task execution, release, publication of product artifacts, or route cutover.

## Design Depth and Existing Decision

**Rung: D2 — subsystem/new architectural boundary.** This invocation produces durable implementation-facing artifacts for a new public MCP evidence boundary consumed across future sessions. A wrong boundary would require coordinated changes to the host, Windows observer, test fixture, artifact model, and external consumer contract. The scope remains limited to the already-approved M0-G0/M0/M1 boundary; it does not design M2-M5 or the external Factory.

**Existing ADR**: ADR-003 is the accepted decision record. This plan implements its delivery decomposition; it does not create a competing ADR.

### Alternatives considered

| Shape | Decision | Reason |
|---|---|---|
| C# host owns session/artifact authority; existing Windows C# bridge supplies UIA/raster facts; opt-in WPF probe supplies atomic M1 facts | **Chosen — REVERSIBLE** | Preserves the cross-platform host, confines Windows APIs, retains a bounded C# evidence path, and keeps artifact authority server-side. Additive registration can be removed without changing legacy routes. |
| Extend the retained Python UI route or launch a Python worker from the new route | **Rejected** | Violates the native-only boundary, couples new authority to legacy behavior, and prevents independent rollback. |
| Put WPF/FlaUI/property-system APIs in the `net8.0` host | **Rejected** | Makes the cross-platform host Windows/framework-dependent and does not itself establish atomic snapshot authority. |
| Embed scene JSON/PNG in MCP responses or give the Factory paths to the artifact root | **Rejected** | Violates response bounds and turns ambient filesystem access into retrieval authority. Opaque bounded reads are required. |

## Technical Context

| Context | Planned decision |
|---|---|
| **Language/Version** | C# on .NET 8. The host remains `net8.0`; Windows bridge and WPF probe remain or become `net8.0-windows`. |
| **Primary Dependencies** | Production: existing `ModelContextProtocol`, `Microsoft.Extensions.Hosting`, existing bridge `FlaUI.UIA3` 5.0.0, and existing `System.Drawing.Common` 8.0.10 where their current project already owns them. BCL owns `System.Text.Json`, `System.IO.Pipes`, `FileStream`, `SHA256`, `RandomNumberGenerator`, `TimeProvider`, `Process`, and `SemaphoreSlim`. Tests may add only `NJsonSchema` 11.6.1 and `Microsoft.Extensions.TimeProvider.Testing` 10.9.0 after approval. |
| **Storage** | A host-owned per-session artifact root. Each artifact is staged, flushed/closed, atomically committed, then immutable. Retention ends at the earlier of session stop or 14,400 seconds after commit. |
| **Testing** | xUnit in `host/NetCoreDbg.Mcp.Stateless.Tests`, existing `ModernMcpProcessDriver`, an M0-G0 schema/corpus parity suite, focused host/bridge lifecycle tests, and a planned opt-in WPF fixture. Time-dependent behavior uses `FakeTimeProvider`, not sleeps. |
| **Target Platform** | Cross-platform native MCP host. Windows-only UIA/raster bridge and WPF probe are capability-negotiated optional components; a non-Windows host declares them unavailable rather than importing Windows-only dependencies. |
| **Project Type** | MCP tool server with a native debug-session registry, a local observer/probe boundary, and server-owned immutable evidence storage. |
| **Performance/size goals** | At most 65,536 raw bytes per artifact read; lossless visual artifact at most 67,108,864 bytes; scene artifact at most 16,777,216 bytes; structured response at most 262,144 bytes; `selectedState` and `currentState` each at most 262,144 serialized UTF-8 bytes before DTO materialization; scene graph at most 4,096 nodes; four artifact descriptors; 256 issues; settle timeout at most 30,000 ms. |
| **Safety constraints** | One synchronous bridge connection has one in-flight correlated request; all local I/O, connect/write/read, response, graph, artifact, and settle budgets are enforced. Cancellation triggers bounded idempotent cleanup. No network listener, target scan, caller-selected output path, path/root retrieval, arbitrary URI, or MCP stdout diagnostics is introduced. |
| **Scope** | M0-G0, M0, and M1 only. M2 WPF presentation facts, M3 source/resource provenance, M4 complex-control coverage, and M5 Avalonia coverage remain future work requiring new approval and specification. |

### Frozen candidate constraints to approve at M0-G0

The only authoritative schema locations are `specs/004-native-scene-probe/contracts/native-scene-probe.schema.json` and `specs/004-native-scene-probe/contracts/native-scene-artifact.schema.json`; `parity-corpus.json` is the accompanying representative exchange corpus. Future binaries may embed or copy those exact approved bytes, but may not define duplicate authoritative schemas elsewhere.

M0-G0 acceptance covers the following exact contract constraints in addition to the bounds above:

- `artifactId`, `captureId`, and probe capability/authorization IDs are public opaque capabilities generated from at least 128 bits of CSPRNG output and encoded as base64url in the inclusive length range 22–86. They are non-enumerable and session/capture/probe-bound.
- `debugSessionId` remains a compatibility handle with a minimum accepted length of 16; it is not promoted into artifact or probe authority.
- The capability declaration retains its `primitives` array but has exactly six entries: its Draft-7 `minItems: 6`, `maxItems: 6`, and six `allOf`/`contains` constraints require one entry for each approved name. Each `primitiveCapability` is structurally constrained by `oneOf` fixed `{name, milestone}` pairs: `get_ui_probe_capabilities`, `capture_visual_evidence`, and `read_capture_artifact` map only to M0; `wait_for_ui_stable`, `capture_element_snapshot`, and `capture_native_scene` map only to M1. Runtime also asserts the exact paired six-entry set with no duplicate or omission.
- `supportedProtocolVersions` and `supportedSchemaVersions` are each Draft-7 non-empty unique arrays that `contains` the active `native-scene-probe/1` and `native-scene-probe.schema/1` value, respectively.
- Capability output declares an explicit state for each requested settle condition—`dispatcherIdle`, `stableLayout`, `animationState`, `windowGeometry`, `contextMaterialization`, and `asyncLoadSettled`—rather than inferring support from a request. It structurally fixes `settleSampleCountMin` to 2 and `settleSampleCountMax` to 16; request `sampleCount` remains inclusive 2–16.
- Custom opaque JSON permits arrays and objects of at most 256 members, rejects nesting deeper than 16 at runtime before DTO materialization, and rejects a serialized UTF-8 custom payload exceeding 262,144 bytes. `selectedState` and `currentState` are independently measured before DTO materialization and each reject serialized UTF-8 input above 262,144 bytes. The host preserves a negotiated unknown namespace as opaque evidence or reports it unsupported; it never interprets it as design semantics.
- Every element snapshot and every native-scene result branch that can return or commit capture evidence has capture-time `revalidatedByCapture: true`, including `PARTIAL` and `UNOBSERVABLE`; a standalone wait receipt alone has `false`.
- `captureManifestBase.evidenceScope` is an evidence-scope-or-null member. A visual manifest requires the non-null scope and runtime equality to its `capture_visual_evidence` request; element and native-scene manifests require `null`.
- `toolError` is exactly six tool-specific `oneOf` branches. Each accepts the common boundary outcomes plus only its mapped operation outcomes; cross-paired tool/code envelopes are schema-rejected. `ARTIFACT_NOT_FOUND` remains exactly `{kind: "tool_error", tool: "read_capture_artifact", code: "ARTIFACT_NOT_FOUND", message: "Artifact is not available."}`—no additional member, artifact metadata, or free-text variation.
- A `capture_visual_evidence` result with completeness `COMPLETE` has at least one artifact descriptor with `mediaType: image/png` and `evidenceGrade: lossless_visual`; a `capture_native_scene` result with completeness `COMPLETE` has at least one descriptor with `mediaType: application/vnd.netcoredbg.native-scene+json` and `evidenceGrade: observed_facts`; a `capture_element_snapshot` result with completeness `COMPLETE` has that descriptor for a retrievable one-node `element_snapshot` artifact. Element `PARTIAL` may retain committed qualified facts; element `UNOBSERVABLE` has no artifacts. A persisted `native_scene` artifact with `PARTIAL` uses only in-process or UIA-guarded atomicity; UIA retains unchanged guards and the atomicity issue, while element artifacts retain `not_applicable`.
- `captureArtifactChunk.byteLength` conditionally caps native-scene JSON at 16,777,216 bytes and retains the 67,108,864-byte cap for PNG/WebP.
- Contract/runtime-validator parity at M0-G0 covers exact-byte schema loading; closed roots, formats, scalar/collection bounds, version syntax and active-version array membership, fixed primitive-name/milestone pairs, fixed 2/16 capability constants, scope/null branch shapes, permitted error code/tool pairs, required COMPLETE descriptors, and request/result validator agreement on concrete fixtures; corpus syntax/internal references and expected classification vocabulary; and negative structural/version cases. Runtime implementation behavior—including session binding, observer output containment, commit hashing, file identity, per-read touched-chunk hashing, artifact operations, capture revalidation, and dispatcher transactions—remains unexecuted until M0/M1 and is exercised comprehensively at T032/T034.
- Malformed protocol/schema-version syntax is `INVALID_TOOL_ARGUMENTS`; syntactically valid known-but-unsupported versions are `UNSUPPORTED_PROTOCOL`; structurally valid requests for unavailable declared capabilities are `UNSUPPORTED_CAPABILITY`.

These are candidate contract acceptance conditions, not a claim that product code, schemas, validators, or parity tests were changed in this documentation delivery. T007 validates the contract gate only; it does not make a behavioral GREEN claim.

## Constitution Check

No repository constitution file exists. This project therefore uses the following binding gates from the operator assignment, `AGENTS.md`, ADR-003, Spec 003, and the feature contract packet. The check is passed for this documentation-only Phase 1 delivery and must be re-checked before any future implementation slice.

| Gate | Evidence required before implementation | Phase 1 result |
|---|---|---|
| Explicit approval boundary | T001's durable operator Approval Record names the exact candidate schema and corpus bytes and authorizes only M0-G0 T002–T007. T007's GREEN is validator/corpus-integrity evidence only; primitive implementation starts only after it passes. | **PASS — no implementation attempted** |
| Single canonical contract | The two schema files and corpus in this feature directory are the only authoritative bytes; future runtime embedding/copying preserves them exactly. | **PASS — no duplicate schema created** |
| Native-only route | New M0/M1 code is C#/.NET only and neither invokes nor depends on Python, Python workers, pythonnet, or new Python product code. | **PASS — design mandates the boundary** |
| Host/platform isolation | The host remains `net8.0`; bridge/probe Windows code stays `net8.0-windows` and optional. | **PASS — documented structure respects TFMs** |
| Dependency budget | BCL and existing host/bridge packages serve production; NJsonSchema 11.6.1 and FakeTimeProvider 10.9.0 are test-only candidates. JsonSchema.Net, StreamJsonRpc, MessagePack, protobuf/gRPC, ImageSharp, DTCG packages, and Python interop are excluded. | **PASS — no package edit** |
| Observation-only authority | No DTCG resolver/comparator, `PASS`/`FAIL`, root cause, repair advice, Factory/Gallery implementation, or `check_element_tokens` is registered by M0/M1. | **PASS — planned interface is bounded** |
| Planning-only scope | This feature directory may contain and publish the complete SpecKit planning packet, including unchecked `tasks.md` and candidate schemas/corpus. Planning commit/push/PR/merge is allowed and does not satisfy T001. No SpecKit workflow file/run, product source/test/package edit, implementation command, build, formatter, external agent CLI, product publication, or implementation task is authorized. | **PASS — planning artifacts only; all tasks unchecked** |
| Honest atomicity | Only a WPF in-process non-yielding immutable-DTO transaction with equal probe revisions can report atomic `COMPLETE`; UIA/FlaUI is `uia_guarded` `PARTIAL`/`UNOBSERVABLE`; raster has independent timing. | **PASS — authority split explicit** |
| Compatibility and release restraint | Existing `start_debug`, `get_debug_state`, `stop_debug`, and retained Python route remain unchanged except future additive registrations. No public route selection, cutover, release, package publication, or Factory deployment belongs to this feature. | **PASS — no execution or release activity** |
| Session/artifact safety | Positive local session/candidate binding, capability authorization, bounded local IPC, immutable artifacts, opaque reads, non-disclosure, SHA-256 verification, and 4-hour-or-stop cleanup are preserved. | **PASS — gates mapped below** |

## Project Structure

### Documentation (this feature)

```text
specs/004-native-scene-probe/
├── spec.md                                  # Approved feature intent and acceptance criteria
├── checklists/
│   └── requirements.md                      # Completeness receipt
├── research.md                              # Phase 0/1 dependency and platform decisions
├── data-model.md                            # Candidate entities, invariants, and limits
├── contracts/
│   ├── native-scene-probe.schema.json       # Candidate wire schema; later authoritative bytes
│   ├── native-scene-artifact.schema.json    # Candidate artifact schema; later authoritative bytes
│   └── parity-corpus.json                   # Candidate representative exchanges
├── plan.md                                  # This Phase 1 implementation plan
├── architecture.md                          # Component, authority, lifecycle, and data-flow design
├── quickstart.md                            # Future acceptance playbook; not an executed receipt
└── tasks.md                                 # Unchecked dependency-ordered future work; T001 approval gate
```

`tasks.md` is present as the requested Phase 2 planning artifact, but every item remains unchecked. T001 authorizes only M0-G0 T002–T007; T008+ remains blocked until GREEN T007 contract-validator/corpus-integrity evidence. SpecKit workflow files/runs, executed commands, and generated execution reports are deliberately absent.

### Planned source and test structure (not created by this delivery)

```text
host/
├── NetCoreDbg.Mcp.Stateless/
│   ├── Program.cs                           # Existing MCP composition; future additive registrations only
│   ├── DebugAdapter/
│   │   ├── NetCoreDbgSession.cs             # Existing DAP/process authority seam
│   │   └── DapSessionState.cs               # Existing session-state seam
│   ├── NetCoreDbg.Mcp.Stateless.csproj      # Future exact-byte embedded-resource link, if approved
│   └── NativeScene/                         # Planned M0/M1 native implementation namespace
│       ├── NativeSceneContractCatalog.cs    # Exact approved schema/corpus bytes and version classification
│       ├── NativeSceneToolDispatcher.cs     # Six additive primitive dispatch and typed envelopes
│       ├── NativeSceneSessionBinding.cs     # Debug-session/PID/process-identity/nonce authority
│       ├── NativeSceneBridgeClient.cs       # Bounded local C# bridge/probe client
│       ├── NativeSceneArtifactStore.cs      # Staging, commit, retention, integrity, opaque reads
│       ├── NativeSceneStabilityCoordinator.cs # M1 settle and capture-time revalidation
│       └── NativeSceneCaptureCoordinator.cs # M1 element/scene capture and authority qualification
├── NetCoreDbg.Mcp.Stateless.Tests/
│   ├── ModernMcp/                           # Existing process driver and front-door contract conventions
│   ├── NativeScene/                         # Planned focused tests
│   │   ├── NativeSceneSchemaParityTests.cs
│   │   ├── NativeSceneNegativeWireTests.cs
│   │   ├── NativeSceneCapabilityTests.cs
│   │   ├── NativeSceneArtifactStoreTests.cs
│   │   ├── NativeSceneBridgeLifecycleTests.cs
│   │   ├── NativeSceneVisualEvidenceTests.cs
│   │   ├── NativeSceneStabilityTests.cs
│   │   └── NativeSceneAtomicityTests.cs
│   └── Fixtures/
│       └── NativeSceneProbe.WpfFixture/     # Planned M1 opt-in WPF test executable/project
│           ├── NativeSceneProbe.WpfFixture.csproj
│           ├── App.xaml
│           └── ProbeFixtureWindow.xaml.cs
└── NetCoreDbg.Mcp.DesignProbe.Wpf/           # Planned M1 opt-in in-process WPF probe project
    ├── NetCoreDbg.Mcp.DesignProbe.Wpf.csproj
    ├── LocalProbeClient.cs
    ├── WpfAtomicSnapshotTransaction.cs
    └── WpfSceneSnapshotDto.cs

bridge/
├── Program.cs                               # Existing bridge process; planned local-pipe mode preserves stdin mode
├── JsonRpcHandler.cs                        # Existing command registry; planned additive evidence handler registration
└── Commands/
    ├── ScreenshotCommands.cs                # Existing lossless raster evidence owner reused by M0
    ├── ElementCommands.cs                   # Existing UIA identity/geometry owner reused by M1 fallback
    └── NativeSceneEvidenceCommands.cs        # Planned bounded capability/raster/guarded-fact bridge command

specs/004-native-scene-probe/contracts/      # The sole schema/corpus source; no source-tree duplicate
```

**Structure decision**: retain the existing native host as the sole MCP/session/artifact owner and add one `NativeScene` namespace under it. Retain the bridge as the Windows-only UIA/raster provider. Add the WPF probe only as an opt-in `net8.0-windows` component and fixture for M1 atomic authority. This preserves the existing host/bridge split and avoids a new service, database, route, package, or Python seam.

## Component Boundaries and Planned Interfaces

| Component | Milestone | Responsibility | May depend on | Must not own |
|---|---|---|---|---|
| `Program` + `DebugSessionRegistry` | M0 | MCP composition, existing debug-session lifecycle, additive dispatch wiring, host shutdown cleanup | Existing DAP session types and `NativeSceneToolDispatcher` | Windows UI APIs, artifact paths, Factory logic |
| `NativeSceneContractCatalog` | M0-G0 | Exact candidate/approved schema bytes, compatibility/version classification, schema/corpus lookup for tests | Feature contract files as embedded/copied exact bytes, BCL JSON | Duplicate schema definitions or a private competing protocol |
| `NativeSceneSessionBinding` | M0 | Explicit `debugSessionId` lookup, positive local PID/process identity, nonce/capability binding | Existing session registry, BCL crypto/process primitives | Process scan, remote target selection, connection-global target selection |
| `NativeSceneBridgeClient` | M0 | Spawn/connect/serialize bounded C# bridge or probe work, correlation, one in-flight request, cancellation cleanup | BCL pipes/process/semaphore | Python, network listener, authoritative design conclusions |
| `NativeSceneArtifactStore` | M0 | Server-owned root, staged/atomic commits, immutable descriptors, retention, bounded reads, hash/identity checks | BCL files/crypto/time | Caller paths, external root access, mutable artifact replacement |
| Existing bridge evidence commands | M0/M1 | Lossless PNG, process/window identity, UIA accessibility/geometry, guarded traversal facts | Existing FlaUI/System.Drawing dependencies | Atomic `COMPLETE`, computed-style or DTCG authority |
| `NativeSceneStabilityCoordinator` | M1 | Bounded settle receipt and immediate capture-time revalidation | Declared adapter/probe capability | Authorization from an older receipt |
| `NativeSceneCaptureCoordinator` | M1 | Element/scene manifest, typed uncertainty, atomicity qualification, scene-artifact hand-off | Store, bridge client, optional probe | DTCG verdict or token mapping |
| `NetCoreDbg.Mcp.DesignProbe.Wpf` | M1 | Test/Gallery-only local probe protocol and dispatcher-affine immutable scene DTO transaction | WPF, local capability authorization | Production listener, session discovery, Factory logic |
| Design Contract Factory | External | DTCG Format/Resolver semantics, comparison, diagnosis, repair planning, artifact consumption through MCP | Opaque capability and bounded `read_capture_artifact` | Debug-session authority, artifact root, direct paths |

## Phased Decomposition

All tickets below are future execution work. They become executable only after the Approval Record permits M0-G0 work. A ticket's acceptance checkpoint is independent and its blocking edge prevents accidental out-of-order implementation.

| Ticket | Type | Scope and blocking edge | Acceptance checkpoint |
|---|---|---|---|
| NSP-G0-01 | Input | Record operator approval for exact candidate schema/corpus bytes and the normative capability, JSON, and sample-count constraints. Blocks every later ticket. | Durable Approval Record identifies the two schema files, corpus, exact byte hashes, operator decision, and approval time. |
| NSP-G0-02 | Test/design gate | Prove exact-byte Draft-7 request/result contract-validator parity on concrete fixtures after NSP-G0-01. No primitive registration or live observer/artifact/stability/atomicity behavior belongs here. | Exact approved bytes load; validators agree on contract-gate fixtures; corpus syntax, internal references, classification vocabulary, and `contractGateExpectation`/`runtimeBehaviorRequired` metadata are valid for C001–C024; closed roots/bounds, active-version non-empty/unique/contains rules, capture scope/null branches, conditional chunk lengths, fixed primitive-name/milestone pairs, and rejected tool/code cross-pairs are enforced; malformed version syntax is `INVALID_TOOL_ARGUMENTS`; valid unsupported versions are `UNSUPPORTED_PROTOCOL`. |
| NSP-M0-01 | Code | Add native capability/session/provenance front door after M0-G0. | Focused host test shows positive local session/candidate binding, six-name catalog metadata with M0 availability, and unchanged legacy three-tool contracts. |
| NSP-M0-02 | Code | Add the bounded native C# bridge client and server-owned artifact lifecycle after NSP-M0-01. | Focused test proves nonce/correlation, one in-flight bridge operation, cancellation cleanup, staged atomic commit with public full SHA-256 and server-internal fixed 65,536-byte chunk hashes, CSPRNG opaque IDs, 4-hour-or-stop retention, and typed unavailable/integrity outcomes. |
| NSP-M0-03 | Code | Add M0 lossless visual capture and bounded chunk retrieval after NSP-M0-02. | A Windows fixture produces a compact PNG manifest whose `COMPLETE` result includes at least one `image/png`/`lossless_visual` descriptor; each authorized read verifies identity/length and every touched chunk before release, and chunks reconstruct its hash/length without a path/root or embedded lossless bytes. |
| NSP-M0-04 | Test | Prove M0 external behavior after NSP-M0-01 through NSP-M0-03. | RED-then-GREEN evidence covers capabilities, candidate mismatch, unavailable observer, raw PNG/preview separation, range boundaries, foreign/expired/nonexistent non-disclosure, and same-identity/same-length in-place tampering after a prior success, including an unaligned two-chunk read that contains no bytes. |
| NSP-M1-01 | Code | Add settle receipts and capture-time revalidation after M0 and an available probe/bridge capability. | A changed scene after `wait_for_ui_stable` is re-settled/revalidated during capture; every evidence-returning or committing element/scene result, including qualified branches, records `revalidatedByCapture: true`; an old success cannot authorize the new capture. |
| NSP-M1-02 | Code | Add the opt-in WPF probe and immutable dispatcher transaction after NSP-M1-01. | WPF fixture proves one non-yielding materialization transaction with equal probe-owned before/after revisions before `COMPLETE` is emitted with at least one `application/vnd.netcoredbg.native-scene+json`/`observed_facts` descriptor. |
| NSP-M1-03 | Code/Test | Add bounded element/scene artifacts and UIA guarded fallback after NSP-M1-01; atomic branch also depends on NSP-M1-02. | Unique/missing/ambiguous identity, geometry/DPI/clip/relations, opaque adapter facts, changed revisions, and UIA traversal during mutation result in the prescribed complete/partial/unobservable classification. `COMPLETE` element snapshots retrieve a one-node `element_snapshot` artifact; `PARTIAL` may retain qualified facts and `UNOBSERVABLE` has none. |
| NSP-M1-04 | Review | Independently inspect the M0/M1 candidate after all M0/M1 tests pass. | Verdict explicitly confirms no Python dependency/change, no Factory/Gallery/comparator, no screenshot verdict path, no artifact path/root retrieval, and no M2-M5 scope. |

### Milestone map

| Milestone | Independently demonstrable outcome | Included tickets | Binding constraints |
|---|---|---|---|
| M0-G0 — frozen wire | The evidence contract has one reviewed byte-identical schema/corpus definition, validated contract-gate fixtures, and unambiguous declared classifications; no primitive implementation or live behavior has started. | NSP-G0-01, NSP-G0-02 | Recorded operator approval is mandatory; this is a contract gate, not behavioral evidence or a public release. |
| M0 — attributable lossless evidence | A bound native session can declare M0 support, create an immutable lossless PNG manifest with the required `image/png`/`lossless_visual` descriptor, and return only authorized bounded chunks with provenance and integrity containment. | NSP-M0-01 through NSP-M0-04 | Depends on M0-G0; C# only; host stays cross-platform; no scene verdict/Factory; no Python route change or public cutover. |
| M1 — qualified scene facts | A bound native session can stabilize/revalidate, capture element/scene facts, distinguish an in-process WPF atomic transaction from guarded UIA, and persist bounded scene evidence. A `COMPLETE` native scene has the required `application/vnd.netcoredbg.native-scene+json`/`observed_facts` descriptor; a `COMPLETE` element snapshot retrieves a one-node `element_snapshot` artifact. | NSP-M1-01 through NSP-M1-04 | Depends on M0; `COMPLETE` native scenes require the WPF probe transaction; UIA remains `PARTIAL`/`UNOBSERVABLE`; raster is independently timed corroboration. |

These are internal capability milestones, not release, package-publication, selection, or migration commitments. M2-M5 are intentionally not designed or scheduled here.

## Requirements-to-Files Map

| Requirements | Milestone/ticket | Existing and planned file ownership |
|---|---|---|
| FR-001–FR-003, SC-001 | M0-G0 / NSP-G0-01–02 | Authoritative candidate bytes: `specs/004-native-scene-probe/contracts/{native-scene-probe.schema.json,native-scene-artifact.schema.json,parity-corpus.json}`; planned verifier: `host/NetCoreDbg.Mcp.Stateless.Tests/NativeScene/{NativeSceneSchemaParityTests.cs,NativeSceneNegativeWireTests.cs}`. |
| FR-004–FR-005, FR-020, SC-007 | M0 / NSP-M0-01 | Existing: `host/NetCoreDbg.Mcp.Stateless/{Program.cs,DebugAdapter/NetCoreDbgSession.cs,DebugAdapter/DapSessionState.cs}`. Planned: `NativeScene/{NativeSceneToolDispatcher.cs,NativeSceneSessionBinding.cs,NativeSceneContractCatalog.cs}` and `NativeSceneCapabilityTests.cs`. |
| FR-006–FR-007, FR-011, SC-002 | M0 / NSP-M0-02–03 | Existing Windows source: `bridge/{Program.cs,JsonRpcHandler.cs,Commands/ScreenshotCommands.cs}`. Planned: `bridge/Commands/NativeSceneEvidenceCommands.cs`, host `NativeScene/{NativeSceneBridgeClient.cs,NativeSceneArtifactStore.cs}`, and `NativeSceneVisualEvidenceTests.cs`. |
| FR-008–FR-010, NFR artifact limits, SC-003 | M0 / NSP-M0-02–04 | Planned host `NativeSceneArtifactStore.cs`; planned tests `NativeSceneArtifactStoreTests.cs` and `NativeSceneBridgeLifecycleTests.cs`. |
| FR-012–FR-013, SC-005 | M1 / NSP-M1-01 | Planned host `NativeScene/NativeSceneStabilityCoordinator.cs` and `NativeSceneStabilityTests.cs`; all `sceneRequest` shape remains defined only by the contract files. |
| FR-014–FR-016, SC-004 | M1 / NSP-M1-02–03 | Planned host `NativeSceneCaptureCoordinator.cs`; planned WPF probe `host/NetCoreDbg.Mcp.DesignProbe.Wpf/{LocalProbeClient.cs,WpfAtomicSnapshotTransaction.cs,WpfSceneSnapshotDto.cs}`; planned fixture `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/NativeSceneProbe.WpfFixture/`; planned test `NativeSceneAtomicityTests.cs`. |
| FR-017–FR-018, FR-021, SC-006 | M0/M1 / NSP-M0-01 and NSP-M1-04 | Planned `NativeSceneToolDispatcher.cs`, manifest contract tests, and independent boundary review. The Design Contract Factory has no repository source path and receives artifacts only through `read_capture_artifact`. |
| FR-019, M2-M5 exclusion | Every milestone / NSP-M1-04 | This plan, [architecture.md](architecture.md), and [quickstart.md](quickstart.md); no M2-M5 source path is authorized. |

## Dependency and Rollback Strategy

### Dependency order

1. T001 records approval of the exact candidate bytes and authorizes only M0-G0 work T002–T007.
2. GREEN T007 proves exact-byte contract/runtime-validator parity, corpus integrity, and negative structural/version classification only; it authorizes M0 primitive implementation T008+ but does not prove any live C001–C024 behavior. T032/T034 later execute every C001–C024 behaviorally.
3. M0 establishes host session binding, C# bridge lifecycle, immutable store, capability declaration, lossless visual capture, and artifact reading.
4. M1 reuses the M0 session/store boundary for settle/revalidation and qualified element/scene capture. The WPF probe is an additional opt-in authority only for `COMPLETE` atomic scenes.
5. A future Factory consumes M0/M1 artifacts through MCP. Its absence does not block observation capture and does not cause fallback comparison behavior.

This is a strangler-compatible additive path: the native route is added alongside the retained Python route without invoking, adapting, or migrating callers to Python. Existing native three-tool behavior remains unchanged. There is no user/data migration and no public route cutover in M0/M1.

### Rollback

M0/M1 can be withdrawn by disabling/removing the additive primitive registrations, native scene dispatcher, bridge/probe launch path, and server-owned artifact components as one bounded feature surface. The existing debug-session core, three existing native tools, and retained Python route remain intact.

Rollback must preserve these invariants:

- retained artifacts follow their declared 4-hour-or-session-stop lifecycle; rollback neither reinterprets them nor turns paths into capabilities;
- no rollback path invokes Python, adds a compatibility alias, restores `check_element_tokens`, or creates a private design comparator;
- no Factory contract, debug-session contract, or existing native tool schema changes;
- the WPF probe remains opt-in and is not converted into a long-running production listener.

## Inline Design Challenge — FULL

**Verdict: GO.** The smallest viable design is the six-primitive evidence boundary already fixed by ADR-003 and the feature contract. The plan uses existing host session lifecycle, existing C# bridge raster/UIA ownership, existing modern-MCP test driver, and the candidate schemas/corpus; it does not create a second observer stack, Factory, or migration.

| Finding | Tag | Evidence |
|---|---|---|
| Extending Python would reduce initial local reuse but violates the stated native-only route and collapses rollback independence. | trade-off | ADR-003 ownership split; research native-only disposition. |
| Direct artifact paths would be mechanically simpler but grant ambient storage authority and cannot implement session/capture binding or indistinguishable non-disclosure. | actionable | ADR-003 artifact retrieval decision; FR-008–010. |
| WPF project scope is limited to opt-in atomic authority and its fixture; M2 presentation/provenance work is not pulled forward. | contract-gap resolved by scope | Spec FR-019 and the M1 authority rule. |
| The existing bridge currently uses stdin JSON-RPC; a bounded C# local-pipe mode must preserve its current mode rather than replace it. | actionable | `bridge/Program.cs` and `JsonRpcHandler.cs` current ownership. |
| A standalone wait receipt could appear to simplify capture, but it creates a time-of-check/time-of-use error. | actionable | FR-013 and C014. |
| A broad new transport framework might shorten code but adds dependencies and fails the BCL-only production decision. | noise | Research rejects StreamJsonRpc, MessagePack, protobuf/gRPC, and ImageSharp. |

| Full-check dimension | Result |
|---|---|
| Staleness | Checked against current branch source seams, ADR-003, Spec 003, feature packet, and contract files. |
| False dependencies | Factory availability and M2-M5 are not M0/M1 dependencies; only the M1 `COMPLETE` branch requires an opt-in WPF authority. |
| Complexity | One host namespace plus bounded bridge/probe seams reuses existing ownership; no new service, database, resolver, or generic RPC stack is introduced. |
| Value comparison | M0 creates independently useful attributable lossless evidence and safe retrieval; M1 adds qualified scene facts without pretending to solve comparison. |
| Scope creep | Explicitly excludes Python changes, Factory/Gallery, comparator, `check_element_tokens`, cutover, release, and M2-M5. |
| Assumptions | Local debug session, identifiable candidate, C# bridge, and WPF fixture are named; absence is a typed unavailable/partial condition, not fabricated evidence. |
| Cognitive bias | The plan rejects anchoring on existing Python/bridge behavior as authorization for the new route. |
| Security assumptions | Opaque CSPRNG capability, local binding, nonce, bounded IPC, root isolation, non-disclosure, and integrity containment are direct design gates. |
| Cross-reference | The only external consumer is the Factory, constrained to opaque MCP reads and denied debug/session/storage authority. |

## Complexity Tracking

No constitution exception is requested. The planned Windows bridge/probe split is required to preserve the cross-platform host boundary, not an additional service or abstraction layer.
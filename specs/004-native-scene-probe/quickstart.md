# Native Scene Probe — Future Acceptance Playbook

## Status: NOT EXECUTED

This is a future acceptance playbook, not an execution receipt. The planning packet may be committed, reviewed, and merged without constituting T001 approval. The M0/M1 source files, test classes, WPF fixture, bridge mode, package changes, and registration described below do not exist in this documentation-only delivery. No command in this file has been run, and no product build, test, formatter, package restore, implementation PR, release, product publication, or public-route cutover is authorized now.

T001 operator approval authorizes only M0-G0 T002–T007. M0/M1 primitive implementation and acceptance remain blocked until T007 has produced GREEN M0-G0 contract/runtime-validator evidence:

1. T001's Approval Record names the exact candidate bytes in `contracts/native-scene-probe.schema.json`, `contracts/native-scene-artifact.schema.json`, and `contracts/parity-corpus.json`; and
2. T002–T007 prove exact-byte schema loading, request/result validator parity on concrete fixtures, corpus syntax/internal-reference integrity, expected classification vocabulary, fixed primitive-name/milestone pairing, and negative structural/version cases.

T007 does not execute observer, artifact, stability, or atomicity behavior and does not require full behavioral C001–C024 GREEN. T032 and T034 run all C001–C024 behaviorally and record the complete mapping after M0/M1 implementation.

M0 and M1 are internal capability acceptance milestones only. They do not authorize a Python change, a Design Contract Factory/Gallery implementation, `check_element_tokens`, a route selection, a package release, or M2-M5 work.

## 1. Prerequisites for the future approved run

| Requirement | Required condition | Expected evidence |
|---|---|---|
| Contract authority | Approval Record identifies the exact two schema files and parity corpus, their byte hashes, operator decision, and approval time. | Reviewable approval record; no reliance on a copied/renamed schema. |
| SDK and projects | .NET 8 SDK restores and builds the approved host/test projects. The host is `net8.0`; Windows bridge and WPF fixture/probe are `net8.0-windows`. | Focused command output after approval, with no production package beyond the decided budget. |
| Test-only dependencies | `NJsonSchema` 11.6.1 and `Microsoft.Extensions.TimeProvider.Testing` 10.9.0 are present only in the future test graph if M0-G0 added them. | Project-file review and focused test restore output; no production reference. |
| Windows evidence environment | M0 lossless capture and M1 WPF atomic scenarios run on Windows with the existing C# FlaUI bridge available. | Capability declaration names supported Windows features; an unavailable component is reported, not replaced with Python. |
| Debuggee | An explicit local controlled DAP/debuggee fixture can be started and positively bound to a `debugSessionId`, PID, and process identity. | Test driver creates an explicit native session; no process scan occurs. |
| WPF atomic fixture | The future `NativeSceneProbe.WpfFixture` loads the opt-in probe only for its test/Gallery scenario. | Fixture can report a probe-owned revision; it has no production listener. |
| Artifact clock | The future artifact store receives `TimeProvider`; expiry tests use a fake clock. | Four-hour and session-stop expiry evidence contains no sleeps or wall-clock race. |

Use the repository's existing focused-host pattern: `ModernMcpProcessDriver` starts the native host and controlled debug adapter, captures MCP structured content, and disposes the process tree. The planned WPF fixture extends that controlled-fixture pattern; it must not target a random desktop process.

## 2. Planned focused commands

The commands below are exact future commands. They are intentionally marked **NOT EXECUTED** because their named test classes are planned source paths in [plan.md](plan.md).

### M0-G0 — freeze and prove the wire

```powershell
# NOT EXECUTED — run only after recorded operator approval and M0-G0 implementation.
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj -c Debug --filter "FullyQualifiedName~NativeSceneSchemaParityTests|FullyQualifiedName~NativeSceneNegativeWireTests" -v minimal
```

Expected evidence:

- the exact approved schema/corpus bytes are the only contract source used by the tests;
- every C001–C024 corpus record has valid syntax and internal references, one allowed expected-classification vocabulary value, and explicit `contractGateExpectation` plus `runtimeBehaviorRequired` metadata;
- request and result validators agree with the frozen schemas on concrete contract-gate fixtures, including closed roots, `sceneRequest`, scalar/collection limits, and result/error envelopes;
- malformed protocol/schema-version syntax is `INVALID_TOOL_ARGUMENTS`, while syntactically valid `native-scene-probe/2` / `native-scene-probe.schema/2` input is `UNSUPPORTED_PROTOCOL`;
- the capability declaration retains exactly six entries: Draft-7 `minItems: 6`, `maxItems: 6`, and one `allOf`/`contains` rule per name; each `primitiveCapability` uses `oneOf` fixed name/milestone pairs, with the three M0 names paired only to M0 and the three M1 names only to M1; the runtime validator rejects an omission, duplicate, or cross-milestone pair;
- structural negatives cover closed-root violations, invalid range/identifier, `sampleCount` below 2 or above 16, object/array with more than 256 members, nesting deeper than 16, custom payload larger than 262,144 UTF-8 bytes, and `selectedState`/`currentState` larger than 262,144 serialized UTF-8 bytes;
- this gate does not invoke an observer, commit/read an artifact, perform stability/revalidation, or exercise atomicity. Runtime-behavior cases, including C005–C016 and C020, are deferred to T032/T034.

### M0 — native capability, bridge, visual artifact, and bounded reads

```powershell
# NOT EXECUTED — run only after M0-G0 is accepted and M0 implementation exists.
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj -c Debug --filter "FullyQualifiedName~NativeSceneCapabilityTests|FullyQualifiedName~NativeSceneArtifactStoreTests|FullyQualifiedName~NativeSceneBridgeLifecycleTests|FullyQualifiedName~NativeSceneVisualEvidenceTests" -v minimal
```

Expected evidence:

- capability declaration lists exactly the six contract primitive names once with only their fixed milestones—M0 for `get_ui_probe_capabilities`, `capture_visual_evidence`, and `read_capture_artifact`; M1 for `wait_for_ui_stable`, `capture_element_snapshot`, and `capture_native_scene`—and declares protocol/schema versions, limits, explicit settle-condition states, negotiated `sampleCount` minimum 2 / maximum 16, context support, namespaces, and candidate provenance; it never enumerates `artifactId`;
- a valid local session/candidate binding is required before bridge/probe or artifact activity; stale, missing, remote, or mismatched bindings do not trigger target discovery;
- the existing C# bridge supplies a lossless PNG through the native C# route, while the retained Python route is neither called nor changed;
- capture response is a compact manifest, not raw PNG bytes; every `COMPLETE` `capture_visual_evidence` result has at least one descriptor with `mediaType: image/png` and `evidenceGrade: lossless_visual`, with its own `rasterCaptureId`, timestamp, length, SHA-256, schema version, capture ID, and `session-until-stop-or-4h-gc` retention. `PARTIAL`/`UNOBSERVABLE` may have zero artifacts only when evidence could not be committed;
- new artifact/capture/probe capability IDs are base64url, 22–86 characters, and derive from at least 128 CSPRNG bits; `debugSessionId` accepts the compatibility minimum of 16 without becoming artifact authority;
- chunk reads at offset 0, an interior boundary, and end-of-artifact reconstruct the original bytes and hash. A 65,536-byte request never returns more raw bytes, and a terminal empty range returns `bytesRead: 0`, `dataBase64: ""`, `endOfArtifact: true`;
- foreign, unknown, expired, deleted, and unavailable IDs produce exactly `{kind: "tool_error", tool: "read_capture_artifact", code: "ARTIFACT_NOT_FOUND", message: "Artifact is not available."}` with no additional member or metadata; an authorized hash/identity/length mismatch produces `ARTIFACT_INTEGRITY_FAILED` with zero released bytes;
- connection framing/correlation mismatch, disconnected/oversized bridge response, cancellation, and artifact commit failure become typed failures and clean up boundedly; diagnostic text is absent from MCP stdout.

### M1 — settle/revalidate, element facts, and atomicity qualification

```powershell
# NOT EXECUTED — run only after M0 acceptance and M1 implementation exists.
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj -c Debug --filter "FullyQualifiedName~NativeSceneStabilityTests|FullyQualifiedName~NativeSceneAtomicityTests" -v minimal
```

Expected evidence:

- a successful `wait_for_ui_stable` result is recorded as historical evidence with `revalidatedByCapture: false`;
- every `capture_element_snapshot` and `capture_native_scene` branch that returns or commits capture evidence, including `PARTIAL` and `UNOBSERVABLE`, has performed its own settle/revalidation and records `revalidatedByCapture: true`; a changed layout/state after a prior wait cannot reuse the old receipt as authorization;
- a unique element capture has canonical identity separate from accessibility/AutomationId and includes only observed supported facts; missing and ambiguous selectors return their typed outcomes;
- the opt-in WPF fixture proves one dispatcher-affine non-yielding transaction: revision-before, full bounded immutable DTO materialization, revision-after, then equal valid revisions for an atomic `COMPLETE` result that has at least one descriptor with `mediaType: application/vnd.netcoredbg.native-scene+json` and `evidenceGrade: observed_facts`; `PARTIAL`/`UNOBSERVABLE` may have zero artifacts only when evidence could not be committed;
- a changed revision or incomplete probe evidence becomes `PARTIAL` or `UNOBSERVABLE`, never `COMPLETE`, while retaining capture-time revalidation when it returns or commits evidence;
- a UIA/FlaUI multi-element traversal with matching guards returns `PARTIAL` plus `ATOMICITY_UNPROVEN_UIA_GUARDED`; a changed or unusable guard returns `UNOBSERVABLE`; 0 guarded traversals are reported as atomic `COMPLETE`;
- the committed scene graph is bounded to 4,096 nodes and 16,777,216 bytes, has unique node IDs, valid internal parent links, and no parent cycle; custom facts remain opaque under their versioned authority namespace;
- no M1 response contains DTCG resolution, a token-to-property mapping, `PASS`/`FAIL`, a root cause, or repair advice. A raster remains separately timed corroboration.

### Full approved M0/M1 focused acceptance set

```powershell
# NOT EXECUTED — run only after all three milestone groups above are implemented.
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj -c Debug --filter "FullyQualifiedName~NativeSceneSchemaParityTests|FullyQualifiedName~NativeSceneNegativeWireTests|FullyQualifiedName~NativeSceneCapabilityTests|FullyQualifiedName~NativeSceneArtifactStoreTests|FullyQualifiedName~NativeSceneBridgeLifecycleTests|FullyQualifiedName~NativeSceneVisualEvidenceTests|FullyQualifiedName~NativeSceneStabilityTests|FullyQualifiedName~NativeSceneAtomicityTests" -v minimal
```

This is the planned focused acceptance set. It is not a full-suite, packaging, release, or customer-publication command.

T032 runs this full set only after M0/M1 implementation. It behaviorally executes every C001–C024 and records the full case-to-result mapping; it is the first gate that may claim observer, artifact, stability, or atomicity behavior.

## 3. Future scenario playbook

### Scenario A — M0-G0 contract validators and corpus integrity

1. Confirm the Approval Record byte hashes match the two schema files and parity corpus.
2. Run the M0-G0 command above.
3. Validate every C001–C024 record for corpus syntax, internal references, allowed expected-classification vocabulary, and explicit `contractGateExpectation`/`runtimeBehaviorRequired` metadata; run only the concrete fixtures marked for the contract gate through the request/result validators.
4. Inspect validator results and corpus-integrity output, not source text or a live native route.

| Case class | M0-G0 expected result |
|---|---|
| C001 capability-shape fixture | The validator accepts the six names with their fixed M0/M1 pairings, supported-version/settle-limit fields, and no artifact ID; it does not dispatch a capability call. |
| C002 malformed version | `INVALID_TOOL_ARGUMENTS` from the version validator. |
| C003 well-formed unsupported version | `UNSUPPORTED_PROTOCOL`, distinct from malformed syntax. |
| C004 unavailable capability envelope | The result validator accepts the prescribed `UNSUPPORTED_CAPABILITY` envelope; it does not invoke an unavailable primitive. |
| C017–C019 and C024 structural bounds | Contract validators reject the invalid request/result shapes and values. |
| C021–C022 capability declaration | The validator rejects omitted, duplicate, or cross-milestone primitive pairs despite the six-entry shape. |
| Runtime-behavior records | C005–C016 and C020, together with any other record marked `runtimeBehaviorRequired`, are recorded as corpus-valid but are not executed in Scenario A. T032/T034 execute them through the controlled native route. |

### Scenario B — M0 lossless artifact round trip

1. Start the controlled local debuggee through the existing native test driver and obtain its explicit `debugSessionId`.
2. Call `get_ui_probe_capabilities`; proceed only if lossless visual support is declared.
3. Call `capture_visual_evidence` with the corpus's fully explicit `sceneRequest` and window evidence scope.
4. Assert a compact manifest whose `COMPLETE` result contains at least one `image/png` descriptor with `lossless_visual` evidence grade, then retain only its `artifactId`; do not inspect a filesystem location.
5. Read the artifact in 65,536-byte or smaller chunks with `read_capture_artifact`, concatenate decoded raw bytes, and compare the exact byte count and SHA-256 with the descriptor.
6. Repeat with an end offset, a foreign ID, an expired ID produced by the fake clock/session stop, and a deliberately tampered authorized artifact.

Expected evidence: C005–C010 plus the capability-declaration negatives C021–C022 pass; raw evidence never appears in the capture envelope; the fixed unavailable envelope discloses no artifact property; integrity containment emits no bytes.

### Scenario C — M1 atomic WPF record

1. Start the planned WPF fixture with the probe explicitly enabled for the fixture only and bind it to the native session.
2. Submit the fully explicit `sceneRequest` with a `sampleCount` between 2 and 16.
3. Call `capture_native_scene` and retrieve its scene artifact by bounded reads.
4. Verify `atomicity.authority` is `in_process_framework_probe`, the two declared revisions are equal and valid, the result is `COMPLETE` with at least one `application/vnd.netcoredbg.native-scene+json` descriptor with `observed_facts` evidence grade, and `issues` is empty.
5. Run the fixture's changed-revision variation during materialization.

Expected evidence: C011–C012 and C020 pass; C020 contains oversized observer output before DTO materialization/artifact commit, and no comparison/DTCG conclusion appears in either manifest or artifact.

### Scenario D — M1 guarded UIA uncertainty

1. Run the same scene with the in-process WPF probe unavailable and only the existing C# UIA/FlaUI bridge declared.
2. Capture a guarded scene while matching window/client/DPI/fingerprint guards are available.
3. Repeat while the fixture changes UI during traversal or makes a required guard unavailable.
4. Call `wait_for_ui_stable`, mutate required layout/state, then call `capture_native_scene`.

Expected evidence: C013 is `PARTIAL` with `ATOMICITY_UNPROVEN_UIA_GUARDED`; changed/unusable guards are `UNOBSERVABLE`; C014 proves no stale receipt authorization; C023 proves a qualified capture still has `revalidatedByCapture: true`; C024 proves selected/current-state byte bounds. A scene epoch/sequence from this path names only the host operation and does not claim atomicity.

### Scenario E — external fact consumption boundary

1. Give an external consumer only a compact manifest, a valid `debugSessionId`, and an opaque `artifactId` during retention.
2. Have it call `read_capture_artifact` in bounded chunks and reconstruct the artifact locally.
3. Confirm it never receives a server path/root, debug-target discovery capability, design verdict, root cause, or repair plan.
4. Confirm the native host does not require the Factory to be installed or available to create M0/M1 observed evidence.

Expected evidence: C015–C016 preserve preview independence/non-authority and return only an observation result with typed uncertainty; the Design Contract Factory remains external and is the only future owner of comparison.

## 4. Evidence record required after future execution

A future approved acceptance receipt must contain the following concrete facts, not a claim that tests were merely invoked:

| Gate | Evidence to record |
|---|---|
| Approval | Exact file paths, byte hashes, operator decision, and approval time. |
| M0-G0 contract gate | Command, pass/fail count, exact-byte receipt, request/result validator agreement on concrete contract-gate fixtures, corpus syntax/internal-reference and metadata validation for C001–C024, fixed primitive-name/milestone pairing, and negative structural/version classifications. It records no live observer/artifact/stability/atomicity claim. |
| T032/T034 behavioral mapping | The full C001–C024 case-to-result mapping from controlled M0/M1 execution, including C005–C016 and C020 runtime behavior. |
| Session/locality | Explicit session handle, positive local candidate identity, and proof that no process scan/Python route was used. |
| Artifact creation | Descriptor fields, including the required `image/png`/`lossless_visual` descriptor for every `COMPLETE` visual capture; staged-to-committed result, independent raster identity/time, and size/hash proof. |
| Artifact reads | Requested offsets/max bytes, decoded bytes/terminal state, reconstructed hash/length, unavailable non-disclosure, and authorized integrity containment. |
| Lifecycle | One-in-flight/correlation and cancellation cleanup results; fake-time four-hour expiry and session-stop expiry. |
| Atomicity | Probe authority, before/after revisions, immutable DTO fixture result, the required `application/vnd.netcoredbg.native-scene+json`/`observed_facts` descriptor for every `COMPLETE` native scene, UIA guarded result, and stale-wait revalidation result proving `revalidatedByCapture: true` for every evidence-returning/committing element or scene capture branch. |
| Boundary review | Explicit confirmation of no Python change/dependency, no Factory/Gallery/comparator, no path/root retrieval, no `check_element_tokens`, no M2-M5, and no public route cutover/release. |

## 5. Stop conditions and exclusions

Stop the future run and classify the failing acceptance criterion if any of these occur: missing approval record; M0 work starts before GREEN T007; mismatch between approved bytes and runtime-embedded/copied bytes; a capability declaration omits, duplicates, or cross-pairs any of the six fixed name/milestone entries; any observer process not positively bound to the explicit local session; a `COMPLETE` visual capture without an `image/png`/`lossless_visual` descriptor; a `COMPLETE` native scene without an `application/vnd.netcoredbg.native-scene+json`/`observed_facts` descriptor; a path/root in an MCP response; an unavailable-artifact envelope that differs from `{kind: "tool_error", tool: "read_capture_artifact", code: "ARTIFACT_NOT_FOUND", message: "Artifact is not available."}`; artifact bytes after an integrity mismatch; a UIA `COMPLETE` atomic scene; any evidence-returning/committing element or scene capture with `revalidatedByCapture: false`; a Python invocation/change; or a design verdict in the host.

Do not expand this playbook into M2 WPF effective presentation facts, M3 binding/resource provenance, M4 complex-control coverage, M5 Avalonia, Factory implementation, release/packaging, or a public-route migration. Each is a separate future decision and approval boundary.
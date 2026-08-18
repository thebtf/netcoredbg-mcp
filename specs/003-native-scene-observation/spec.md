---
feature_id: 003
slug: native-scene-observation
title: "Native scene observation evidence boundary"
status: PLANNED
created: 2026-08-18
baseline: origin/main@7d245c2
design_rung: D2
release_intent: none
---

# PRD: Native scene observation

## Problem

The repository can control a debuggee and gather UIA/FlaUI and raster evidence, but it has no canonical, attributable native-scene capture or artifact-retrieval boundary. ADR-002 proposed treating a narrow geometry observer and an in-host DTCG resolver as a `check_element_tokens` conformance feature. That would conflate token resolution, design identity and contract mapping, live framework observation, and comparison/diagnosis.

UIA geometry and screenshots are useful facts, but neither establishes effective framework properties, resource/template/value-source provenance, DTCG token identity, a conformance verdict, or a root cause. Nor can a UIA/FlaUI multi-element traversal prove an atomic graph: its reads are separately timed. The Design Contract Factory is the required external consumer for DTCG Format/Resolver semantics and scene comparison; it is not an existing repository component. There is also no Gallery implementation in this repository.

The JSON examples below are **normative semantic sketches**, not frozen wire schemas. The exact JSON Schema artifact, runtime/schema parity tests, and negative wire tests are the first mandatory M0 design gate. No schema file is created by this docs amendment, and no M0 primitive may be implemented before that gate is accepted.

## Product outcome

`netcoredbg-mcp` gains an additive, read-only native-scene observation contract. For an explicit debug session and fully explicit scene context, it negotiates capability, stabilizes or revalidates, captures observed evidence under an explicit atomicity authority, writes bounded immutable artifacts to a server-owned per-session root, and returns a small manifest with attributable provenance and typed uncertainty.

The contract intentionally returns **observation completeness**, never `PASS`, `FAIL`, a token mapping, a root cause, or a repair plan. A future Design Contract Factory may retrieve manifest-referenced artifacts only through `read_capture_artifact`, using opaque session/capture-bound artifact capability IDs. `check_element_tokens` is deferred as an optional facade only after the shared comparator exists.

## Scope and non-goals

In scope:

- explicit local debug-session authority and native probe capability negotiation;
- bounded local IPC observer/probe lifecycle, immutable artifact transport, and bounded artifact reads;
- lossless visual evidence and provenance in M0;
- explicitly qualified scene geometry/relations/stability facts in M1;
- an opt-in in-process framework transaction as the sole authority for `COMPLETE` atomic scenes; and
- typed partial, unavailable, and integrity outcomes rather than invented evidence.

Out of scope:

- a private DTCG Format/Resolver implementation, generic token resolver, comparator, conformance verdict, root-cause engine, or repair planner;
- a Design Contract Factory or Gallery implementation in this repository;
- an implementation of `check_element_tokens`;
- remote listeners, filesystem token import, arbitrary URI dereference, caller-selected artifact paths, artifact-root disclosure, caller-directed process scans, or public Python cutover;
- screenshot-driven conformance or a raster silently labeled as the same epoch as a scene; and
- M2+ framework presentation or binding facts before their named milestones.

## Roles and authority

| Role | Authority | Boundary |
|---|---|---|
| Native host | Owns `debugSessionId`, local debuggee identity, capability negotiation, observer/probe lifecycle, stabilization/capture orchestration, server-owned per-session artifacts, opaque artifact capabilities, bounded reads, and typed outcomes. | Does not resolve DTCG, infer a token mapping, compare contracts, diagnose, repair, or expose a filesystem/root retrieval surface. |
| UIA/FlaUI fallback | Supplies process/window identity, physical geometry, accessibility, interaction, raster, and guarded best-effort traversal evidence. | Cannot assert computed-style, resource, template, token, verdict, or `COMPLETE` atomic-scene authority. |
| Opt-in WPF/Avalonia probe | Supplies framework-specific facts through local test/Gallery-only IPC. An in-process probe may own the atomic snapshot transaction. | Must be explicitly capability-negotiated; cannot open a production listener, weaken native session locality, or make DTCG verdicts. |
| Design Contract Factory (external required integration) | Owns DTCG Format/Resolver, component/layout contracts, statecharts, comparison, root-cause minimization, repair planning, and client use of opaque artifact capabilities. | Is not implemented, launched, or assumed available by this repository; cannot access server roots or use local provenance paths as retrieval authority. |

## Contract model

### Versioned transport, schema gate, and binding

All primitives use the `native-scene-probe/1` protocol and a versioned scene-artifact contract. Before any capture or artifact read, the host positively binds the request to the opaque `debugSessionId`, an explicitly local debuggee PID, and process identity. A server-issued nonce/capability authorization binds a local bridge or opt-in probe to that same session and candidate. The host rejects an absent, remote, stale, mismatched, or unverified binding rather than discovering a process by scanning.

One synchronous bridge connection permits one in-flight request. Each request has a correlation ID. Negotiated limits bound connect, write, read, complete response bytes, graph nodes, artifact bytes, raw artifact chunk bytes, total settle time, and artifact retention. Cancellation stops the operation, tears down server-owned bridge/probe work within its cleanup bound, and is idempotent. A malformed payload, correlation mismatch, timeout, disconnect, or oversized response is a typed failure, never a successful partial fact.

Before any M0 primitive implementation, the first mandatory design gate must produce one exact frozen JSON Schema artifact and prove:

1. runtime validation and that artifact agree for each request and result root, required field, closed-object rule, type, bound, and error envelope;
2. negative wire tests reject malformed protocol/schema-version syntax as `INVALID_TOOL_ARGUMENTS`; and
3. negative wire tests classify syntactically valid, known protocol/schema versions that this negotiated server does not support as `UNSUPPORTED_PROTOCOL`.

This is a future implementation deliverable, not an assertion that Spec003 itself has frozen schemas. The sketches in this document establish semantics that the artifact must encode; they do not substitute for it.

The host stays `net8.0` cross-platform. Windows bridge and WPF/Avalonia adapters are optional, separately launched or opt-in components. No primitive starts a network listener.

### Scene request context

Every stability or capture request carries a `sceneRequest` object. Its future schema root is closed; each listed field is required and is either a constrained value or literal `null`, which means the caller expressly does not constrain that aspect. `null` never permits the observer to invent a default that changes scene meaning.

| Field | Required semantic | Capability behavior |
|---|---|---|
| `storyId`, `sceneId`, `fixtureId` | Stable strings identifying the requested story, scene, and fixture. | A capability either supports each identifier as evidence/provenance or declares it unobservable. |
| `scope` | Bounded capture scope (`scene`, a declared slot, or one canonical element identity). | Unsupported scope is rejected before capture. |
| `appearance`, `theme`, `density`, `contrast` | Explicit display-appearance constraints. | The adapter reports each as applied, unsupported, or unobservable. |
| `viewport` | Requested logical width/height and viewport policy. | A capture records actual client/window geometry and whether the policy was met. |
| `expectedDpiPolicy` | Exact, range, or unconstrained DPI policy. | Missing DPI evidence is `PARTIAL`/`UNOBSERVABLE`, not guessed DPI. |
| `focusTarget` | Canonical target identity or explicit `null`. | A capability declares whether it can materialize and observe focus. |
| `selectedState`, `currentState` | Explicit state payloads or `null`. | Unsupported state controls are reported, not silently ignored. |
| `scrollOffsets` | Explicit per-container offsets or `null`. | An adapter declares whether it can materialize and observe each offset. |
| `animationPolicy` | Required disabled, finished, or observed-animation state. | Lack of authority to prove it prevents `COMPLETE` for an atomic scene. |
| `settlePolicy` | Bounded timeout, sample count, stable duration, and required observation conditions. | The host refuses values above negotiated maxima. |
| `contractSetHash` | Consumer-provided hash identifying the external contract set. | It is provenance only; the host does not parse or compare the contract. |
| `expectedCandidateIdentity` | Expected executable/binary/probe identity or explicit `null`. | A mismatch is `CANDIDATE_MISMATCH`; unavailable identity prevents a claimed match. |

Capabilities enumerate supported values, maximums, and `supported`, `unsupported`, or `unobservable` status for every context field. A required field marked unsupported produces `UNSUPPORTED_CAPABILITY`; a field that is supported but not observable during the requested capture produces `PARTIAL` or `UNOBSERVABLE` as applicable.

### Canonical identity and snapshot authority

Canonical design identity is:

```json
{
  "contractId": "Button.Primary",
  "instanceKey": "save-1",
  "templatePart": "ContentHost",
  "storyScope": "Button/Primary/Default",
  "componentSlot": "content"
}
```

`contractId` is required when the probe can provide canonical identity. `instanceKey` and `templatePart` are optional. `automationId` is captured separately as fallback/accessibility identity; it is never promoted into a design identity by inference.

An M1 `capture_native_scene` can report `COMPLETE` only when an opt-in in-process framework probe advertises the required snapshot authority and performs all of the following in **one dispatcher-affine, non-yielding transaction**:

1. read its probe-owned monotonic layout/state revision immediately before materializing the graph;
2. materialize the bounded graph and all included facts into an immutable DTO while no transaction step awaits, yields, or schedules an intervening dispatcher callback;
3. read that same revision immediately after materialization; and
4. bind the two values and probe authority to the capture artifact.

`COMPLETE` requires valid equal before/after revision values and complete required evidence. If the revisions differ, the probe is unavailable, a dispatcher-affine non-yielding transaction cannot be completed, or required evidence is absent, the scene is `PARTIAL` or `UNOBSERVABLE` with a typed issue; it must not claim atomicity. Writing the already immutable DTO to the artifact may occur after the transaction, but no later observation may alter it.

A UIA/FlaUI-only multi-element traversal is `uia_guarded` best effort. It may record before/after window, client, DPI, and visual-tree fingerprint guards. Even when those guards match, an atomic-scene request returns `PARTIAL` with `ATOMICITY_UNPROVEN_UIA_GUARDED`; if traversal or guard evidence is unusable or changes during traversal, it returns `UNOBSERVABLE`. It never returns `COMPLETE` for an atomic scene. A one-element non-atomic `capture_element_snapshot` may report its own completeness but must not imply a complete scene epoch.

A committed scene has `sceneEpoch` and ordered capture `sequence`. For a `COMPLETE` atomic scene, their authority is the probe transaction and its bound revision pair; for `uia_guarded`, they identify only the host capture operation and explicitly do not confer atomicity. Its scene artifact may include:

- a bounded element graph with parent/child and slot relations;
- canonical identity where available, accessibility identity, and adapter namespace;
- logical and physical `x`, `y`, `width`, and `height`;
- DPI, transforms, clip, window/client geometry, visibility, and accessibility facts;
- focus, selection, current state, scroll, animation, and async-load observations;
- adapter-reported typed effective values/value-source/resource/template/text facts when that named adapter has authority; and
- opaque typed semantic regions from versioned custom adapter namespaces (for example, a waveform overview).

Unknown adapter namespaces are preserved without interpretation when within negotiated size limits, or reported unsupported. The host never translates these facts into token usage, conformance, or diagnosis.

### Stabilization and capture-time revalidation

`wait_for_ui_stable` uses the supplied settle policy to produce a **stability receipt**. Where declared by a capability, the receipt records dispatcher idle, stable layout samples, `stableForMs`, disabled-or-finished animation policy, stable window/client geometry, materialized fixture/theme/focus/selection/scroll state, pending async-load status, timeout, epoch, and sequence.

A later capture does not accept the receipt as authority. `capture_element_snapshot` and `capture_native_scene` must either perform their own settle cycle or revalidate all required conditions immediately before reading evidence. They set `stability.revalidatedByCapture: true` only when that revalidation succeeded. Changed epoch/sequence, changed probe revision, missing required evidence, or timeout yields the defined partial/unobservable/unstable result; it never reuses a stale wait success.

### Public primitive semantic sketches

These examples are semantic sketches pending the M0 frozen-schema gate. They do not claim `additionalProperties: false`, exact JSON type validation, or a frozen result schema.

```json
{
  "get_ui_probe_capabilities": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1"
  },
  "read_capture_artifact": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1",
    "artifactId": "opaque session-and-capture-bound artifact capability",
    "offset": 0,
    "maxBytes": 65536
  },
  "wait_for_ui_stable": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1",
    "sceneRequest": { "all required context fields": "value or null" }
  },
  "capture_element_snapshot": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1",
    "sceneRequest": { "all required context fields": "value or null" },
    "element": { "contractId": "required when supported", "instanceKey": null, "templatePart": null, "automationId": null }
  },
  "capture_native_scene": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1",
    "sceneRequest": { "all required context fields": "value or null" }
  },
  "capture_visual_evidence": {
    "debugSessionId": "opaque process-local capability",
    "protocolVersion": "native-scene-probe/1",
    "sceneRequest": { "all required context fields": "value or null" },
    "evidenceScope": "window-or-bounded-element"
  }
}
```

`get_ui_probe_capabilities`, `read_capture_artifact`, and `capture_visual_evidence` are M0. `wait_for_ui_stable`, `capture_element_snapshot`, and `capture_native_scene` are M1. Before their milestone, a recognized primitive is reported as unsupported; no undeclared compatibility alias exists.

#### Artifact capability and bounded-read semantics

An artifact is immutable after its staged file has atomically committed beneath the server-owned per-session artifact root. Public manifests reference it only by its opaque unguessable `artifactId`, bound by the server to its creating session and capture. A relative provenance string may accompany the manifest for local diagnostics, but it cannot be submitted to a primitive, dereferenced, or used as retrieval authority. The server never returns the absolute root.

`read_capture_artifact` requires an integer `offset` with `0 <= offset <= byteLength` and a positive integer `maxBytes` no greater than `artifactReadMaxBytes` negotiated by capabilities. It returns no more than `maxBytes` raw bytes, encoded as padded standard base64:

```json
{
  "kind": "capture_artifact_chunk",
  "artifactId": "opaque echoed capability",
  "offset": 0,
  "bytesRead": 65536,
  "dataBase64": "base64 of exactly bytesRead raw bytes",
  "endOfArtifact": false,
  "mediaType": "application/vnd.netcoredbg.native-scene+json",
  "byteLength": 123456,
  "sha256": "lowercase-hex manifest hash",
  "schemaVersion": "native-scene-artifact/1"
}
```

A zero-byte artifact or an `offset` equal to `byteLength` returns `bytesRead: 0`, `dataBase64: ""`, and `endOfArtifact: true`. The future frozen schema fixes all field names, formats, and result/error envelopes.

On the first successful authorized read, the server verifies the full committed file SHA-256 against the manifest and records immutable file identity plus length. On every subsequent read it verifies that recorded identity and length before emitting the requested chunk. Any hash, identity, or length mismatch returns `ARTIFACT_INTEGRITY_FAILED` and emits no bytes. A valid debug session that requests an unknown, foreign-session, foreign-capture, expired, deleted, or otherwise unavailable `artifactId` gets the same `ARTIFACT_NOT_FOUND` result; that result discloses no existence, ownership, retention, size, hash, provenance, or path. Invalid syntax/bounds are `INVALID_TOOL_ARGUMENTS` before lookup.

### Observation manifest semantic sketch

Successful and partial captures return a compact manifest, not scene JSON or raster bytes. The future schema must define its exact closed root. Semantically, it contains `kind`, `status`, `captureId`, protocol/schema versions, stability, candidate, atomicity, artifact descriptors, and issues:

```json
{
  "kind": "native_scene_capture",
  "status": "COMPLETE",
  "captureId": "opaque capture capability",
  "protocolVersion": "native-scene-probe/1",
  "schemaVersion": "native-scene-artifact/1",
  "stability": {
    "status": "STABLE",
    "revalidatedByCapture": true,
    "sceneEpoch": 42,
    "sequence": 9
  },
  "atomicity": {
    "authority": "in_process_framework_probe",
    "layoutStateRevisionBefore": 88,
    "layoutStateRevisionAfter": 88
  },
  "candidate": {
    "processId": 1234,
    "hwnd": "0x0000000000012345",
    "executableSha256": "lowercase-hex",
    "assemblyVersion": "8.0.0",
    "probeVersion": "1.0.0",
    "observerVersions": [{ "name": "wpf-in-process-probe", "version": "x.y.z" }],
    "contractSetHash": "lowercase-hex",
    "storyHash": "lowercase-hex",
    "capturedAt": "2026-08-18T00:00:00Z",
    "source": { "kind": "launch_manifest", "verification": "verified" }
  },
  "artifacts": [
    {
      "artifactId": "opaque session-and-capture-bound capability",
      "relativeProvenance": "captures/opaque/scene.json",
      "mediaType": "application/vnd.netcoredbg.native-scene+json",
      "byteLength": 12345,
      "sha256": "lowercase-hex",
      "schemaVersion": "native-scene-artifact/1",
      "captureId": "opaque capture capability",
      "retention": "session-until-stop-or-4h-gc",
      "evidenceGrade": "observed_facts"
    }
  ],
  "issues": []
}
```

`relativeProvenance`, if retained, is informative local provenance only; `artifactId` is the sole public retrieval authority and is valid only through `read_capture_artifact` with its owning `debugSessionId`. Scene JSON and raw PNG use different IDs. Raster descriptors include their own `capturedAt` and `rasterCaptureId`, plus an optional `adjacentToCaptureId`; they do not claim the scene's epoch. An optional preview has `evidenceGrade: "preview_only"`; previews cannot be authority for observation completeness or external comparison.

`status` is exactly `COMPLETE`, `PARTIAL`, or `UNOBSERVABLE`; it is never a conformance status. A `COMPLETE` atomic-scene manifest has `atomicity.authority: "in_process_framework_probe"`, equal probe-owned revisions, and no issues. A UIA/FlaUI-only scene manifest has `atomicity.authority: "uia_guarded"` and cannot be `COMPLETE`; matching guards produce `PARTIAL` with `ATOMICITY_UNPROVEN_UIA_GUARDED`, and failed/changed/unavailable guard traversal is `UNOBSERVABLE`. `issues` contains typed missing, unsupported, or atomicity evidence; it is empty for `COMPLETE`.

The host stages each artifact and atomically commits it beneath its server-owned per-session root before returning its descriptor. Repository, worktree, branch, HEAD, and tree facts are optional candidate metadata only when an explicit launch/probe manifest supplies them; each field then names source and verification status. The host must not infer source-control metadata from the current directory or process scan.

### Typed outcomes

| Condition | Result |
|---|---|
| Invalid closed request, malformed protocol/schema version, type, bound, or limit | `INVALID_TOOL_ARGUMENTS` |
| Missing, invalid, stale, remote, or unavailable debug session | `DEBUG_SESSION_NOT_FOUND` |
| Syntactically valid known protocol/schema version not supported by the negotiated server | `UNSUPPORTED_PROTOCOL` |
| Bridge/probe launch, transport, framing, lifecycle, or response failure | `OBSERVER_UNAVAILABLE` |
| Required context field/capability is unsupported | `UNSUPPORTED_CAPABILITY` |
| Named canonical scene/element absent or non-unique | `SCENE_NOT_FOUND`, `SCENE_AMBIGUOUS`, `ELEMENT_NOT_FOUND`, or `ELEMENT_AMBIGUOUS` |
| Required settle condition does not complete within its bound | `UI_NOT_STABLE` |
| Staged artifact cannot be atomically committed | `ARTIFACT_WRITE_FAILED` |
| Unknown, foreign, expired, deleted, or unavailable artifact capability after a valid session binding | `ARTIFACT_NOT_FOUND` |
| Manifest hash verification, immutable identity, or length check fails for an authorized artifact | `ARTIFACT_INTEGRITY_FAILED` |
| Positive runtime identity differs from `expectedCandidateIdentity` | `CANDIDATE_MISMATCH` |

A valid request with incomplete evidence returns the non-error manifest status `PARTIAL` or `UNOBSERVABLE` with typed issues. A typed error does not return invented facts or artifact bytes. Screenshot evidence remains corroborative in all outcomes.

## Functional requirements

- **FR-001 — Authority:** Every primitive requires an explicit opaque `debugSessionId`; native code binds observer/probe work and artifact capabilities only to an explicitly local, positively identified debuggee process. No connection-global or process-scan target exists.
- **FR-002 — Capability contract:** `get_ui_probe_capabilities` declares supported protocol/schema versions, primitive availability, context-field support, observer/probe namespaces, limits including `artifactReadMaxBytes`, and candidate identity before a consumer depends on capture behavior.
- **FR-003 — Frozen-wire gate:** Before any M0 primitive implementation, one exact JSON Schema artifact, runtime/schema parity tests, and negative wire tests are accepted. Spec003 sketches remain semantic until that gate closes; no implementation may claim schemas are already frozen.
- **FR-004 — Closed inputs and version classification:** The frozen schemas make primitive request roots and `sceneRequest` closed. Malformed protocol/schema-version syntax is `INVALID_TOOL_ARGUMENTS`; syntactically valid known-but-unsupported protocol/schema input is `UNSUPPORTED_PROTOCOL`.
- **FR-005 — Transport safety:** Native IPC is session-scoped, local-only, nonce/capability-authorized, correlated, single-in-flight per synchronous bridge, bounded, cancellation-aware, and idempotently cleaned up.
- **FR-006 — No wait TOCTOU:** A standalone stability receipt is evidence only. Element and scene capture independently stabilize or immediately revalidate required conditions before committing evidence.
- **FR-007 — Atomic facts:** M1 `capture_native_scene` returns `COMPLETE` only from an opt-in in-process framework probe's dispatcher-affine non-yielding transaction, immutable DTO, and equal probe-owned monotonic layout/state revisions before and after materialization. UIA/FlaUI traversal is `uia_guarded` and cannot return `COMPLETE` for an atomic scene.
- **FR-008 — Identity:** Canonical identity uses `contractId` with optional `instanceKey`, `templatePart`, story scope, and component slot. AutomationId remains fallback/accessibility evidence.
- **FR-009 — Honest observation:** UIA/FlaUI records only supported physical/accessibility/raster and guarded traversal evidence. Framework probes attach their own namespace and authority. Neither becomes token provenance or a conformance verdict.
- **FR-010 — Stabilization:** Settle policy is bounded and reports dispatcher/layout/animation/window/materialization/async-load evidence or its absence. Unsupported conditions are typed partial/unobservable, never inferred stable.
- **FR-011 — Artifact safety and retrieval:** Artifacts are staged then atomically committed under a server-owned per-session root, become immutable, and are described by opaque session/capture-bound `artifactId` capabilities. `read_capture_artifact` reads bounded base64 chunks by offset and negotiated `maxBytes`; paths and roots are never retrieval authority or response data.
- **FR-012 — Artifact authorization and integrity:** Foreign, expired, deleted, and unknown artifact IDs return indistinguishable `ARTIFACT_NOT_FOUND` to an otherwise valid session. First authorized read verifies manifest SHA-256; later reads verify immutable file identity and length. An authorized mismatch returns `ARTIFACT_INTEGRITY_FAILED` without bytes.
- **FR-013 — Provenance:** Manifest and artifact provenance include capture ID, timestamp, epoch, sequence, atomicity authority, PID, HWND when available, binary hash, assembly/probe/observer versions, and contract-set/story hashes. Raster carries independent capture timing. Repository provenance requires an explicit verified launch/probe manifest.
- **FR-014 — Outcome separation:** Capture uses only `COMPLETE`, `PARTIAL`, `UNOBSERVABLE`, and the typed errors above. No capture result contains a DTCG resolver result, `PASS`/`FAIL`, root cause, or repair recommendation.
- **FR-015 — Deferred facade:** `check_element_tokens` is not registered or implemented by M0/M1. A later facade must consume the shared canonical artifact and Factory comparator rather than add a private resolver/comparator.
- **FR-016 — Compatibility:** The retained Python public route and existing native three-tool catalog remain unchanged by M0/M1 except for additive registration of the new primitive catalog at its own milestone.
- **FR-017 — External integration:** Factory and framework-adapter interfaces are required integration contracts, not repository implementations. M1 `COMPLETE` atomic-scene work depends on a framework snapshot authority satisfying FR-007; each implementation slice re-verifies current official framework and DTCG sources before code.

## Non-functional requirements

- **NFR-001:** Captures are read-only and do not mutate debuggee application state beyond explicitly requested test/Gallery-only probe materialization.
- **NFR-002:** Native and bridge diagnostics remain off MCP stdout.
- **NFR-003:** No remote endpoint, arbitrary URI, token import, caller-selected artifact destination, root disclosure, or path-based artifact read is introduced.
- **NFR-004:** Existing `start_debug`, `get_debug_state`, and `stop_debug` contracts remain unchanged.
- **NFR-005:** Artifact and observer cleanup is bounded, idempotent, and owned by the native session lifecycle.
- **NFR-006:** Result certainty never exceeds the producing observer/probe's evidence authority; UIA guarded traversal and raster never prove an atomic scene.
- **NFR-007:** Scene graph, response, artifact, and artifact-chunk limits prevent unbounded allocation, traversal, hashing, or MCP transport.
- **NFR-008:** Artifact lookup is non-enumerable: foreign/expired/unavailable capabilities reveal no artifact metadata or existence.

## Milestone map and executable slices

| Milestone | Release-closing statement | Scope | Binding constraints |
|---|---|---|---|
| M0-G0 — Frozen wire contract | The semantic sketches become one exact implementation-ready wire contract; no primitive code has started. | JSON Schema artifact, runtime/schema parity suite, and negative wire tests. | Mandatory first M0 design gate; no schema file is created by this docs amendment. |
| M0 — Observer transport and artifact reads | Native MCP had no negotiated, attributable path for lossless UI evidence or Factory-accessible server artifacts; it can advertise observation capabilities, return bounded visual-evidence manifests, and read owned artifact chunks. | Capabilities, positive session/candidate binding, bounded local lifecycle, staged immutable artifacts, lossless PNG, opaque artifact IDs, chunk reads, provenance, and typed outcomes. | Depends on M0-G0; no scene comparison; no Factory implementation; no Python changes; native host remains cross-platform. |
| M1 — Atomic geometry scene | Native evidence could not represent a stable, attributable scene with qualified atomicity; it can now capture bounded graph/relations/geometry/DPI/clip facts and distinguish in-process atomic authority from UIA fallback. | Wait receipt, revalidation, canonical identity, `uia_guarded` fallback facts, scene JSON artifact, element/scene primitives, in-process probe transaction and revision binding. | Depends on M0 and a framework snapshot authority for any `COMPLETE` atomic scene; UIA/FlaUI must return `PARTIAL`/`UNOBSERVABLE` for atomic-scene claims; raster remains adjacent corroboration. |
| M2 — WPF computed presentation | The artifact lacked effective WPF presentation facts; a WPF adapter can add typed spacing, border, radius, color, opacity, typography, baseline, state, and template facts. | Opt-in WPF local adapter. | WPF value/source categories are not DTCG token identity. |
| M3 — Binding/resource provenance | Equal observed values could not distinguish a binding/resource source; a WPF adapter can add resource keys and DynamicResource/value-source/style/template facts. | Positive WPF adapter provenance. | Only the external Factory may turn facts into a binding-mode verdict. |
| M4 — Complex-control parity | Complex controls lacked portable scene facts; bounded adapters can describe virtualization, DataGrid, popups, menus, dialogs, and typed custom semantic regions. | Capability-negotiated complex-control/custom namespaces. | Unknown namespaces remain opaque/unsupported. |
| M5 — Avalonia adapter | Avalonia lacked the same portable envelope; an opt-in Avalonia adapter can emit equivalent fact categories. | Avalonia local adapter. | Re-verify current official Avalonia contracts; do not add WPF-specific public fields. |

### M0/M1 typed execution tickets

| Ticket | Type | Blocking edge | Acceptance checkpoint |
|---|---|---|---|
| Freeze the exact `native-scene-probe/1` and artifact JSON Schema artifact | Design gate | None | One authoritative schema defines every primitive, field, bound, closed-object rule, result, and typed error; the artifact path and compatibility policy are decided and recorded. No primitive implementation precedes it. |
| Prove runtime/schema parity and negative wire classification | Test/design gate | Frozen schema artifact | Runtime and schema accept/reject the same representative request/result corpus; negative wire tests prove malformed protocol/schema-version input is `INVALID_TOOL_ARGUMENTS` and valid known-but-unsupported input is `UNSUPPORTED_PROTOCOL`. |
| Add native capability/session/provenance front door | Code | M0-G0 | Focused native contract test and diff show positive local binding, declared capabilities/limits, and unchanged existing three-tool schemas. |
| Add bounded observer/probe lifecycle, immutable artifact storage, and artifact reader | Code | Native front door | Focused tests prove correlation, one in-flight operation, cancellation cleanup, atomic writes, opaque IDs, negotiated chunk limit, base64 offset reads, first-read SHA verification, subsequent identity/length checks, and typed write/not-found/integrity failures. |
| Prove M0 external behavior | Test | M0 code | RED-then-GREEN wire evidence covers capabilities, candidate mismatch, raw PNG manifest, preview non-authority, unavailable observer, artifact failure, offset boundaries, foreign/expired/nonexistent indistinguishable not-found, and hash/identity/length mismatch without bytes. |
| Add M1 settle/revalidation and qualified graph capture | Code | M0 lifecycle and a framework snapshot authority | Focused fixture tests prove a standalone wait cannot authorize changed UI; a probe transaction materializes an immutable DTO with equal before/after revision for `COMPLETE`; UIA-only capture is `uia_guarded`. |
| Prove M1 facts and uncertainty | Test | M1 code | RED-then-GREEN fixture evidence covers unique/ambiguous/missing identity, physical geometry, DPI/clip/relations, unsupported conditions, partial/unobservable, no verdict fields, UI mutation during a UIA multi-element traversal, and a changed probe revision. |
| Independently inspect ownership boundary | Review | M0/M1 candidate | Verdict confirms no Python route change, no private DTCG/comparison logic, no Gallery/Factory implementation claim, no screenshot verdict path, and no artifact-path/root retrieval surface. |

## Acceptance criteria

- **AC-001:** M0-G0 delivers a frozen JSON Schema artifact before primitive code and proves runtime/schema parity plus negative wire classification: malformed protocol/schema-version syntax is `INVALID_TOOL_ARGUMENTS`; syntactically valid known-but-unsupported protocol/schema input is `UNSUPPORTED_PROTOCOL`.
- **AC-002:** M0 advertises only declared `native-scene-probe/1` capabilities with explicit primitive/context support, supported schema versions, and negotiated limits including artifact read maximum; unsupported functions/features use their typed outcome.
- **AC-003:** M0 binds a local observer/probe and artifact capability to the explicit native debug session, positive process identity, and nonce/capability authorization. It rejects stale, remote, absent, and mismatched identity without process scanning.
- **AC-004:** M0 produces separate lossless PNG and optional preview descriptors using staged atomic writes under a server-owned per-session root. Every descriptor uses an opaque session/capture-bound `artifactId` and includes media type, byte length, SHA-256, schema version, capture ID, and retention policy; any relative provenance is not retrieval authority.
- **AC-005:** M0 `read_capture_artifact` returns only bounded standard-base64 chunks by zero-based offset and negotiated `maxBytes`, never full capture artifacts or a root/path. First authorized read verifies SHA-256; later reads verify immutable identity and length.
- **AC-006:** M0 proves that a valid session receives indistinguishable `ARTIFACT_NOT_FOUND` with no metadata for foreign, expired, and nonexistent artifact capabilities; it proves `ARTIFACT_INTEGRITY_FAILED` and zero emitted bytes for an authorized hash, identity, or length mismatch.
- **AC-007:** M0 proves bounded connect/write/read/response behavior, one in-flight synchronous bridge request, correlation mismatch handling, cancellation cleanup, observer-unavailable, artifact-write-failed, candidate-mismatch, and unchanged MCP stdout discipline.
- **AC-008:** M1 produces a scene JSON artifact containing one bounded graph with identity, relations, physical/logical geometry, DPI, transforms, clip, accessibility, adapter namespaces, and dependency metadata sufficient for an external comparator, with explicit atomicity authority.
- **AC-009:** M1 returns `COMPLETE` for an atomic scene only when an opt-in in-process framework probe executes a dispatcher-affine non-yielding transaction, materializes an immutable DTO, and reports equal probe-owned monotonic layout/state revisions before and after. Missing authority, differing revisions, or incomplete evidence cannot produce `COMPLETE`.
- **AC-010:** M1 treats UIA/FlaUI-only multi-element traversal as `uia_guarded`: matching before/after window, client, DPI, and/or fingerprint guards still yield `PARTIAL` with `ATOMICITY_UNPROVEN_UIA_GUARDED`; unavailable or changed traversal guards yield `UNOBSERVABLE`. Fixture evidence mutates UI during traversal and proves no atomic `COMPLETE` result. Raster has independent timing and is only adjacent corroboration.
- **AC-011:** M1 proves capture-time stabilization/revalidation. A UI change after `wait_for_ui_stable` makes a later capture re-settle/revalidate and report a new or failed epoch; it cannot reuse the old receipt as authorization.
- **AC-012:** M1 returns `COMPLETE`, `PARTIAL`, and `UNOBSERVABLE` honestly for supported, missing, unsupported, and unobservable conditions, with the specified typed errors for closed-request, session, observer, protocol, capability, selection, stability, artifact, integrity, and candidate failures.
- **AC-013:** M1 proves canonical identity remains separate from AutomationId and unknown custom adapter namespaces are preserved as typed opaque evidence or reported unsupported.
- **AC-014:** M0/M1 results contain no DTCG resolution, token-to-property mapping, `PASS`/`FAIL`, root cause, or repair plan. Screenshots are corroborative/preview-only and cannot determine observation completeness or comparison.
- **AC-015:** The diff contains no public Python route change, no Design Contract Factory or Gallery implementation, no `check_element_tokens`, and no modification to the existing three native tool contracts beyond additive primitive registration at the relevant milestone.

## Requirements-to-owner/file map

| Requirement | Milestone | Current owner/file area or required external interface |
|---|---|---|
| FR-001, FR-002, FR-004–FR-005, FR-016 | M0 | Native MCP/session owner: `host/NetCoreDbg.Mcp.Stateless/Program.cs`; DAP lifecycle: `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs` and `DapSessionState.cs`. |
| FR-003–FR-004, AC-001 | M0-G0 | New authoritative native wire-schema artifact and focused runtime/schema parity/negative wire test owners selected by the M0-G0 slice; no artifact or test file exists in this docs-only decision. |
| FR-005, FR-009, FR-010 | M0/M1 | Windows UIA/FlaUI owner: `bridge/JsonRpcHandler.cs`, `bridge/Commands/ElementCommands.cs`, and `bridge/Commands/ScreenshotCommands.cs`; any native bridge client is a new additive owner selected in the M0 slice. |
| FR-006–FR-010, FR-013 | M1 | New native observation/capture owner under `host/NetCoreDbg.Mcp.Stateless/`, selected after M0-G0; an opt-in in-process framework snapshot interface is required for `COMPLETE` atomic scenes. |
| FR-011–FR-013 | M0/M1 | New native session-artifact and `read_capture_artifact` owner under `host/NetCoreDbg.Mcp.Stateless/`; legacy reference only: `src/netcoredbg_mcp/ui/temp_manager.py::SessionTempManager`, `tools/ui.py`, and `ui/flaui_client.py`. |
| FR-014–FR-015, FR-017 | M0/M1 | MCP primitive catalog/protocol contract in the native host; Design Contract Factory and DTCG Resolver are external required interfaces, not repository files. |
| M2/M3 facts | M2/M3 | Required future local `DesignProbe.Wpf` integration contract; verify WPF property/value-source/template/resource APIs before implementation. |
| M5 facts | M5 | Required future local `DesignProbe.Avalonia` integration contract; verify current official Avalonia property/styling APIs before implementation. |
| AC-001–AC-015 | M0/M1 | Focused native host, bridge/fixture, schema-parity, negative-wire, and independent review tests selected by each implementation slice; no test files are created by this docs-only decision. |

## Rollback

M0/M1 are additive, have no data migration, and do not authorize public-route selection or publication. Rollback disables/removes the additive native primitive registrations, including `read_capture_artifact`, and server-owned observer/probe/artifact components; retains the native debug-session core and Python route unchanged; and lets existing per-session retention cleanup expire artifacts according to their manifest policy. It must not delete or reinterpret retained evidence, alter a Factory-owned contract, introduce a legacy fallback, reintroduce artifact-path/root retrieval, or replace the deferred facade with private comparison code.

## External contracts and current evidence

- [DTCG Format Module 2025.10](https://www.designtokens.org/TR/2025.10/format/), especially sections 6.2, 6.7.2, and 7.1.1, is the primary format authority for the `group.$root` fact. DTCG format/resolver handling is Factory-owned.
- [DTCG Resolver Module 2025.10](https://www.designtokens.org/TR/2025.10/resolver/) is an external required integration contract, not M0/M1 host behavior.
- [UIA `BoundingRectangle`](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.boundingrectangleproperty?view=windowsdesktop-10.0) documents physical-screen-coordinate geometry and its limits; it supports fallback facts only and cannot prove an atomic graph.
- [WPF dependency property value precedence](https://learn.microsoft.com/en-us/dotnet/desktop/wpf/properties/dependency-property-value-precedence) and [`DependencyPropertyHelper.GetValueSource`](https://learn.microsoft.com/en-us/dotnet/api/system.windows.dependencypropertyhelper.getvaluesource?view=windowsdesktop-10.0) define future WPF evidence categories, not DTCG identity. M1 implementation must independently verify the dispatcher, immutable DTO, and probe revision mechanism it chooses.
- The official Avalonia property/styling API contract must be re-verified in M5 before implementation; it is a future external integration, not an asserted current repository capability.
- Current repository evidence owners are `bridge/JsonRpcHandler.cs`, `bridge/Commands/ElementCommands.cs`, `bridge/Commands/ScreenshotCommands.cs`, `src/netcoredbg_mcp/tools/ui.py`, `src/netcoredbg_mcp/ui/flaui_client.py`, `src/netcoredbg_mcp/ui/temp_manager.py`, and the native `Program.DebugSessionRegistry`, `NetCoreDbgSession`, and `DapSessionState` seams named above.

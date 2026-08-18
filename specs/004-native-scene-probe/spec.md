# Feature Specification: Native Scene Probe

**Feature Branch**: `work/native-scene-speckit`

**Created**: 2026-08-18

**Status**: Draft

**Input**: User description: "Freeze an interoperable native-scene evidence contract, then enable attributable lossless visual evidence, honestly qualified scene evidence, and external fact consumption without debugger-owned design verdicts."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Approve One Exact Evidence Contract Before Building (Priority: P1)

A product owner needs one complete, interoperable evidence contract that every participating party can review and approve before any product implementation begins. The contract makes the evidence vocabulary, version behavior, limits, outcomes, and safety rules unambiguous, so later delivery cannot silently change what a capture means.

**Why this priority**: All later outcomes depend on a shared meaning for evidence. Building before that meaning is frozen would create incompatible evidence and undermine every later capture.

**Independent Test**: Review the contract against representative valid, invalid, and unsupported-version exchanges. Confirm that every exchange has one defined outcome and that no product implementation is authorized until the recorded operator approval is present.

**Acceptance Scenarios**:

1. **Given** the evidence contract has not received recorded operator approval, **When** delivery is proposed, **Then** product implementation remains out of scope and only contract-finalization work may proceed.
2. **Given** a candidate contract and representative exchanges, **When** stakeholders validate its required information, versions, limits, success results, partial results, and error results, **Then** each exchange has one unambiguous classification.
3. **Given** malformed version information and well-formed but unsupported version information, **When** each is submitted for classification, **Then** the former is rejected as invalid input and the latter is reported as unsupported rather than conflating the two.

---

### User Story 2 - Capture and Safely Retrieve Lossless Visual Evidence (Priority: P2)

A debugging or quality specialist needs to capture lossless visual evidence for one explicitly identified local debug session and later retrieve the exact evidence in safe, bounded portions. Each capture is attributable to the observed candidate and can be verified without exposing server storage locations or allowing one session to access another session's evidence.

**Why this priority**: Lossless, attributable visual evidence provides immediate investigation value and establishes the reusable evidence-and-retrieval boundary required before richer scene observations.

**Independent Test**: Capture visual evidence from an authorized session, retrieve it in several bounded portions, reassemble it, and verify that it matches the capture manifest. Independently attempt invalid, foreign, expired, and tampered retrievals.

**Acceptance Scenarios**:

1. **Given** an authorized local debug session with a declared visual-observation capability, **When** lossless visual evidence is captured, **Then** the response contains a compact manifest with a separately retrievable lossless artifact and attributable candidate, capture, integrity, and retention information.
2. **Given** a valid artifact capability, **When** a consumer retrieves the artifact in bounded portions from the beginning, middle, and end, **Then** the returned portions reconstruct the manifest-described original exactly and do not expose a storage location.
3. **Given** an unknown, foreign-session, foreign-capture, expired, or deleted artifact capability, **When** an otherwise authorized session requests it, **Then** the same unavailable-artifact outcome is returned without revealing whether evidence existed or any of its metadata.
4. **Given** an authorized artifact whose committed contents no longer match its recorded integrity information, **When** it is requested, **Then** an integrity-failure outcome is returned and no artifact bytes are released.

---

### User Story 3 - Capture Scene Facts With Honest Atomicity (Priority: P3)

A quality specialist needs a bounded scene record that states what was observed, how stable the scene was, and how much confidence the capture authority can honestly claim. A high-confidence atomic scene is available only from an explicitly authorized in-process observation transaction; best-effort multi-element traversal remains useful but is visibly qualified rather than presented as atomic.

**Why this priority**: Scene relations and geometry make evidence useful for systematic comparison, but overstating their timing or authority would mislead consumers and invalidate their conclusions.

**Independent Test**: Capture the same bounded scene through an authorized atomic observation path and through a guarded best-effort traversal. Verify that a changed revision or changed guard prevents an atomic result, and that a prior stability receipt cannot authorize a later changed capture.

**Acceptance Scenarios**:

1. **Given** an authorized in-process observation authority that materializes a bounded immutable scene during one non-interruptible observation transaction and reports unchanged revisions before and after it, **When** a scene capture is requested, **Then** the capture may be reported as complete with its authority and revision evidence.
2. **Given** the same observation authority reports changed revisions or lacks required evidence, **When** a scene capture is requested, **Then** the result is partial or unobservable and does not claim atomicity.
3. **Given** only independently timed best-effort traversal evidence, **When** before-and-after guards appear unchanged, **Then** the scene remains partial with an explicit unproven-atomicity qualification; if those guards are unusable or change, the result is unobservable.
4. **Given** a prior stability result and a scene that changes before capture, **When** the later capture is requested, **Then** it performs its own stabilization or immediate revalidation and never treats the prior result as authorization.

---

### User Story 4 - Consume Observation Facts Externally Without Debugger Verdicts (Priority: P4)

An external design-evaluation consumer needs to obtain the captured facts through the safe artifact capability and apply its own design-contract interpretation. The debugger remains an evidence producer: it does not resolve design tokens, map tokens to properties, issue conformance verdicts, diagnose causes, or recommend repairs.

**Why this priority**: Separating observation from interpretation prevents the evidence producer from becoming an incomplete and competing design-evaluation system while allowing external consumers to evolve independently.

**Independent Test**: Give a captured manifest and retrieved evidence to an external consumer. Confirm that the evidence producer supplies only observations and provenance, while any comparison or conclusion is produced outside the debugger and does not gain storage access.

**Acceptance Scenarios**:

1. **Given** an external design-evaluation consumer with a valid artifact capability, **When** it obtains a scene or visual artifact, **Then** it can consume the observed facts through bounded retrieval without direct access to server-owned storage.
2. **Given** a capture response, **When** it is inspected, **Then** it contains observation completeness and typed uncertainty only, never a design-conformance pass or fail, token assignment, root cause, or repair recommendation.
3. **Given** external contract information or an unavailable external consumer, **When** evidence is captured, **Then** the capture remains an attributable fact record and does not infer, substitute, or require an external verdict.

---

### Edge Cases

- An implementation request arrives before the operator has approved the frozen evidence contract: implementation is refused as out of scope.
- Required session, candidate, capability, scene, element, or context information is absent, invalid, non-unique, unavailable, or mismatched: the response reports the applicable typed outcome and invents no observation.
- A request is structurally invalid, exceeds an agreed limit, or uses malformed version information: it is rejected as invalid input; a structurally valid but unsupported known version is reported as unsupported.
- An observer is unavailable, disconnects, times out, returns inconsistent information, is cancelled, or exceeds a bound: the outcome is typed failure or qualified incomplete evidence, never silent partial success.
- A requested artifact portion begins at the end of an empty or non-empty artifact: retrieval returns an empty final portion; a negative, oversized, or otherwise invalid range is rejected before lookup.
- An artifact capability belongs to another session or capture, has expired, has been deleted, or never existed: the response is indistinguishable from other unavailable-artifact cases and discloses no artifact properties.
- An authorized artifact fails its integrity or immutable-identity check: retrieval returns no bytes and reports an integrity failure distinct from unavailable evidence.
- A visual preview is requested or retained alongside lossless evidence: it is explicitly non-authoritative and cannot determine scene completeness or an external comparison.
- A scene changes after a stability receipt, during guarded traversal, or while an atomic observation is being materialized: the later capture revalidates conditions and reports partial or unobservable evidence unless the atomic authority proves unchanged revisions.
- A scene contains unknown adapter-specific facts: they are preserved as bounded opaque facts or reported unsupported, never reinterpreted as a design verdict.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The feature MUST require recorded operator approval of one frozen interoperable evidence contract before any product implementation for this feature begins.
- **FR-002**: The approved contract MUST define the complete evidence vocabulary, required and optional information, versions, limits, success outcomes, qualified outcomes, error outcomes, and compatibility behavior for every M0 and M1 user outcome. Each declared primitive name MUST be structurally bound to its fixed M0 or M1 milestone and cannot pair with the other milestone.
- **FR-003**: The contract MUST distinguish structurally invalid input from structurally valid, recognized, but unsupported version or capability requests, and that distinction MUST be demonstrable with negative exchanges.
- **FR-004**: Every observation and artifact retrieval MUST be bound to one explicit local debug session, a positively identified local candidate, and an authorized capability; it MUST NOT discover a target by scanning processes or accept a remote target.
- **FR-005**: Before a consumer relies on observation behavior, the feature MUST declare supported contract versions, available observation capabilities, supported context conditions, limits, and candidate identity.
- **FR-006**: M0 visual capture MUST be read-only, bounded, and attributable. It MUST produce lossless visual evidence plus a compact manifest rather than embedding the lossless evidence in the capture response. A `COMPLETE` visual-capture result MUST include at least one descriptor with `mediaType` `image/png` and `evidenceGrade` `lossless_visual`; `PARTIAL` and `UNOBSERVABLE` may contain zero artifacts only when evidence could not be committed.
- **FR-007**: Each visual or scene artifact MUST be staged and committed immutably under server ownership, described by an opaque unguessable capability bound to its session and capture, and accompanied by media type, length, integrity value, contract version, capture identity, and retention information.
- **FR-008**: Artifact retrieval MUST accept only the owning session, an artifact capability, and a bounded range; return no more raw content than the agreed bound; preserve exact byte order; and disclose neither a storage location nor a storage-root identifier.
- **FR-009**: The first authorized artifact retrieval MUST verify complete content integrity against the manifest. Later retrievals MUST verify immutable identity and length before releasing data. A failed authorized integrity check MUST return no bytes and a distinct integrity-failure outcome.
- **FR-010**: Unknown, foreign, expired, deleted, and otherwise unavailable artifact capabilities requested by an otherwise valid session MUST return one indistinguishable unavailable-artifact outcome without disclosing existence, ownership, retention, size, integrity value, provenance, or location.
- **FR-011**: Every capture manifest MUST record attributable candidate and capture provenance, including capture identity and time, observation sequence, stated atomicity authority, candidate identity, observer identity where applicable, and supplied external contract and story references. Visual evidence MUST retain its own capture time and identity rather than silently sharing a scene epoch.
- **FR-012**: Every requested scene condition MUST be explicit: a constrained value means the consumer requires that condition, and an explicit unconstrained value means it is not required. The feature MUST report unsupported or unobservable conditions rather than substitute a meaning-changing default.
- **FR-013**: A standalone stability result MUST be evidence only. Every element or scene capture MUST independently stabilize or immediately revalidate all required conditions before committing its evidence.
- **FR-014**: A scene may be reported complete and atomic only when an explicitly authorized in-process observation authority materializes the whole bounded scene as an immutable record in one non-interruptible transaction and proves equal valid authority-owned revisions immediately before and after materialization. A `COMPLETE` native-scene result MUST include at least one descriptor with `mediaType` `application/vnd.netcoredbg.native-scene+json` and `evidenceGrade` `observed_facts`; `PARTIAL` and `UNOBSERVABLE` may contain zero artifacts only when evidence could not be committed.
- **FR-015**: Independently timed best-effort traversal MAY provide guarded scene facts, but it MUST be reported as partial or unobservable for atomic-scene requests and MUST never be presented as complete atomic evidence. A one-element observation MUST NOT imply a complete scene epoch.
- **FR-016**: Scene evidence MUST contain bounded observed facts and their stated authority, including identity where available, relations, geometry, display scale, transforms, clipping, accessibility, and supported adapter-specific facts. Unknown adapter-specific facts MUST remain typed opaque evidence or be reported unsupported.
- **FR-017**: Observation results MUST use only completeness states and typed uncertainty or failure outcomes. They MUST NOT contain design-token resolution, token-to-property mapping, conformance verdicts, root-cause analysis, or repair advice.
- **FR-018**: An external design-evaluation consumer MUST obtain artifacts only through the opaque bounded-retrieval capability. It owns design-contract interpretation, comparison, diagnosis, and repair planning; it MUST NOT receive debug-session authority, target discovery authority, or server-storage authority.
- **FR-019**: M0-G0 consists solely of contract freezing, exact-byte loading, contract/runtime-validator parity on concrete fixtures, corpus syntax and reference integrity, negative structural/version exchanges, and operator approval. It validates the declared classification vocabulary and each corpus case's gate metadata; it does not execute observer, artifact, stability, or atomicity behavior, nor require full behavioral C001–C024 GREEN before M0. T032 and T034 execute and record the complete behavioral C001–C024 mapping after M0/M1 implementation. M0 adds capability declaration, attributable lossless visual evidence, safe artifact retrieval, provenance, and typed outcomes. M1 adds stability, element and bounded-scene facts, and qualified atomicity. M2 is a future boundary for additional effective presentation facts; M3 for richer source and resource provenance; M4 for complex-control and custom semantic-region coverage; and M5 for equivalent fact categories in an additional supported presentation environment. M2 through M5 are excluded from this feature's delivery.
- **FR-020**: The new capability MUST be independent of the retained legacy runtime. It MUST NOT execute through, invoke, or depend on that route, and it MUST leave the retained route unchanged.
- **FR-021**: The feature MUST introduce no direct storage-path retrieval, arbitrary external-address retrieval, remote listener, public route cutover, private design comparator, or deferred token-check facade during M0 or M1.

### Key Entities *(include if feature involves data)*

- **Evidence Contract**: The operator-approved, versioned agreement defining evidence meaning, exchange validity, limits, outcomes, and compatibility rules before implementation.
- **Approval Record**: The durable record that the operator approved the frozen Evidence Contract and thereby authorized the next implementation phase.
- **Observation Session**: The explicit local debugging context to which an observation request, candidate, capture, and artifact access are bound.
- **Candidate Identity**: The attributable identity and verification status of the observed local application candidate.
- **Scene Request Context**: The complete set of explicit requested conditions for a visual, element, or scene observation, including constrained and expressly unconstrained conditions.
- **Stability Receipt**: Time-bounded evidence of observed scene conditions that informs a capture but cannot independently authorize a later capture.
- **Capture Manifest**: The compact attributable record of a capture's completeness, authority, provenance, issues, and separately retrievable artifacts.
- **Artifact Capability**: An opaque, unguessable, session- and capture-bound authority for bounded retrieval of one immutable artifact.
- **Scene Record**: A bounded immutable set of observed element facts, relations, context, and stated atomicity authority; it is not a conformance result.
- **External Design-Evaluation Consumer**: The separately owned consumer that interprets design contracts and compares them with observed facts without receiving debugger or storage authority.

### Exclusions

- Design-token resolution, token-to-property mapping, conformance verdicts, root-cause analysis, and repair planning are excluded from the evidence producer.
- Building, launching, or assuming availability of an external design-evaluation consumer is excluded.
- Direct artifact-path access, storage-root disclosure, arbitrary external-address retrieval, remote observation listeners, and target discovery by process scanning are excluded.
- Screenshot- or preview-derived conformance, and labeling independently timed visual evidence as the same epoch as a scene, are excluded.
- M2 through M5 presentation, provenance, complex-control, and additional presentation-environment parity expansions are future work, not partial delivery commitments.
- Any alteration of, invocation of, or dependence on the retained legacy runtime is excluded.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Before any product implementation begins, the approved Evidence Contract validators load only the exact approved bytes; agree on every contract-gate fixture; verify corpus syntax, internal references, expected-classification vocabulary, and each C001–C024 case's `contractGateExpectation` and `runtimeBehaviorRequired` metadata. Full behavioral execution and the complete C001–C024 result mapping occur only at T032/T034 after M0/M1 implementation.
- **SC-002**: In 100% of accepted visual-capture trials, the response contains a compact manifest rather than embedded lossless evidence, and lossless evidence reconstructed from bounded retrieval portions exactly matches the manifest's integrity value and recorded length.
- **SC-003**: In 100% of accepted authorization trials, foreign, expired, deleted, and nonexistent artifact capabilities produce the same unavailable-artifact result with zero disclosed artifact metadata; in 100% of accepted tamper trials, integrity failure returns zero artifact bytes.
- **SC-004**: In 100% of accepted atomic-scene trials, a complete atomic result includes one declared authorized observation authority and equal valid before-and-after revisions; 0% of guarded best-effort traversals are reported as complete atomic scenes.
- **SC-005**: In 100% of accepted post-stability-change trials, a later capture performs fresh stabilization or revalidation and never relies solely on an earlier stability receipt.
- **SC-006**: In 100% of accepted external-consumption trials, the evidence producer emits only observation facts, completeness, and typed uncertainty; it emits no design-conformance result, token assignment, diagnosis, or repair advice, and the external consumer retrieves evidence without direct server-storage access.
- **SC-007**: In 100% of accepted M0 and M1 delivery checks, the new capability operates independently from the retained legacy runtime and leaves that runtime unchanged.

## Assumptions

- The operator is available to review and explicitly approve the frozen Evidence Contract before product implementation is authorized.
- An explicit local debugging context and positively identifiable local application candidate exist before observation is requested; the feature does not create or discover them implicitly.
- Evidence consumers can retain an opaque artifact capability and retrieve the artifact within its declared retention period and agreed bounds.
- The external design-evaluation consumer is a separately owned integration. Its absence does not prevent evidence capture, but no design comparison is produced until that consumer is available.
- Dependencies: the external design-evaluation consumer owns design-contract interpretation and is not delivered by this feature; a qualified complete atomic scene depends on a separately authorized in-process observation authority.
- A qualified complete atomic scene depends on an opt-in in-process observation authority that can provide bounded immutable scene facts and its own monotonic revision evidence. Where that authority is absent, guarded best-effort or unobservable evidence remains valid only within its stated qualification.
- The retained legacy runtime continues to exist as an independent route during this feature and is neither a dependency nor an execution path for the new capability.
- M2 through M5 require separate future approval and specification before implementation; this feature delivers only M0-G0, M0, and M1 planning boundaries.
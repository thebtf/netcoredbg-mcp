# Feature Specification: A1 Opt-In Preview Artifact Runway

**Feature Branch**: `main` (no `before_specify` hook is configured)

**Created**: 2026-08-26

**Status**: Draft

**Input**: User description: "A1 до настоящего opt-in artifact: workflow → exact downloaded-artifact proof → security review → явное решение о preview promotion, далее Program B и Program C"

**Parent**: `specs/005-stateless-preview/` and the Stateless Front-Door Convergence program.

## Clarifications

### Session 2026-08-26

- Q: Must a published, remotely verified preview be required before Program B begins? → A: Yes. Program B begins only after the exact approved opt-in preview is published and its remote bytes are verified.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use a Safe Opt-In Preview (Priority: P1)

A developer wants to try the native preview capability without replacing the currently installed Python command. They can obtain one explicitly selected preview package, verify that it is the reviewed package, configure it for a chosen local project, and complete the preview's supported read-only journey. Removing the preview selection restores the unchanged Python journey.

**Why this priority**: The preview has value only if a consumer can use the exact reviewed artifact safely while retaining a straightforward rollback path.

**Independent Test**: An independent consumer obtains the recorded preview package, verifies its identity record, performs the complete opt-in journey against a permitted local project, removes preview selection, and completes the existing Python journey without reinstalling or changing the Python route.

**Acceptance Scenarios**:

1. **Given** an approved preview package and an unchanged Python installation, **When** a developer verifies the package identity and explicitly selects the preview for a permitted local project, **Then** they can discover and use exactly the preview's one supported read-only capability without changing the default command.
2. **Given** a developer has selected the preview, **When** they remove that selection before starting a stateful session, **Then** the existing Python journey remains usable and reaches its established success outcome.
3. **Given** a preview package cannot verify its recorded identity, **When** a developer attempts to select it, **Then** the preview is not activated and the Python route remains unchanged.

---

### User Story 2 - Prove the Exact Candidate Before Distribution (Priority: P1)

A release owner needs to distinguish a reviewed preview package from a local substitute or later rebuild. They can trace every consumer, safety, and rollback result to one source revision, one retained build result, and one immutable identity record before any public distribution decision is made.

**Why this priority**: An opt-in artifact is trustworthy only when its externally tested bytes, source origin, and security evidence are provably the same.

**Independent Test**: A reviewer starts from the candidate identity record, downloads the retained package, verifies every recorded digest, replays the full positive and denial matrix, and confirms that the evidence refers to that downloaded package rather than a locally rebuilt substitute.

**Acceptance Scenarios**:

1. **Given** a source-pinned build candidate, **When** its retained package and identity record are made available for proof, **Then** the source revision, retained build result, package, manifest, and executable identities are recorded together and can be independently verified.
2. **Given** a downloaded package differs from any recorded identity, **When** proof or review is attempted, **Then** it is rejected before consumer distribution or promotion.
3. **Given** the preview's required positive and denial journeys are executed, **When** any journey reveals an unintended filesystem, process, route, or partial-output effect, **Then** the candidate is not eligible for promotion.

---

### User Story 3 - Make an Explicit, Recoverable Promotion Decision (Priority: P2)

A release authority needs to approve or decline distribution of one exact preview package, and to recover safely if a distribution attempt is interrupted. The decision names the exact evidence it applies to; a later package, changed source revision, or mismatched remote state cannot inherit it.

**Why this priority**: Explicit approval prevents an unreviewed build from becoming public, while recovery rules prevent accidental replacement of a previously published artifact.

**Independent Test**: A review record is evaluated for both approve and decline outcomes, and representative absent, partial, completed, and mismatched remote states are classified to prove that only the exact approved candidate can be promoted or recovered.

**Acceptance Scenarios**:

1. **Given** complete candidate proof and security evidence for one identity record, **When** the authorized decision is recorded, **Then** it is unambiguously `APPROVE` or `DECLINE` and names the exact candidate it applies to.
2. **Given** a declined, incomplete, expired, or mismatched candidate, **When** promotion is requested, **Then** no public preview artifact, tag, or default-route change is created from it.
3. **Given** an approved candidate and an interrupted promotion, **When** recovery is attempted, **Then** recovery proceeds only when every observed remote identity matches the approved candidate; otherwise it stops without replacing or deleting existing public artifacts.

---

### Edge Cases

- A retained build package expires or is unavailable before proof completes: the candidate is not promoted; a new candidate must establish a new identity record and repeat proof.
- The package, manifest, executable, source revision, build result, approval record, or remote artifact differs from the recorded candidate: promotion and recovery fail closed.
- A safety review finds an unresolved high-severity issue or lacks a nonzero required evidence denominator: the candidate cannot receive an approval decision.
- A user supplies an invalid project authority, requests an excluded capability, or encounters a bounded preview refusal: the preview returns only its documented refusal and does not alter the Python route or expose partial output.
- A prior tag or draft release already exists: it is recoverable only when it represents the exact approved candidate; a collision or mismatched published state is not overwritten, moved, or deleted.
- An approval is declined: no distribution is performed, and the retained Python route remains the available consumer route.

## Scope Boundaries

This feature completes the A1 artifact runway: source-pinned build, downloaded-artifact proof, exact-candidate security review, an explicit preview-promotion decision, and promotion only when that later decision approves the exact candidate.

Program B and Program C are downstream roadmap stages, not implementation scope for this feature:

- **Program B** transfers stateful, UI, DAP, and remaining route families only after the exact approved opt-in preview is published and its remote bytes are verified.
- **Program C** owns default native selection, package cutover, deprecation, and final Python removal only after Program B reaches zero Python-owned public behavior and its own consumer/rollback criteria pass.

This feature does not change the default selector, publish to the existing Python channel, alter the Python package or entrypoint, migrate state, or remove Python.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST produce a separately identifiable opt-in preview candidate from one exact source revision without changing the currently selected Python route.
- **FR-002**: Each preview candidate MUST have one immutable identity record containing its source revision, retained build result, package identity, manifest identity, executable identity, intended preview version, and intended distribution destination.
- **FR-003**: Consumer proof and security review MUST use the retained, downloaded candidate package identified by the record; a local rebuild, replacement package, or later build MUST NOT substitute for it.
- **FR-004**: The downloaded candidate MUST prove the complete supported opt-in consumer journey, including package identity verification, explicit project selection, the sole supported read-only capability, clean shutdown, and rollback to the unchanged Python journey.
- **FR-005**: The candidate proof MUST execute every defined launch, authority, input, containment, resource, protocol, transport, and rollback denial case and record whether each case produces only its documented outcome with no partial output or unintended side effect.
- **FR-006**: The candidate MUST receive independent security and code-review evidence scoped to its exact identity record, with nonzero required evidence denominators and no unresolved high-severity finding before it can be approved.
- **FR-007**: The system MUST record an explicit `APPROVE` or `DECLINE` promotion decision that names the exact identity record, review evidence, decision time, and intended preview destination. A decision MUST NOT apply to any other candidate.
- **FR-008**: Promotion MUST occur only for an approved candidate and MUST distribute only the exact reviewed package and manifest. The externally available package MUST match the approved identity record after distribution.
- **FR-009**: A promotion retry MUST recover only an absent or exactly matching interrupted state. It MUST NOT overwrite assets, move or delete a tag, replace a different release, or reinterpret mismatched remote evidence as recoverable.
- **FR-010**: A declined, unreviewed, incomplete, expired, or mismatched candidate MUST leave the preview unpublished and the Python route selected and usable.
- **FR-011**: The feature MUST preserve the public Python package, console entrypoint, default selection, legacy rollback journey, and all non-A1 route families unchanged.
- **FR-012**: A Program B handoff MUST record the published exact opt-in preview, its verified remote identity, its rollback result, and the explicit boundary for separately authorized Program B and Program C work.
- **FR-013**: Program B MUST NOT begin after a declined, unreviewed, locally proved only, unpublished, or remotely unverified A1 candidate; only a published exact approved preview with verified remote bytes satisfies the A1 handoff gate.

### Key Entities *(include if feature involves data)*

- **Preview Candidate**: One source-pinned, retained preview package proposed for opt-in distribution; it cannot be substituted after proof begins.
- **Candidate Identity Record**: The immutable association between a Preview Candidate, its source revision, retained build result, identities, target version, and destination.
- **S2/S3 Review Aggregate**: The sealed seven-lens independent security, code, and workflow review record for one Candidate Identity Record.
- **Independent PR Review Receipt**: The distinct project release-review record for the same Candidate Identity Record; it does not substitute for an S2/S3 lens.
- **Stage Gate Evidence**: The sealed exact-candidate evidence record for one pre-decision, pre-publication, or post-publication catalog stage.
- **Promotion Decision**: An explicit `APPROVE` or `DECLINE` bound to one Candidate Identity Record and passing pre-decision Stage Gate Evidence.
- **Published Preview**: The externally distributed package state, which is valid only when its identity matches an approved Candidate Identity Record.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of candidates considered for distribution have one complete Candidate Identity Record, and all recorded package, manifest, and executable identities match the independently downloaded proof bytes.
- **SC-002**: 100% of defined positive and denial-matrix scenarios are executed against the downloaded candidate with a recorded outcome; zero scenario may be replaced by a local rebuild or omitted without an explicit documented exclusion.
- **SC-003**: Every approved candidate has independent security and code-review evidence with nonzero required denominators and zero unresolved high-severity findings.
- **SC-004**: An opt-in developer can complete the preview's one supported read-only journey and then restore the Python journey by removing preview selection, with both journeys reaching their documented success outcomes in one verification session.
- **SC-005**: 100% of promotion attempts with a missing, expired, declined, incomplete, or mismatched identity are refused without creating, replacing, moving, or deleting a public preview artifact.
- **SC-006**: For each approved promotion, the externally available preview package and manifest exactly match the approved Candidate Identity Record; for each declined decision, no preview distribution is created.
- **SC-007**: A Program B handoff occurs only after an exact approved preview is published and its remote bytes match the Candidate Identity Record; the closeout records the separately authorized Program C default-cutover and Python-retirement boundary.

## Assumptions

- The previously merged A1 local source-run preview is the implementation input for this artifact runway; this feature does not re-scope or replace its local behavior contract.
- The preview remains an opt-in Windows x64 offering with one read-only capability; the published Python command remains the default and rollback route throughout A1.
- Promotion approval is an exact-candidate external decision made only after the candidate evidence exists; this specification does not pre-approve unknown future bytes.
- A declined promotion is a valid A1 proof outcome but does not satisfy the Program B entry condition; the Python route remains selected and usable.
- Program B and Program C require separate accepted scopes, implementation plans, and consumer evidence; their mention here establishes sequencing only.

# Artifact Evidence Requirements Review Checklist: A1 Opt-In Preview Artifact Runway

**Purpose**: Review whether the written A1 requirements define a complete, clear, consistent, measurable, and scope-safe artifact-evidence and promotion boundary.
**Created**: 2026-08-26
**Feature**: [spec.md](../spec.md) · [plan.md](../plan.md) · [tasks.md](../tasks.md)

**Review Ownership**: Release, security, and independent PR reviewers assess requirement quality. Mark an item `[x]` only when its written criterion is satisfied; completion never represents implementation, publication, or Program B/C authorization.
**Marker Semantics**: `[Gap]`, `[Ambiguity]`, `[Conflict]`, and `[Assumption]` identify questions that require explicit requirements resolution or review evidence.

## Candidate Authority & Evidence Completeness

- [ ] CHK001 Are canonical source revision, retained build result, package, manifest, executable, version, and destination all specified as required members of one immutable candidate identity? [Completeness, Spec §FR-002, §SC-001]
- [ ] CHK002 Are the equality relationships among retained archive, manifest, executable, and later public assets specified precisely enough to distinguish “same candidate” from merely a matching version or commit? [Clarity, Spec §FR-002–FR-003, §FR-008, §SC-001, §SC-006]
- [ ] CHK003 Are authoritative retained-artifact, downloaded-proof, and remote-proof sources defined consistently, including explicit exclusion of local rebuilds, replacements, and source-tree substitutes? [Consistency, Spec §FR-003–FR-004, Plan §Constraints]
- [ ] CHK004 Is the meaning of a trusted build result and its retention/expiry evidence specified with admissible facts rather than an undefined assumption of continued availability? [Clarity, Spec §FR-002, §Edge Cases, Research §Decision 6]
- [ ] CHK005 Does the requirements set define which candidate change requires a new identity and which earlier proof, review, decision, attempt, or remote records become ineligible? [Completeness, Plan §Tracer-Bullet Execution Plan, §Granularity quiz]
- [ ] CHK006 Are the authority boundaries for candidate identity, consumer proof, review evidence, stage evidence, decision, and handoff records defined without relying on local paths, arbitrary JSON, or matching commits alone? [Completeness, Plan §Operational Interfaces, Data Model §Lifetimes and storage classes]

## Scenario, Denominator & Acceptance-Criteria Quality

- [ ] CHK007 Does the requirements set name the canonical owner, version identity, and update rule for the complete positive-and-denial scenario matrix? [Gap, Spec §FR-005, §SC-002, Contract: artifact-consumer-proof.schema.json §scenario_matrix]
- [ ] CHK008 Are “documented outcome,” “partial output,” and “unintended side effect” defined with objective criteria that can distinguish acceptable refusal from an unsafe partial result? [Clarity, Spec §FR-005, §Edge Cases]
- [ ] CHK009 Are all launch, authority, input, containment, resource, protocol, transport, and rollback denial families either enumerated or explicitly inherited with a stable identity? [Completeness, Spec §FR-005, §SC-002, Contract: artifact-consumer-proof.schema.json §scenario_matrix]
- [ ] CHK010 Can the stated 100% scenario and evidence denominators be objectively determined when a scenario is excluded, renamed, or replaced by a later parent-contract revision? [Measurability, Spec §SC-002, Contract: artifact-consumer-proof.schema.json §scenario_matrix]
- [ ] CHK011 Are retained-artifact proof, remote-release proof, EOF, and Python rollback requirements clearly distinguished so one proof class cannot silently stand in for another? [Consistency, Spec §FR-004, §FR-011–FR-012, Plan §Operational Interfaces]
- [ ] CHK012 Are the success criteria explicit about which proof outcomes must be preserved for a safe decline or refusal, rather than describing only successful approval and publication paths? [Completeness, Spec §FR-010, §SC-005–SC-006, §Edge Cases]

## Stage Evidence, Review & Decision Clarity

- [ ] CHK013 Are the required pre-decision, pre-publication, and post-publication evidence sets complete, separately named, and tied to one exact candidate? [Completeness, Spec §Key Entities, Plan §Operational Interfaces, Contract: stage-gate-evidence.schema.json]
- [ ] CHK014 Is the temporal rule that later-stage evidence cannot be required or substituted before its stage exists stated consistently across the decision, attempt, and remote-proof requirements? [Consistency, Plan §Tracer-Bullet Execution Plan AR-05–AR-08, Contract: promotion-recovery.md §Admit an approved candidate by stage]
- [ ] CHK015 Are applicability and explicit inapplicability criteria for every release gate documented well enough that no caller-supplied gate list can omit a binding obligation? [Completeness, Contract: release-gate-catalog.md §Define the six gates, §Seal evidence by stage]
- [ ] CHK016 Is the stated policy-authority set consistent between the Plan’s security-review ownership sources and the Release Gate Catalog’s tracked snapshot sources? [Conflict, Plan §Security review ownership, Contract: release-gate-catalog.md §Bind the canonical source]
- [ ] CHK017 Are the distinct roles, required denominators, independence conditions, and non-substitutability of the S2/S3 aggregate and independent PR review specified unambiguously? [Clarity, Spec §FR-006, §Key Entities, Contract: s2-s3-review.schema.json, Contract: independent-pr-review.schema.json]
- [ ] CHK018 Is the required relation between the reviewed PR commit and the canonical merged candidate commit explicitly defined for every admissible review topology? [Ambiguity, Contract: independent-pr-review.schema.json §review_target, Plan §Tracer-Bullet Execution Plan AR-04]

## Authorization, Recovery & Refusal Coverage

- [ ] CHK019 Are `APPROVE` and `DECLINE` requirements explicit about the identity, evidence set, decision author, dispatcher, and non-transferability conditions that make a decision authoritative? [Clarity, Spec §FR-007, §FR-010, Contract: promotion-decision.schema.json]
- [ ] CHK020 Are “fresh,” “expired,” “stale,” and retry eligibility quantified or otherwise deterministically invalidated for evidence, authorization, remote observation, and promotion attempts? [Gap, Spec §FR-009–FR-010, §Edge Cases, Contract: promotion-attempt.schema.json]
- [ ] CHK021 Are current actor, run, attempt, permission, source, and decision bindings complete enough to prevent an old or different authorized-looking attempt from inheriting approval? [Completeness, Spec §FR-007–FR-009, Contract: promotion-attempt.schema.json]
- [ ] CHK022 Are all admitted remote states and their only legal recovery dispositions specified exhaustively, including `tag_only`, partial drafts, completed publication, unreadable state, and collision? [Completeness, Spec §FR-009, §Edge Cases, Contract: promotion-recovery.md §Classify remote state before mutation]
- [ ] CHK023 Is an unavailable, expired, unreadable, or identity-mismatched remote record unambiguously distinguished from an absent remote state? [Clarity, Spec §Edge Cases, Contract: promotion-recovery.md §Classify remote state before mutation]
- [ ] CHK024 Can the no-overwrite, no-move, no-delete, no-replay, and no-Python-channel safety claims be objectively assessed from the stated promotion and recovery requirements? [Measurability, Spec §FR-009–FR-010, §SC-005, Contract: promotion-recovery.md]

## Remote Proof, Handoff & Scope Containment

- [ ] CHK025 Are the freshness, independent-download, identity, denial-matrix, EOF, and rollback requirements for public prerelease proof specified separately from retained-artifact proof? [Completeness, Spec §FR-012, §SC-007, Contract: promotion-recovery.md §Close the post-publication stage]
- [ ] CHK026 Are the requirements explicit that Remote Verification and post-publication Stage Gate Evidence must exist before the Program B Handoff is eligible? [Consistency, Spec §FR-012–FR-013, Contract: program-b-handoff.md §Require every stage, §Require published remote proof]
- [ ] CHK027 Does the handoff requirement distinguish evidence that proves A1 completion from the separate authority required to start Program B or Program C? [Scope Boundary, Spec §Scope Boundaries, §FR-012–FR-013, Contract: program-b-handoff.md §Preserve program boundaries]
- [ ] CHK028 Are external dependencies—retained-artifact availability, GitHub release state, credential/permission authority, and remote-readability failures—documented with their required refusal outcomes? [Dependencies, Spec §Edge Cases, Research §Decision 6]
- [ ] CHK029 Are redaction, secret-handling, and private-path constraints for durable evidence records specified with objective acceptance conditions rather than a generic “safe evidence” expectation? [Non-Functional, Plan §Constitution Check, Contract: artifact-consumer-proof.schema.json]
- [ ] CHK030 Are Python-default preservation and same-session rollback requirements consistent between the feature scope, user scenarios, functional requirements, and success criteria? [Consistency, Spec §Scope Boundaries, §FR-004, §FR-011, §SC-004, §Assumptions]

## Notes

- This checklist intentionally complements rather than replaces `checklists/requirements.md`, which covers generic specification readiness.
- Focus areas: exact-candidate evidence authority; stage-bound promotion, recovery, remote proof, and handoff requirements.
- Depth: Standard reviewer review, with contract-level traceability where the feature already makes release evidence part of its requirements.
- Leave items unchecked when the requirement remains missing, ambiguous, inconsistent, or dependent on an unvalidated assumption.

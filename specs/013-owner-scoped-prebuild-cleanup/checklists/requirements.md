# Specification quality checklist: Owner-Scoped Pre-Build Cleanup

**Purpose:** Review the Wave 2 D2 packet before implementation.
**Feature:** [spec.md](../spec.md)
**Parent:** `specs/011-issue450-sonar-release-program/`, Wave 2.
**Status:** Planning-document review only. It does not claim source changes, test execution, candidate acceptance, an acceptance receipt, or a release.

## Content quality

- [x] The packet names `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`, PRG-003, PRG-007, PRG-008, PRG-010, Wave 1's accepted lifecycle contract, and `release_intent: none`.
- [x] The packet states the foreign-owner risk without claiming that global cleanup caused either historical issue #450 incident.
- [x] The packet sets D2 depth for this child and does not create a D3 program or release plan.
- [x] The packet identifies the selected private `WindowsOwnedProcess` boundary in `src/netcoredbg_mcp/windows_process_owner.py`.
- [x] The packet contains no fabricated test, review, receipt, publication, or source-change evidence.
- [x] All machine artifacts are in English and contain no unimplemented marker or fabricated evidence.

## Architecture completeness

- [x] `architecture.md` includes alternatives, an embedded ADR decision, caller-first types/signatures, component ownership, data flow, state/failure rules, cutover, security constraints, and D2 challenge result.
- [x] Adapter `_DapRun` and each `BuildSession` command own distinct capabilities.
- [x] The caller chain names `SessionManager.capture_prebuild_owner()`, `NoOwnedAdapter | OwnedAdapterCleanup`, and required `BuildManager.pre_launch_build(owner=...)` consumption.
- [x] The Job sequence is explicit: create Job, set limits, create suspended, assign, verify/account, wire I/O, resume.
- [x] Admission failure, resume failure, stale capture, graceful force, query failure, timeout, and outer cancellation have named outcomes.
- [x] `ActiveProcesses == 0` is the only successful Windows tree-drain condition.
- [x] The packet rejects global services, singleton maps, pywin32 dependency changes, selector fallback, breakaway, and ProcessRegistry authority.

## Requirement and task completeness

- [x] WOC-001 through WOC-013 are individually testable in `spec.md`.
- [x] The first implementation task is behavior-first RED, and its failure cause is current behavior rather than a missing planned symbol.
- [x] The RED matrix has an explicit nonzero denominator of 15 rows: O1-O11 and C1-C4.
- [x] Every WOC requirement maps to tasks and files in `plan.md` and `tasks.md`.
- [x] Every task names at least one WOC requirement and one behavior-based acceptance checkpoint.
- [x] The task graph orders RED, owner primitive, build containment, explicit pre-build migration, selector deletion, focused proof, candidate, review, judgment, and delayed receipt.
- [x] The D2 slices S1-S4 are independently valuable internal checkpoints with `release_intent: none`.

## Scope containment

- [x] The packet preserves Wave 1 generation/finalizer semantics, the Python/default route, stateless preview, package dependencies, Sonar/coverage policy, workflows, and release boundaries.
- [x] The packet preserves ProcessRegistry as observation and explicitly scoped legacy compatibility only.
- [x] The packet deletes selector authority rather than preserving a no-op wrapper or hidden fallback.
- [x] The packet does not claim containment for breakaway, `Win32_Process.Create`, or arbitrary external descendants without production-path proof.
- [x] The packet does not create an acceptance receipt during planning. T014 alone creates it after exact candidate proof, review, and judgment.

## Evidence and acceptance planning

- [x] `research.md` separates observed facts, inferred requirements, library dispositions, primary sources, and remaining proof debt.
- [x] `data-model.md` makes authority, cardinality, legal states, invalid states, and storage boundaries explicit.
- [x] `quickstart.md` names focused future proof, real Windows two-owner evidence, supporting selector scan, compatibility controls, and exact receipt preconditions without claiming execution.
- [x] `contracts/windows-owned-process.md` states preconditions, postconditions, errors, drain semantics, forbidden paths, and privacy limits.
- [x] The independent reviewer and separate acceptance judgment examine the exact final candidate SHA.

## Notes

This checklist assesses only the packet bytes. It does not replace T001's RED evidence, T010's focused GREEN evidence, T012's independent review, T013's acceptance judgment, T014's future exact receipt, or any parent release protocol.
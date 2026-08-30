# Specification Quality Checklist: Adapter Transport-Death Lifecycle

**Purpose:** Verify the Wave 1 D1 specification packet before implementation.  
**Created:** 2026-08-30  
**Feature:** [spec.md](../spec.md)  
**Parent:** `specs/011-issue450-sonar-release-program/` Wave 1.

This checklist records the pre-implementation specification review. Source, tests, an atomic candidate commit, independent review, and an acceptance receipt now exist and are recorded in `tasks.md` and `acceptance-receipt.md`; this checklist is not that evidence.

## Content quality

- [x] The packet states the observed Issue #450 user-visible contradiction without asserting an unproven adapter producer cause.
- [x] The packet binds Engram #450, `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`, the investigation record, PRG-002, PRG-007, and PRG-010.
- [x] The child is explicitly D1, Wave 1 internal verification, with no public release intent or second shipping moment.
- [x] The boundary contract names inputs, output, current integration points, and excluded surfaces.
- [x] The packet defines a DapTransportTerminal-style immutable semantic shape without declaring private field spellings to be public API.
- [x] The three observer roles and one guarded finalizer owner are named with no competing cleanup owner.
- [x] The DAP `exited`/`terminated`, adapter-process-exit, and transport-EOF distinctions are explicit.
- [x] All prose is in English for repository artifacts and contains no placeholder, TODO, or fabricated evidence.

## Requirement completeness

- [x] TD-001 through TD-010 are individually stated, testable, and mapped to exact current source/test files in `plan.md` and `tasks.md`.
- [x] TD-001 requires the first task to be a deterministic fake-subprocess EOF RED that fails because current behavior is stale, not because a planned symbol is absent.
- [x] TD-002 through TD-005 define immutable bounded terminal facts, three observers, one finalizer, and diagnostic bounds without inventing a historical cause.
- [x] TD-006 defines one manager-owned terminal state/resource transition and a truthful public `get_debug_state` outcome.
- [x] TD-007 preserves DAP protocol semantics rather than deriving one event/process fact from another.
- [x] TD-008 excludes build cleanup, Sonar, coverage, workflows, releases, package/default-route work, and stateless-preview changes.
- [x] TD-009 requires maintainable code-level docstrings/comments plus public/internal documentation.
- [x] TD-010 requires focused nonzero-denominator proof, one atomic candidate commit, one independent review, and a receipt created only after those facts exist.

## Test and acceptance planning

- [x] The plan names direct fake-process RED, client observer/diagnostic, manager integration, public liveness, resource behavior, semantic-separation, and terminal-race test categories.
- [x] The tasks keep test creation before implementation and retain existing `tests/test_client.py`, `tests/test_session.py`, `tests/test_debuggee_liveness.py`, and `tests/test_resource_updates.py` as focused owners.
- [x] The quickstart specifies post-implementation focused verification and expected behavioral observations without claiming it ran.
- [x] The plan names exactly one D1 independent reviewer and scopes review to the exact atomic candidate commit.
- [x] At planning time, the packet identified `specs/012-adapter-transport-death-lifecycle/acceptance-receipt.md` as deliberately absent until after proof and review. The retained receipt now records T014.

## Scope containment

- [x] The parent one-release/five-wave Governor decision is represented without calling Wave 1 a release.
- [x] The adjacent global pre-build cleanup defect is deferred to Wave 2 without denying its separate risk.
- [x] No Sonar suppression, exclusion, baseline reset, accepted risk, threshold change, or policy weakening is suggested.
- [x] The public Python/default route and stateless-preview boundaries are explicitly preserved.
- [x] The child does not add a second state machine, lifecycle framework, process registry extension, persistent incident store, or public debug tool.

## D1 readiness

- [x] The D1 boundary contract, integration-point list, test plan, challenge-LITE GO verdict, and one-checker commitment are present.
- [x] The packet does not add an ADR, D2/D3 milestone map, full multi-lens review scheme, or distant-program redesign.
- [x] The child can enter implementation without an operator clarification.
- [x] At planning time, the only intentionally absent artifact was the future acceptance receipt. `acceptance-receipt.md` now records the completed internal closure.

## Notes

The repository's `.specify/feature.json` is stale and was not changed or treated as authority. This packet follows the accepted manual `specs/**` convention evidenced by prior packets. The checklist is a document-quality review, not a test, review, commit, or release receipt.

# Feature Specification: Cross-Language Sonar Coverage Evidence

**Feature Branch**: `work/sonarqube-coverage-design`  
**Created**: 2026-08-24  
**Status**: Draft  
**Input**: A release maintainer needs one exact-head SonarQube scan to carry trustworthy coverage evidence for the repository's Python and .NET source, without weakening quality-gate policy or changing SonarQube server state.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Produce exact-head coverage evidence (Priority: P1)

A release maintainer runs the existing exact-head scanner command in a clean detached worktree and receives one receipt proving that coverage evidence for both supported repository languages was produced during that same scanner transaction and imported into the submitted analysis.

**Why this priority**: The current quality gate requires coverage on new code, but the runner produces none. Until the scanner can bind real coverage evidence to its exact analysis, it cannot prove a release-safe result.

**Independent Test**: A controlled exact-head runner test can model successful language-specific coverage outputs and prove that the scanner command receives the corresponding report locations, while the final receipt binds each report's normalized repository-relative path and SHA-256 to the captured HEAD.

**Acceptance Scenarios**:

1. **Given** a clean detached worktree at one fixed commit, **When** the runner executes a successful scan, **Then** it creates a new run-owned coverage directory, produces and validates the required report set for each supported language, and records every report identity with the run and commit in the receipt.
2. **Given** a report from an earlier transaction or another commit, **When** it appears at an expected coverage location, **Then** the runner rejects the pre-existing path or a mismatched run marker before scanner end.

---

### User Story 2 — Fail closed on coverage evidence defects (Priority: P2)

A release maintainer receives a precise blocked receipt rather than a plausible PASS when a required coverage report is absent, empty, malformed, outside the scanner worktree, or cannot be fingerprinted.

**Why this priority**: A valid scanner upload alone is not evidence that coverage was measured or imported. The release gate must distinguish missing evidence from a successful zero-defect release.

**Independent Test**: A focused runner suite can inject each invalid coverage-evidence condition and assert that scanner finalization/PASS publication cannot occur, while the receipt contains a secret-free failure category and no forged coverage binding.

**Acceptance Scenarios**:

1. **Given** one required language report set is missing, empty, stale, malformed, outside the worktree, or cannot be hashed, **When** the runner reaches coverage validation, **Then** it blocks before scanner end and PASS publication.
2. **Given** a producer times out, is cancelled, or leaves generated state after scanner begin, **When** the runner handles the failure, **Then** it terminates the owned child tree, attempts generated-artifact cleanup, and records the original safe failure without hiding a cleanup failure.

---

### User Story 3 — Inspect coverage provenance after a scan (Priority: P3)

A reviewer can inspect a secret-free exact-head receipt and determine which language reports were supplied, where they came from relative to the worktree, and whether their identities match the reports passed to analysis.

**Why this priority**: Release and code-review consumers need durable evidence rather than a mutable local output directory or a scanner success message.

**Independent Test**: Receipt-schema tests can reject a PASS receipt missing a language, fingerprint, normalized path, or exact-head binding.

**Acceptance Scenarios**:

1. **Given** a completed successful scan, **When** a reviewer reads its receipt, **Then** they can identify the expected report for each supported language and its immutable fingerprint without reading credentials, source bodies, or report contents.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The exact-head scanner transaction MUST own production of coverage evidence for the repository's supported Python and .NET source before scanner finalization.
- **FR-002**: The runner MUST derive expected report paths before scanner begin, then create a previously absent run-owned `.tmp` coverage directory and provenance marker after scanner begin succeeds. The marker contains the run identifier, captured commit, expected language sets, project mappings, and expected report locations.
- **FR-003**: The runner MUST validate every required coverage report as a regular, nonempty file inside that run-owned directory and MUST reject a missing, stale, malformed, escaping, symlinked, duplicate, unhashable, or unmapped-source report.
- **FR-004**: The scanner begin invocation MUST receive the complete deterministic expected language-specific report path sets as comma-delimited properties. The runner MUST validate the produced files before scanner end. The command must not rely on a conflicting committed coverage property.
- **FR-005**: A successful receipt MUST bind the run identifier, captured commit, normalized repository-relative report path, source mapping digest, language, format, XML root, byte count, and SHA-256 for every required coverage report.
- **FR-006**: Any coverage-production, timeout, cancellation, validation, or cleanup failure MUST produce a secret-free typed BLOCKED coverage outcome and MUST prevent PASS publication. The runner MUST preserve the first causal failure and record a later cleanup failure separately.
- **FR-007**: The coverage phase MUST preserve the runner's existing credential isolation, serialized scan lock, detached-worktree requirement, exact-head bindings, and quality-gate fail-closed behavior.
- **FR-008**: The runner MUST preserve separate Python and .NET evidence sets. It MUST NOT create or accept a generic cross-language merged coverage format.
- **FR-009**: The runner MUST use one bounded coverage deadline and terminate its owned producer process tree before cleanup when that deadline expires or the run is cancelled.
- **FR-010**: Before PASS validation, the runner MUST bind analysis coverage metrics to the submitted analysis through matching before-and-after current-analysis readbacks for the captured HEAD.

### Key Entities

- **Coverage evidence set**: Secret-free receipt evidence for one language. It contains one or more report bindings, the run identifier, and the digest of the matching canonical run provenance marker.
- **Analysis coverage binding**: Secret-free receipt evidence that brackets a fixed coverage-metric query with matching current-analysis readbacks for the submitted analysis and captured HEAD.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A successful controlled runner scenario records one required coverage evidence set for Python and one for .NET. Each report belongs to the current run directory, matches the marker's captured HEAD, has a nonzero XML coverage denominator, and is passed to scanner begin.
- **SC-002**: Each invalid report class, including a pre-seeded prior-run path, marker mismatch, timeout, cancellation, missing, empty, malformed, outside-worktree, symlinked, duplicate, and unhashable report, deterministically prevents scanner end and PASS receipt publication.
- **SC-003**: A receipt validator rejects a purported PASS receipt if either required language evidence set, run marker identity, report identity, or analysis coverage binding is absent, malformed, inconsistent, or out of deterministic order.
- **SC-004**: A disposable exact-head scanner scenario records the fixed coverage-metric query, matching before-and-after analysis bindings, a positive aggregate coverage metric, and evidence that the runner supplied both language report sets. It does not infer a per-language server import from aggregate metrics alone.
- **SC-005**: Existing exact-head scanner credential, cleanup, and quality-gate tests remain green after the coverage phase is added.

## Assumptions

- Existing Python and .NET tests are the authoritative behavior producers; this feature adds deterministic coverage evidence around those test workloads rather than inventing a second test suite.
- The chosen per-language coverage formats and dependency mechanism will be selected by the technical plan from current official SonarQube and coverage-tool documentation.
- SonarQube project configuration and quality-gate policy remain external authority and are not mutated by this feature.

## Out of Scope

- Changing the SonarQube quality gate, New Code definition, issue dispositions, or other server-side settings.
- Resolving the existing 137 exact new-code violations.
- Merging Python and .NET coverage into a generic combined format.
- Changing the configured SonarQube origin, credentials, redaction, or transport policy.
- Publishing, tagging, or deploying a release.

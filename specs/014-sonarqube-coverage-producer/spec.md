# Feature specification: exact-head SonarQube coverage producer

**Feature branch**: `work/issue450-sonar-coverage-producer`
**Created**: 2026-08-31
**Source base**: `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
**Parent**: `specs/011-issue450-sonar-release-program/`, Wave 3
**Design authority**: `agent://ArchitectWave3Coverage`
**Source evidence**: `agent://Wave3CoverageSource`
**Test evidence**: `agent://Wave3CoverageTests`
**Design depth**: D2
**Release intent**: `none`
**Status**: Planned packet. No implementation, diagnostic receipt, acceptance receipt, tag, publication, or release claim exists in this packet.

## Purpose

The exact-head Sonar runner must produce, validate, import, and bind real Python and .NET coverage from one captured source head. A stale, empty, unmapped, wrong-head, or partially imported report must not reach scanner end or support a release pass.

The parent packet contains an earlier Wave-3 directory pointer named `specs/014-sonarqube-cross-language-coverage/`. This packet uses the path required by this task, `specs/014-sonarqube-coverage-producer/`. The parent Wave-3 semantics remain binding. This packet does not edit the parent pointer.

## Current evidence and non-goals

`agent://Wave3CoverageSource` records that the retained runner currently executes scanner begin, builds, scanner end, and analysis readback without a coverage producer, report validation, report import argument, coverage measure binding, or coverage receipt identity. It also records a valid but blocked exact-head baseline with `new_coverage=0.0` against the unchanged threshold of `80`.

This feature does not repair the global finding denominator. It does not alter the quality gate, a threshold, New Code, the Sonar project key, exclusions, credentials, `SonarQube.Analysis.xml`, `docs/RELEASE-PROTOCOL.md`, a public runtime route, a package version, a tag, or publication.

## User scenarios and testing

### User story 1: Produce evidence from one source head

A quality owner runs the existing exact-head runner from a clean scanner worktree. The runner creates one fresh run root after scanner begin. It produces one Python Cobertura report and five fixed .NET OpenCover reports before scanner end.

**Independent test**: A focused command-spy test proves that the runner derives the plan before begin, supplies exact runtime import properties, claims the root only after begin, invokes the isolated producer with the closed five-project inventory, and calls scanner end only after local validation succeeds.

**Acceptance scenarios**:

1. **Given** a captured 40-character source head and an absent run root, **when** scanner begin succeeds, **then** the runner exclusively creates `.tmp/sonarqube-coverage/<run-id>/` and writes one canonical marker for that head and fixed inventory.
2. **Given** a coverage transaction, **when** the producer runs, **then** it writes exactly one Cobertura report and five ordered OpenCover reports at the planned paths.
3. **Given** the Stateless test project, **when** it produces coverage, **then** it alone receives the absolute `IncludeDirectory` for the production Stateless output and the production DLL and PDB bytes match their pre-producer hashes.

### User story 2: Reject invalid local coverage before import

A quality owner must know that XML existence is not mistaken for coverage evidence. The runner rejects missing, empty, malformed, zero-denominator, escaping, symlinked, duplicate, test-only, stale, or wrong-head report data before scanner end.

**Independent test**: Local marker and XML fixtures drive each failure through the runner boundary. The test observes a typed coverage failure and zero scanner-end calls.

**Acceptance scenarios**:

1. **Given** a producer returns exit code zero without the planned OpenCover file, **when** validation runs, **then** the runner returns `COVERAGE_REPORT_MISSING` and does not end the scanner.
2. **Given** a valid-shaped report whose denominator is zero or whose source paths resolve only to tests or fixtures, **when** validation runs, **then** the runner blocks the transaction.
3. **Given** a pre-seeded run directory, altered marker bytes, or a report outside the planned root, **when** the runner prepares the transaction, **then** it rejects the evidence rather than reusing it.

### User story 3: Record a diagnostic without changing release authority

A quality owner needs server evidence that both language source sets contributed to the submitted analysis. The diagnostic record remains non-release evidence while unrelated findings keep the overall Quality Gate red.

**Independent test**: Fake Sonar API responses prove that the runner requires matching submitted-analysis and current-analysis identities, positive coverage measures, complete component paging, and positive mapped contributions from both language source sets.

**Acceptance scenarios**:

1. **Given** all six reports validate, **when** scanner end and Compute Engine processing succeed, **then** two current-analysis bookends and the submitted analysis bind to the captured head.
2. **Given** project-level coverage is positive but only one language has mapped component evidence, **when** the diagnostic validator evaluates the result, **then** it returns `COVERAGE_IMPORT_UNPROVEN`.
3. **Given** both language sets and the unchanged `new_coverage` condition are valid, **when** the runner records a diagnostic result, **then** it writes schema-v3 evidence with `release_intent: none`, never a release pass.

## Scope boundaries

### In scope

- A runner-owned coverage transaction in `scripts/run_sonarqube_exact_head.py`.
- A thin `build/coverage.sh` producer and `.coveragerc` configuration.
- Isolated, locked `uv` execution with temporary `coverage==7.15.4`, without changing `pyproject.toml`, `uv.lock`, or the project `.venv`.
- Exact private `coverlet.msbuild` `10.0.1` references in the five fixed test projects.
- Local report, path, source, marker, head, analysis, and cleanup validation.
- Runtime scanner report arguments, schema-v3 diagnostic evidence, and focused runner tests.

### Out of scope

- Any source or test edit while authoring this packet.
- `SonarQube.Analysis.xml` coverage properties, static report globs, a second scanner command, a generic coverage merge, a filter, an exclusion, a threshold, or a New Code change.
- A release-role waiver, a tag, a package publication, a public route change, or a claim that the global Quality Gate is green.
- A timeout or process-tree ownership design beyond foreground producer completion. A later timeout feature must consume a proven owner capability or re-enter design.

## Functional requirements

| ID | Requirement | Observable future acceptance |
| --- | --- | --- |
| **COV-001** | `scripts/run_sonarqube_exact_head.py` MUST remain the only scanner, analysis-binding, and receipt authority. | The existing `candidate` and `post-merge` entry points remain the only release callers. `build/coverage.sh` has no token, scanner, API, discovery, or receipt behavior. |
| **COV-002** | The runner MUST derive a `CoveragePlan` from one captured 40-character head before scanner begin without creating files. | A focused test observes deterministic absolute and scanner-relative report paths without filesystem writes. |
| **COV-003** | After scanner begin succeeds, the runner MUST claim a previously absent UUID run root and canonical marker with exclusive operations. | Pre-seeded root, marker, path escape, or altered marker bytes block the run before scanner end. |
| **COV-004** | Scanner begin MUST receive exactly one Python report property and one ordered five-report OpenCover property as runtime arguments. | Command capture contains the two exact slash-normalized relative arguments and no static XML coverage property. |
| **COV-005** | The producer environment MUST contain no `SONAR_*` name or secret value. | Captured `uv`, shell, pytest, restore, test, and test-host environments contain no `SONAR_*` variable. |
| **COV-006** | `.coveragerc` MUST make Python branch coverage, relative source paths, and `src/netcoredbg_mcp` the only Python measurement root. | Cobertura XML has positive line and branch denominators and every accepted mapping resolves under `src/netcoredbg_mcp`. |
| **COV-007** | Python coverage MUST run through `uv run --isolated --locked --extra dev --with coverage==7.15.4` without modifying `pyproject.toml`, `uv.lock`, or `.venv`. | Command capture and file hashes prove the external tool route and unchanged permanent Python dependency surfaces. |
| **COV-008** | Each fixed .NET test project MUST reference `coverlet.msbuild` version `10.0.1` with `PrivateAssets=all`. | The five project files contain the exact direct test-only reference. |
| **COV-009** | The .NET producer MUST run exactly the closed five-project inventory in the declared order. | Command capture rejects broad build inventory, project substitution, fixture projects, duplicate IDs, and wrong report count. |
| **COV-010** | Every .NET producer MUST restore its exact test project and run without `--no-build`, filters, source exclusions, module filters, report merge, or threshold switches. | A zero-exit/no-report fixture blocks with `COVERAGE_REPORT_MISSING`; command capture rejects prohibited arguments. |
| **COV-011** | Only the Stateless test project MUST receive the absolute `IncludeDirectory` for `host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0`. | Its report maps a production Stateless source file, and the selected production DLL and PDB hashes are unchanged after collection. |
| **COV-012** | The runner MUST validate Cobertura roots, denominators, branch denominators, file mappings, path containment, and source-set uniqueness before scanner end. | Missing, malformed, zero, absolute, URI, duplicate, missing, test-only, symlinked, or escaping Python inputs block. |
| **COV-013** | The runner MUST validate all five OpenCover roots, direct sequence-point denominators, aggregate branch denominator, production mappings, and the Stateless mapping before scanner end. | Missing, malformed, zero, test-only, fixture-only, duplicate, escaping, or invalid .NET inputs block. |
| **COV-014** | Scanner end MUST be unreachable until marker, reports, source sets, report hashes, host restoration, and post-producer head all validate. | An event spy observes `begin -> claim -> build -> produce -> validate -> head check -> end`; any earlier failure has zero end calls. |
| **COV-015** | After scanner end, the runner MUST bind the submitted analysis and two current-analysis bookends to the captured head before it reads coverage measures. | A changed analysis ID or revision blocks the diagnostic result. |
| **COV-016** | The runner MUST require positive analysis-bound aggregate coverage and lines-to-cover and an unchanged `new_coverage` condition with threshold `80` and status `OK`. | A zero, malformed, mismatched, or non-OK measure condition blocks the diagnostic result. |
| **COV-017** | The runner MUST prove both language source sets contributed server component measures. | Complete component paging intersects each validated source set and finds positive line coverage plus at least one branch measure for Python and .NET. |
| **COV-018** | Diagnostic evidence MUST use schema version 3, be secret-free, and carry `release_intent: none`. | Schema-v2, an omitted coverage section, a forged hash, a path escape, a report body, or a diagnostic-as-pass representation is rejected. |
| **COV-019** | Candidate and post-merge PASS validation MUST require schema-v3 coverage evidence with no compatibility path for schema-v2 receipts. | A release validator rejects missing, stale, incomplete, forged, or diagnostic coverage evidence. |
| **COV-020** | Cleanup MUST remove only the claimed run root after foreground producers terminate. Cleanup failure MUST remain secondary to the first causal failure. | The cleanup test preserves foreign `.tmp` content, reports only planned relative paths, and retains primary failure precedence. |
| **COV-021** | Global Quality Gate, findings, project identity, thresholds, New Code, exclusions, credentials, and release protocol authority MUST remain unchanged. | The final source diff excludes `SonarQube.Analysis.xml` and `docs/RELEASE-PROTOCOL.md`; the diagnostic record names unresolved global blockers instead of waiving them. |
| **COV-022** | The implementation MUST satisfy the 15 behavior-first RED/GREEN rows in [tasks.md](tasks.md#binding-redgreen-matrix). | Every row fails through the current caller behavior before its implementation and passes only after its owned contract is present. |
| **COV-023** | A future Wave-3 acceptance receipt MUST be created only by the final task after exact-head review, independent acceptance judgment, and the diagnostic transaction succeed. | No receipt file exists before the final task. The final receipt names the implementation SHA, diagnostic path and hash, review, judge, and remaining global blockers. |

## Success criteria

| ID | Measurable outcome | Requirement links |
| --- | --- | --- |
| **SC-001** | The produced evidence contains exactly six planned reports: one Python Cobertura report and five ordered .NET OpenCover reports. | COV-002 to COV-011 |
| **SC-002** | Every accepted report has a positive local denominator, a positive branch denominator, and at least one valid intended production mapping. | COV-006, COV-012, COV-013 |
| **SC-003** | 100% of injected local producer, marker, report, mapping, and head failures make scanner end unreachable. | COV-003, COV-012 to COV-014, COV-020 |
| **SC-004** | The post-analysis component inventory proves a positive covered-line contribution for both Python and .NET source sets. | COV-015 to COV-017 |
| **SC-005** | The diagnostic record is schema-v3, redacts secrets, reports `release_intent: none`, and cannot satisfy release PASS validation. | COV-018, COV-019, COV-021 |
| **SC-006** | All 15 RED/GREEN rows have an exact focused command, a nonzero oracle, a source owner, and a task dependency. | COV-022 |
| **SC-007** | No acceptance or diagnostic receipt is created by this planning packet. The future receipt exists only after the final task completes. | COV-023 |

## Key entities

- **Coverage plan**: The pure, head-bound declaration of the run root, marker, Python report, five .NET reports, and source inventory.
- **Coverage run claim**: The exclusive on-disk ownership of one planned root and marker after scanner begin.
- **Marker**: Canonical JSON bytes that bind one run ID, head, project key, expected report set, tool versions, and producer/config hashes.
- **Report evidence**: The identity, format, denominator, source set, and hash of one validated report. It never stores a report body.
- **Coverage measure snapshot**: Analysis-bound aggregate and per-language Sonar values bracketed by current-analysis readbacks.
- **Diagnostic receipt**: A schema-v3, non-release record for one exact analysis. It can be `DIAGNOSTIC_COMPLETE` or `BLOCKED`; it cannot be `PASS`.

## Requirement traceability

| Requirement | Design and contract authority | Implementation slice and tasks | Future proof |
| --- | --- | --- | --- |
| COV-001 to COV-004 | [architecture.md](architecture.md), [contracts/coverage-evidence.md](contracts/coverage-evidence.md) | S3, T012 to T015 | V01, V02, V11 |
| COV-005 to COV-007 | [architecture.md](architecture.md#producer-commands), [research.md](research.md) | S2, T006 to T009 | V03 to V05, V12 |
| COV-008 to COV-011 | [architecture.md](architecture.md#fixed-net-producer-inventory), [data-model.md](data-model.md) | S2, T010 to T011 | V06 to V10 |
| COV-012 to COV-014 | [data-model.md](data-model.md#validation-invariants), [contracts/coverage-evidence.md](contracts/coverage-evidence.md) | S1 and S3, T001 to T005 and T016 to T018 | V04, V05, V08 to V11 |
| COV-015 to COV-017 | [architecture.md](architecture.md#analysis-binding), [data-model.md](data-model.md#analysis-evidence) | S3, T019 to T020 | V13, V14 |
| COV-018 to COV-020 | [data-model.md](data-model.md#diagnostic-receipt), [contracts/diagnostic-receipt-v3.schema.json](contracts/diagnostic-receipt-v3.schema.json) | S3 and S4, T020 to T023 | V15 |
| COV-021 to COV-023 | [plan.md](plan.md#scope-and-rollback), [tasks.md](tasks.md#slice-s4-release-role-enforcement-and-delayed-receipt) | S4, T024 to T028 | Final exact-head review, judge, and delayed receipt |

## Receipt timing

This packet intentionally contains schemas and a contract, not a receipt. `acceptance-receipt.md` is absent. The final implementation task, T028, is the first task that may create a diagnostic receipt and the Wave-3 acceptance receipt. Any red condition leaves both receipts absent and Wave 3 open.

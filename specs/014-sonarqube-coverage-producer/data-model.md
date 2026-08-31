# Data model: exact-head coverage evidence

**Status**: Future evidence contract. It creates no database, persistent runtime service, or current receipt.
**Source base**: `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
**Release intent**: `none`

## Modeling rules

1. A changed source head creates new evidence. No record can be relabeled as evidence for different source bytes.
2. A planned coverage path is not report evidence. The runner accepts only the exact report that the plan names.
3. A report is not analysis evidence. The submitted analysis, current-analysis bookends, measures, and mapped component evidence must bind it to the captured head.
4. A diagnostic record is never a release pass. Its allowed outcomes are `DIAGNOSTIC_COMPLETE` and `BLOCKED`.
5. A missing or zero denominator is a blocker. It is not a zero-coverage result and is not a passing empty set.
6. Receipts store metadata, relative paths, counts, hashes, safe error codes, and page summaries. They do not store credentials, environment dumps, report bodies, or secret-bearing command lines.

## Entity overview

| Entity | Identity | Lifetime | Contract |
| --- | --- | --- | --- |
| `CoveragePlan` | `run_id`, captured head, and deterministic relative paths | In memory before scanner begin | [architecture.md](architecture.md#caller-first-contract) |
| `CoverageRunClaim` | `CoveragePlan` plus marker bytes/hash | From exclusive claim through cleanup | [coverage-run-marker.schema.json](contracts/coverage-run-marker.schema.json) |
| `ReportEvidence` | Report ID, byte hash, source-set hash | From local validation through receipt sealing | [coverage-evidence.md](contracts/coverage-evidence.md) |
| `CoverageEvidence` | One marker plus exactly six ordered reports | From local validation through receipt sealing | [diagnostic-receipt-v3.schema.json](contracts/diagnostic-receipt-v3.schema.json) |
| `CoverageMeasureSnapshot` | Submitted analysis ID, revision, metric set, component pages | After scanner end | [coverage-evidence.md](contracts/coverage-evidence.md#analysis-binding) |
| `CleanupEvidence` | Claimed root and relative removals | Finally path | [coverage-evidence.md](contracts/coverage-evidence.md#cleanup) |
| `DiagnosticReceiptV3` | Head, run ID, project key, and record path | Durable non-release evidence | [diagnostic-receipt-v3.schema.json](contracts/diagnostic-receipt-v3.schema.json) |
| `CoverageFailure` | First code, stage, language/project context | Terminal result for one run | [coverage-evidence.md](contracts/coverage-evidence.md#failure-semantics) |

## Core types

```text
Sha40 = lowercase 40-character hexadecimal Git commit
Sha256 = lowercase 64-character hexadecimal digest
RunId = UUID
RelativePath = slash-separated nonempty path without URI scheme, drive, absolute root, or .. segment

CoverageStage =
  PLANNED |
  SCANNER_BEGUN |
  RUN_CLAIMED |
  PRODUCING |
  REPORTS_VALIDATED |
  SCANNER_ENDED |
  ANALYSIS_BOUND |
  CLEANED |
  BLOCKED

CoveragePath {
  absolute: Path
  scanner_relative: RelativePath
}

CoverageProjectSpec {
  id: codesearch-core | host | stateless-preview | stateless | host-prompts
  project: RelativePath
  report: CoveragePath
  include_directory: Path | null
  restore_check_files: tuple[Path, ...]
}

CoveragePlan {
  run_id: RunId
  head: Sha40
  project_key: "thebtf_netcoredbg_mcp"
  root: Path
  marker: CoveragePath
  python_data: Path
  python_report: CoveragePath
  dotnet: tuple[CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec]
}

CoverageRunClaim {
  plan: CoveragePlan
  marker_sha256: Sha256
  marker_bytes: positive integer
}
```

## Coverage plan and marker

`derive_coverage_plan` is pure. It receives `GitContext` and a `RunId`, computes the root and all report paths, and returns the fixed inventory in the order below. It creates no directory and writes no marker.

| Index | Report ID | Language | Format | Relative path |
| --- | --- | --- | --- | --- |
| 1 | `python` | `python` | `cobertura` | `.tmp/sonarqube-coverage/<run-id>/python/coverage.xml` |
| 2 | `codesearch-core` | `dotnet` | `opencover` | `.tmp/sonarqube-coverage/<run-id>/dotnet/codesearch-core/coverage.opencover.xml` |
| 3 | `host` | `dotnet` | `opencover` | `.tmp/sonarqube-coverage/<run-id>/dotnet/host/coverage.opencover.xml` |
| 4 | `stateless-preview` | `dotnet` | `opencover` | `.tmp/sonarqube-coverage/<run-id>/dotnet/stateless-preview/coverage.opencover.xml` |
| 5 | `stateless` | `dotnet` | `opencover` | `.tmp/sonarqube-coverage/<run-id>/dotnet/stateless/coverage.opencover.xml` |
| 6 | `host-prompts` | `dotnet` | `opencover` | `.tmp/sonarqube-coverage/<run-id>/dotnet/host-prompts/coverage.opencover.xml` |

After scanner begin succeeds, `claim_coverage_run` creates `CoveragePlan.root` exclusively and writes `coverage-run.json` with `O_EXCL` semantics. The marker uses canonical, sorted, compact JSON followed by one LF. It binds:

- `schema_version: 1`
- `run_id`
- `captured_head`
- `project_key`
- the exact ordered six-report list
- `coverage_py: 7.15.4`
- `coverlet_msbuild: 10.0.1`
- SHA-256 hashes of the producer and `.coveragerc`

The JSON Schema checks shape and fixed values. The runner checks uniqueness, exact ordering, path containment, canonical bytes, and equality to the in-memory plan.

## Report evidence

```text
ReportEvidence {
  id: python | codesearch-core | host | stateless-preview | stateless | host-prompts
  language: python | dotnet
  format: cobertura | opencover
  relative_path: RelativePath
  sha256: Sha256
  bytes: positive integer
  xml_root: coverage | CoverageSession
  denominator: positive integer
  covered_count: nonnegative integer
  branch_denominator: nonnegative integer
  branch_covered_count: nonnegative integer
  source_paths: sorted tuple[RelativePath, ...]
  source_set_sha256: Sha256
}

CoverageEvidence {
  marker: {
    relative_path: RelativePath
    sha256: Sha256
    bytes: positive integer
    schema_version: 1
  }
  reports: tuple[ReportEvidence, ReportEvidence, ReportEvidence, ReportEvidence, ReportEvidence, ReportEvidence]
  python_totals: {
    lines_valid: positive integer
    lines_covered: nonnegative integer
    branches_valid: positive integer
    branches_covered: nonnegative integer
  }
  dotnet_totals: {
    sequence_points: positive integer
    visited_sequence_points: nonnegative integer
    branch_points: positive integer
    visited_branch_points: nonnegative integer
  }
  stateless_host_binary: {
    dll_before: Sha256
    dll_after: Sha256
    pdb_before: Sha256
    pdb_after: Sha256
  }
}
```

### Report cardinality and ordering

`CoverageEvidence.reports` contains exactly six members. They must use the marker order. `python` is first. The five .NET report IDs follow the declared fixed inventory. A missing, extra, reordered, duplicated, or differently named report invalidates the evidence.

### Python evidence invariants

1. `xml_root == "coverage"`.
2. `lines_valid > 0`, `0 <= lines_covered <= lines_valid`.
3. `branches_valid > 0`, `0 <= branches_covered <= branches_valid`.
4. Every source path is a unique tracked regular `.py` file under `src/netcoredbg_mcp`.
5. No accepted source path is a URI, absolute path, `..` escape, duplicate normalized path, test-only path, missing path, symbolic link, or reparse point.

### .NET evidence invariants

1. `xml_root == "CoverageSession"`.
2. Each report uses exactly one direct `Summary` for its sequence denominator.
3. Each `denominator` is `numSequencePoints > 0`, and `0 <= covered_count <= denominator`.
4. Each branch count is nonnegative and ordered. `CoverageEvidence.dotnet_totals.branch_points > 0`.
5. Each report resolves at least one tracked non-test, non-fixture `.cs` file inside the worktree.
6. The allowed source set excludes `bin`, `obj`, `tests/fixtures`, and `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures`.
7. The `stateless` report contains at least one source path below `host/NetCoreDbg.Mcp.Stateless`.
8. The stored Stateless DLL/PDB before and after hashes are equal. A mismatch is `COVERAGE_INSTRUMENTATION_NOT_RESTORED`.

## Analysis evidence

```text
AnalysisIdentity {
  analysis_id: nonempty opaque string
  revision: Sha40
}

LanguageComponentEvidence {
  source_set_sha256: Sha256
  page_count: positive integer
  complete: true
  mapped_path_count: positive integer
  lines_to_cover: positive integer
  covered_lines: positive integer
  branch_measure_path_count: positive integer
  mapped_paths_sha256: Sha256
}

CoverageMeasureSnapshot {
  submitted: AnalysisIdentity
  current_before_measures: AnalysisIdentity
  current_after_measures: AnalysisIdentity
  current_final: AnalysisIdentity
  aggregate: {
    coverage: finite number greater than zero
    lines_to_cover: positive integer
    uncovered_lines: nonnegative integer
    line_coverage: finite number
    branch_coverage: finite number
    new_coverage: finite number
    new_lines_to_cover: positive integer
    new_uncovered_lines: nonnegative integer
    new_line_coverage: finite number
    new_branch_coverage: finite number
  }
  new_coverage_condition: {
    status: OK
    threshold: 80
    actual_value: aggregate.new_coverage normalized to the runner's numeric form
  }
  python_components: LanguageComponentEvidence
  dotnet_components: LanguageComponentEvidence
}
```

### Analysis identity equations

```text
captured_head
== submitted.revision
== current_before_measures.revision
== current_after_measures.revision
== current_final.revision

submitted.analysis_id
== current_before_measures.analysis_id
== current_after_measures.analysis_id
== current_final.analysis_id
```

The runner reads the two bookends around the measure and component query. The final current-analysis read also protects the existing finding/hotspot readback. A differing project, analysis ID, revision, incomplete page set, non-finite number, or missing measure blocks the result.

### Per-language import equation

For each language `L` in `{python, dotnet}`:

```text
validated_source_set(L) ∩ normalized_server_component_paths(L) != ∅
component_pages(L).complete == true
component_lines_to_cover(L) > 0
component_covered_lines(L) > 0
component_branch_measure_path_count(L) > 0
```

A positive aggregate project coverage value without both language equations is `COVERAGE_IMPORT_UNPROVEN`.

## Cleanup and failure evidence

```text
CleanupEvidence {
  claimed_root: RelativePath
  producer_terminal: true
  removed_paths: tuple[RelativePath, ...]
  parent_removed_if_empty: boolean
  status: OK | FAILED
  failure: SafeFailure | null
}

CoverageFailure {
  code: string
  stage: CoverageStage
  language: python | dotnet | null
  project_id: python | codesearch-core | host | stateless-preview | stateless | host-prompts | null
  safe_message: nonempty string
}

SafeFailure {
  code: string
  message: nonempty string
}
```

`CleanupEvidence.claimed_root` must equal the marker root. `removed_paths` must be relative to that root or the coverage parent. The parent may be removed only when empty. Cleanup cannot delete arbitrary `.tmp` content.

The first causal `CoverageFailure` is the terminal failure. If cleanup also fails, `cleanup.failure` is secondary. Cleanup never turns a failure into `DIAGNOSTIC_COMPLETE`, and it cannot make scanner end legal.

## Failure vocabulary

| Code | Stage | Required effect |
| --- | --- | --- |
| `COVERAGE_TOOL_UNAVAILABLE` | `PLANNED` | Block before scanner begin. Do not create a run root. |
| `COVERAGE_RUN_ROOT_EXISTS` | `SCANNER_BEGUN` | Block. Scanner end is forbidden. |
| `COVERAGE_MARKER_INVALID` | `RUN_CLAIMED` | Block. Scanner end is forbidden. |
| `COVERAGE_PYTHON_FAILED` | `PRODUCING` | Block with language `python`. Scanner end is forbidden. |
| `COVERAGE_DOTNET_RESTORE_FAILED` | `PRODUCING` | Block with the exact project ID. Scanner end is forbidden. |
| `COVERAGE_DOTNET_TEST_FAILED` | `PRODUCING` | Block with the exact project ID. Scanner end is forbidden. |
| `COVERAGE_REPORT_MISSING`, `COVERAGE_REPORT_EMPTY`, `COVERAGE_REPORT_MALFORMED`, `COVERAGE_REPORT_WRONG_ROOT` | `PRODUCING` or `REPORTS_VALIDATED` | Block even when producer exit code is zero. Scanner end is forbidden. |
| `COVERAGE_DENOMINATOR_ZERO`, `COVERAGE_BRANCH_DENOMINATOR_ZERO` | `REPORTS_VALIDATED` | Block. Do not reinterpret the absence as zero coverage. |
| `COVERAGE_SOURCE_MAPPING_INVALID`, `COVERAGE_HEAD_MISMATCH`, `COVERAGE_INSTRUMENTATION_NOT_RESTORED` | `REPORTS_VALIDATED` | Block. Scanner end is forbidden. |
| `COVERAGE_SCANNER_END_FAILED`, `COVERAGE_ANALYSIS_MISMATCH`, `COVERAGE_IMPORT_UNPROVEN`, `COVERAGE_MEASURES_INVALID` | `SCANNER_ENDED` or `ANALYSIS_BOUND` | Record the submitted analysis if present, then block. |
| `COVERAGE_CLEANUP_FAILED` | `CLEANED` | Record only as secondary cleanup evidence. |

## Diagnostic receipt

The future receipt uses [diagnostic-receipt-v3.schema.json](contracts/diagnostic-receipt-v3.schema.json).

```text
DiagnosticReceiptV3 {
  schema_version: 3
  role: diagnostic
  outcome: DIAGNOSTIC_COMPLETE | BLOCKED
  release_intent: none
  captured_head: Sha40
  project_key: "thebtf_netcoredbg_mcp"
  coverage: CoverageEvidence
  analysis: CoverageMeasureSnapshot | null
  unresolved_global_blockers: {
    complete: boolean
    current_issue_count: nonnegative integer
    blocking_issue_count: nonnegative integer
    hotspot_count: nonnegative integer
  }
  cleanup: CleanupEvidence | null
  failure: CoverageFailure | null
}
```

A `DIAGNOSTIC_COMPLETE` record requires complete local evidence, an exact analysis identity, positive both-language component evidence, the unchanged `new_coverage` condition `OK` at threshold `80`, and `release_intent: none`. It may retain unresolved global issue/finding blockers.

A `BLOCKED` record retains only the evidence safely reached before the failure. It is not a partial pass and cannot be converted into a release result. The JSON schema and runner cross-field validation reject a schema-v2 receipt, an omitted coverage section, a raw report body, an absolute receipt path, a release PASS outcome, or a changed `release_intent`.

## Receipt state and final task

No `DiagnosticReceiptV3` or `acceptance-receipt.md` exists in this planning packet. The final future task creates both only after the implementation head has passed focused proof, independent source review, and independent acceptance judgment. Any earlier task may define a schema or test a synthetic fixture, but it must not seal a real receipt.

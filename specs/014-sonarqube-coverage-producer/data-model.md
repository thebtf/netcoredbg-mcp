# Data model: exact-head coverage evidence

**Status**: Future evidence contract. It creates no database, persistent runtime service, or current receipt.
**Release intent**: `none`

## Modeling rules

1. A changed source head creates new evidence. No record can be relabeled for different source bytes.
2. Sonar sees two final report identities only: Python Cobertura and .NET Cobertura. The five .NET project outputs are private producer inputs.
3. A private input is not final report evidence. The runner must validate and normalize the complete fixed input set before it accepts the final .NET report.
4. A diagnostic record is never a release PASS. Its legal outcomes are `DIAGNOSTIC_COMPLETE` and `BLOCKED`.
5. A `DIAGNOSTIC_COMPLETE` record requires a complete, immutable, hash-bound issue and hotspot inventory. Counts alone are invalid.
6. A missing or zero denominator is a blocker. It is not a zero-coverage result or a passing empty set.
7. Receipts store safe metadata, relative paths, counts, and hashes. They do not store credentials, environment dumps, raw report bodies, or secret-bearing command lines.

## Entity overview

| Entity | Identity | Lifetime | Contract |
| --- | --- | --- | --- |
| `Wave2ClosureEntryV1` | Accepted Wave-2 candidate, PR-head identity, and closure receipt hash | Before all Wave-3 execution | [wave2-closure-entry-v1.schema.json](contracts/wave2-closure-entry-v1.schema.json) |
| `CoveragePlan` | Run ID, captured head, and deterministic paths | In memory before scanner begin | [architecture.md](architecture.md#caller-first-contract) |
| `ToolchainPreflight` | Plan and five evaluated project tuples | Before scanner begin | COV-004 |
| `CoverageRunClaim` | Plan plus marker bytes/hash | From exclusive claim through cleanup | [coverage-run-marker.schema.json](contracts/coverage-run-marker.schema.json) |
| `CoverageEvidence` | Marker, two final reports, five private inputs, normalizer proof | From local validation through receipt sealing | [exact-head-receipt-v3.schema.json](contracts/exact-head-receipt-v3.schema.json) |
| `DiagnosticInventoryV1` | Exact analysis identity and complete inventories | After analysis, before receipt sealing | [diagnostic-inventory-v1.schema.json](contracts/diagnostic-inventory-v1.schema.json) |
| `ExactHeadReceiptV3` | Role, outcome, identity, and evidence links | Durable role evidence | [exact-head-receipt-v3.schema.json](contracts/exact-head-receipt-v3.schema.json) |
| `CoverageFailure` | First code and stage | Terminal result for one run | [coverage-evidence.md](contracts/coverage-evidence.md) |

## Entry evidence and preflight

```text
Wave2ClosureEntryV1 {
  schema_version: 1
  wave: 2
  closure_status: EXACT_CLOSED
  release_intent: none
  tracked_relative_path: "specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json"
  accepted_candidate_sha: Sha40
  closure_receipt: { relative_path, sha256 }
  integration: { kind: pull_request_head, pull_request: 289, head_ref: string, head_sha: Sha40 }
}

ToolchainPreflight {
  uv: available
  bash: available
  dotnet: available
  projects: tuple[ProjectCompatibility, ProjectCompatibility, ProjectCompatibility, ProjectCompatibility, ProjectCompatibility]
}

ProjectCompatibility {
  id: ProjectId
  target_framework: net8.0
  coverlet_msbuild: 10.0.1
  coverlet_private_assets: all
  test_sdk: 17.12.0
  test_platform: vstest
}
```

`verify_wave2_entry` first requires the tracked source schema to state `integration.kind: pull_request_head`, `release_intent: none`, immutable receipt hash and content, and PR #289 head identity. At runtime it separately proves PR #289 merged through GitHub/workflow evidence or first-parent merge proof. It then derives `observed_main_sha` from `origin/main` and `artifact_commit_sha` from the artifact path's repository history, and verifies accepted candidate and artifact commit ancestry to observed main. It rejects a missing or untracked artifact, source kind mismatch, PR head mismatch, absent merge proof, feature branch as runtime authority, missing receipt, non-`none` intent, or ancestry mismatch as `WAVE2_CLOSURE_UNVERIFIED`.

`resolve_wave2_entry` reads only `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json` from the checked-out repository. Wave-2 T014/PR #289 creates the premerge source after the current open PR does its closure work. Existing role callers do not pass an entry-file argument. The source never claims merged state. After `RUN_CLAIMED`, the runner writes a hash-bound resolved copy with `observed_main_sha` beneath the run root; that copy is provenance, not authority.

`preflight_coverage_toolchain` runs before scanner begin. It requires executable `uv`, `bash`, and `dotnet`, evaluates every fixed project, and rejects active MTP. It never injects a property to switch the test platform. `COVERAGE_TOOL_UNAVAILABLE`, `COVERAGE_VSTEST_INCOMPATIBLE`, and `COVERAGE_MTP_INCOMPATIBLE` stop at `PLANNED` before root claim.

## Coverage plan and marker

```text
CoveragePlan {
  run_id: RunId
  head: Sha40
  project_key: "thebtf_netcoredbg_mcp"
  root: Path
  marker: CoveragePath
  tracked_wave2_entry: RepositoryPath
  resolved_wave2_entry: CoveragePath
  python_data: Path
  python_report: CoveragePath
  dotnet_report: CoveragePath
  dotnet_inputs: tuple[CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec, CoverageProjectSpec]
}

CoverageProjectSpec {
  id: codesearch-core | host | stateless-preview | stateless | host-prompts
  project: RelativePath
  raw_cobertura_input: CoveragePath
  include_directory: Path | null
}
```

`derive_coverage_plan` is pure. It writes no file. After scanner begin succeeds, `claim_coverage_run` creates the UUID root exclusively, writes canonical sorted compact JSON, and writes the hash-bound resolved Wave-2 entry below that root. The marker binds the tracked source path/hash, candidate SHA, PR head SHA, runtime artifact commit SHA, runtime observed main SHA, and resolved-copy path.

| Kind | ID | Format | Relative path |
| --- | --- | --- |
| Run provenance | `wave2-entry` | canonical JSON | `.tmp/sonarqube-coverage/<run-id>/wave2-entry.json` |
| Final scanner report | `python` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/python/coverage.xml` |
| Final scanner report | `dotnet` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml` |
| Private producer input | `codesearch-core` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/inputs/codesearch-core/coverage.cobertura.xml` |
| Private producer input | `host` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/inputs/host/coverage.cobertura.xml` |
| Private producer input | `stateless-preview` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/inputs/stateless-preview/coverage.cobertura.xml` |
| Private producer input | `stateless` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/inputs/stateless/coverage.cobertura.xml` |
| Private producer input | `host-prompts` | Cobertura | `.tmp/sonarqube-coverage/<run-id>/dotnet/inputs/host-prompts/coverage.cobertura.xml` |

The marker binds the tracked Wave-2 source plus the two final reports, all five ordered producer inputs, the normalizer algorithm and order, tool versions, producer hash, and `.coveragerc` hash. It does not treat private inputs as Sonar reports.

## Coverage evidence

```text
FinalReportEvidence {
  id: python | dotnet
  language: python | dotnet
  format: cobertura
  relative_path: RelativePath
  sha256: Sha256
  bytes: positive integer
  xml_root: coverage
  lines_valid: positive integer
  lines_covered: nonnegative integer
  branches_valid: positive integer
  branches_covered: nonnegative integer
  source_paths: sorted unique tuple[RelativePath, ...]
  source_set_sha256: Sha256
}

DotnetInputEvidence {
  id: ProjectId
  project: RelativePath
  relative_path: RelativePath
  sha256: Sha256
  bytes: positive integer
  lines_valid: positive integer
  lines_covered: nonnegative integer
  branches_valid: nonnegative integer
  branches_covered: nonnegative integer
  source_paths: sorted unique tuple[RelativePath, ...]
  source_set_sha256: Sha256
}

CoverageEvidence {
  run_id: RunId
  marker: ArtifactReference
  final_reports: tuple[FinalReportEvidence, FinalReportEvidence]
  dotnet_producers: tuple[DotnetInputEvidence, DotnetInputEvidence, DotnetInputEvidence, DotnetInputEvidence, DotnetInputEvidence]
  normalization: {
    algorithm: cobertura-merge-normalize-v1
    input_set_sha256: Sha256
    output_report_id: dotnet
    source_union_complete: true
  }
  stateless_host_binary: { dll_sha256: Sha256, pdb_sha256: Sha256, restored: true }
}
```

The final report tuple is ordered `python`, then `dotnet`. Any missing, extra, reordered, or substituted member fails. Each private input must map a tracked non-test production `.cs` path. The five inputs together must have a positive branch denominator. The final .NET report must have positive line and branch denominators and exactly the normalized source union.

## Canonical identity and analysis evidence

`ExactHeadReceiptV3.identity` is the one canonical record for `captured_head`, `project_key`, and `analysis_id`. The receipt does not duplicate mutable identity fields. The runner proves that submitted analysis and every current-analysis observation equal that identity before it emits `analysis.observations` with all slots true.

```text
AnalysisEvidence {
  observations: {
    submitted: true
    current_before_measures: true
    current_after_measures: true
    current_final: true
  }
  aggregate: {
    coverage: finite number greater than zero
    lines_to_cover: positive integer
    new_coverage: finite number
    new_lines_to_cover: positive integer
  }
  new_coverage_condition: { status: OK, threshold: 80, actual_value: number }
  python_components: LanguageComponentEvidence
  dotnet_components: LanguageComponentEvidence
}
```

For each language, the runner requires complete component paging, at least one mapped path, positive lines to cover, positive covered lines, and a branch measure. A project aggregate never proves both reports were imported.

## Diagnostic inventory authority

`DiagnosticInventoryV1` is a create-new artifact. It stores the canonical identity and complete issue and hotspot inventories. Each page summary has `complete: true`, `result_empty`, page size and count, total, record count, full-key SHA-256, blocking-key count, and blocking-key SHA-256. Each issue record retains its key, component, path, rule, status, resolution, type, and severity. Each hotspot record retains its key, component, path, rule, and status.

The diagnostic receipt stores an `InventoryReference` with the inventory artifact's coordination-relative path, SHA-256, byte count, schema version, and complete issue/hotspot summaries. The runner validates the artifact schema, full record count, unique keys, page totals, key digests, and exact identity before receipt sealing. `complete:false`, count-only data, absent records, or a hash or identity mismatch is `COVERAGE_INVENTORY_INCOMPLETE`.

This makes the artifact sufficient for Wave 4 to derive a fresh manifest and assign each blocking key exactly once. It does not assign the owner itself.

## Unified v3 receipt roles

| Role | Legal outcome | `release_intent` | Completion rule |
| --- | --- | --- | --- |
| `diagnostic` | `DIAGNOSTIC_COMPLETE` or `BLOCKED` | `none` | Complete diagnostic requires coverage, analysis, full inventory, successful cleanup, and no failure. |
| `candidate` | `PASS` or `BLOCKED` | `v0.23.11` | PASS requires the same full evidence plus Quality Gate `OK` and zero blocking issue/hotspot counts. |
| `post-merge` | `PASS` or `BLOCKED` | `v0.23.11` | PASS requires the same full evidence plus Quality Gate `OK` and zero blocking issue/hotspot counts. |

Schema version is exactly `3`. The runner rejects schema version 2 and has no compatibility branch. A diagnostic can never be a PASS. `scripts/stateless_preview_artifact.py` is a v3 post-merge consumer and must reject schema version 2 in the same cutover.

## Failure vocabulary

| Code | Stage | Required effect |
| --- | --- | --- |
| `WAVE2_CLOSURE_UNVERIFIED` | `PLANNED` | Block implementation transaction before preflight, scanner begin, and root claim. |
| `COVERAGE_TOOL_UNAVAILABLE` | `PLANNED` | Block before scanner begin and root claim. |
| `COVERAGE_VSTEST_INCOMPATIBLE`, `COVERAGE_MTP_INCOMPATIBLE` | `PLANNED` | Block before scanner begin and root claim. |
| `COVERAGE_RUN_ROOT_EXISTS`, `COVERAGE_MARKER_INVALID` | `SCANNER_BEGUN`, `RUN_CLAIMED` | Block. Scanner end is forbidden. |
| `COVERAGE_PYTHON_FAILED`, `COVERAGE_DOTNET_RESTORE_FAILED`, `COVERAGE_DOTNET_TEST_FAILED` | `PRODUCING` | Block with exact language and project context. |
| `COVERAGE_REPORT_MISSING`, `COVERAGE_REPORT_EMPTY`, `COVERAGE_REPORT_MALFORMED`, `COVERAGE_DENOMINATOR_ZERO`, `COVERAGE_SOURCE_MAPPING_INVALID`, `COVERAGE_DOTNET_NORMALIZATION_FAILED` | `PRODUCING`, `REPORTS_VALIDATED` | Block. Scanner end is forbidden. |
| `COVERAGE_HEAD_MISMATCH`, `COVERAGE_INSTRUMENTATION_NOT_RESTORED` | `REPORTS_VALIDATED` | Block. Scanner end is forbidden. |
| `COVERAGE_ANALYSIS_MISMATCH`, `COVERAGE_IMPORT_UNPROVEN`, `COVERAGE_MEASURES_INVALID`, `COVERAGE_INVENTORY_INCOMPLETE` | `SCANNER_ENDED`, `ANALYSIS_BOUND` | Record safe reached evidence, then block. |
| `COVERAGE_CLEANUP_FAILED` | `CLEANED` | Secondary cleanup evidence only. |

No receipt exists in this planning packet. T028 may create a real diagnostic record and inventory artifact only after the exact implementation head satisfies all predecessor tasks.

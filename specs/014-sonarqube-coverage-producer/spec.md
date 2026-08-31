# Feature specification: exact-head SonarQube coverage producer

**Feature branch**: `work/issue450-sonar-coverage-producer`
**Created**: 2026-08-31
**Source base**: `5d482b418118a9f17bf40fa0ab40b3c594df34d1`
**Parent**: `specs/011-issue450-sonar-release-program/`, Wave 3
**Design authority**: `agent://ArchitectWave3Coverage`
**Release intent**: `none`
**Status**: T000 entry gate passed against merged Wave-2 evidence. The tracked source schema, reviewed-head equality, canonical Git-blob hashes, first-party PR/head/merge binding, candidate lineage, equal integration trees, artifact path history, and merge-to-observed-main lineage all validate. T001 through T028 may proceed; no implementation, diagnostic receipt, acceptance receipt, tag, publication, or release claim exists here.

## Purpose

The retained exact-head runner must produce, validate, import, and bind two deterministic project-root-relative Cobertura reports from one captured source head:

- `.tmp/sonarqube-coverage/<run-id>/python/coverage.xml`
- `.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml`

The fixed five .NET test projects are producer inputs. They emit five private Cobertura inputs below `dotnet/inputs/`. The runner validates and normalizes those inputs into the one final .NET Cobertura report. Sonar receives only the two final report identities. A stale, empty, unmapped, wrong-head, incomplete, or substituted artifact must not reach scanner end or support a release PASS.

The parent packet has an earlier Wave-3 directory pointer named `specs/014-sonarqube-cross-language-coverage/`. This packet uses the required directory `specs/014-sonarqube-coverage-producer/`. The parent contract remains binding. This packet does not edit the parent pointer.

## Entry condition

Wave 3 requires a schema-valid tracked [Wave-2 closure artifact](contracts/wave2-closure-entry-v1.schema.json) at `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json`. Wave-2 T014 wrote it on PR #289 before that PR's squash merge. Its truthful source state is `integration.kind: pull_request_head`; it carries `release_intent: none`, the accepted Wave-2 implementation candidate SHA, immutable closure receipt hash, and a reviewed source ref/SHA. `integration.head_sha` is the reviewed implementation head and must equal `accepted_candidate_sha`; it is not the final PR head. The source must not claim that PR #289 is already merged or contain a future main SHA.

At Wave-3 runtime, fail-closed first-party PR evidence must bind PR #289's actual PR-head OID to `merge_commit_sha`. The runner requires `accepted_candidate_sha` to be ancestor-or-equal to that actual PR head and requires `tree(actual PR head) == tree(merge commit)`. It records the shared tree ID as `integrated_tree_sha`.

The runner derives `artifact_commit_sha` from current history for the tracked path. It requires that commit to equal `merge_commit_sha` or be ancestor-or-equal to `observed_main_sha`, and it requires `merge_commit_sha` to be ancestor-or-equal to `observed_main_sha`. It hashes the tracked artifact and closure receipt from canonical Git blob bytes, not checkout bytes. The tracked artifact blob must equal the artifact blob at the actual PR head.

Only after scanner begin and run-root claim does the runner write the hash-bound resolved entry and marker. They store `source_sha256`, `accepted_candidate_sha`, actual `pull_request_head_sha`, `artifact_commit_sha`, `merge_commit_sha`, `integrated_tree_sha`, and `observed_main_sha`. A missing tracked artifact, wrong source kind, non-`none` intent, reviewed-head mismatch, wrong PR head or merge OID, missing first-party evidence, unequal trees, mismatched canonical blob hash, invalid path history, or stale observed-main lineage is `WAVE2_CLOSURE_UNVERIFIED` and blocks implementation and diagnostic execution before preflight, scanner begin, and root claim.

## User scenarios and testing

### User story 1: Produce parent-compatible evidence from one source head

A quality owner runs the existing exact-head runner from a clean scanner worktree only after the Wave-2 entry record validates. Before scanner begin, the runner validates the producer toolchain. It then creates one fresh run root after scanner begin, produces the Python final report and five private .NET inputs, and normalizes the .NET inputs into the single final report.

**Independent test**: A focused command and event spy proves `wave2-entry -> preflight -> begin -> claim -> build -> produce -> normalize -> validate -> head-check -> end`. It proves that a failed entry or preflight has zero begin and claim calls.

**Acceptance scenarios**:

1. **Given** valid Wave-2 entry evidence, an available toolchain, a captured 40-character source head, and an absent run root, **when** the transaction runs, **then** the runner creates one canonical marker after begin and records only the two final scanner report paths.
2. **Given** the fixed five .NET producers, **when** they complete, **then** the runner accepts their private Cobertura inputs only after source and denominator validation and writes one normalized `.NET` Cobertura report.
3. **Given** the Stateless producer, **when** it collects coverage, **then** it alone receives the exact `IncludeDirectory`, maps production Stateless source, and preserves the selected DLL and PDB hashes.

### User story 2: Fail before scanner begin when the producer cannot run safely

A quality owner must know that an unavailable tool or an incompatible test platform cannot strand a scanner transaction. The runner resolves `uv`, `bash`, and `dotnet`, checks the five-project VSTest compatibility tuple, and refuses Microsoft Testing Platform activation before scanner begin.

**Independent test**: A command spy injects missing `uv`, `bash`, or `dotnet`, an incorrect `coverlet.msbuild` or `Microsoft.NET.Test.Sdk` version, and active MTP. Every case returns a typed planned-stage failure with zero begin and claim calls.

### User story 3: Record a complete diagnostic without release authority

A quality owner needs a complete current issue and hotspot inventory that Wave 4 can use to assign every blocking key once. A `DIAGNOSTIC_COMPLETE` record binds a create-new, hash-checked inventory artifact that contains all paginated records and their routing fields. Counts alone never satisfy the record.

**Independent test**: Synthetic receipt and inventory fixtures prove that incomplete pagination, count-only summaries, an artifact hash mismatch, a missing key, or an identity mismatch cannot produce `DIAGNOSTIC_COMPLETE`.

### User story 4: Make every exact-head role use one v3 evidence contract

A release owner needs candidate and post-merge PASS validation to consume the same fully specified v3 coverage evidence as diagnostic collection, while retaining distinct role and outcome rules.

**Independent test**: The v3 validator rejects schema v2, missing coverage, diagnostic-as-PASS, incomplete inventory, forged marker or report linkage, stale identity, and a PASS without zero blocking findings or hotspots.

## Scope boundaries

### In scope

- A runner-owned two-report Cobertura transaction in `scripts/run_sonarqube_exact_head.py`.
- A thin `build/coverage.sh` producer, deterministic `build/prepare_preview_fixture.py` local test-input builder, `.coveragerc`, and direct private `coverlet.msbuild` `10.0.1` references in the fixed five test projects. The fixture is created only below the claimed run root, is passed by explicit environment to the Preview test project, and is deleted with that root; it has no release or publication authority.
- The fixed merge-and-normalize algorithm that turns the five private .NET Cobertura inputs into the one final .NET Cobertura report.
- The tracked Wave-2 artifact as an external entry prerequisite, unified v3 receipt schema, complete diagnostic inventory schema, runtime scanner arguments, focused runner tests, and receipt validation.
- A v3 receipt-consumer migration for `scripts/stateless_preview_artifact.py` and its focused tests plus a hosted post-merge workflow verification step. Public stateless-preview behavior remains unchanged.

### Out of scope

- Any source or test edit while authoring this packet.
- A parent-contract amendment, a second scanner, static XML coverage properties, report globs, generic report discovery, an alternate report format, a filter, an exclusion, a threshold, or a New Code change.
- Automatic MTP fallback or project-property injection. MTP is refused by this packet.
- A release-role waiver, tag, package publication, public route change, or claim that the global Quality Gate is green.

## Functional requirements

| ID | Requirement | Observable future acceptance |
| --- | --- | --- |
| **COV-001** | `scripts/run_sonarqube_exact_head.py` remains the only scanner, analysis-binding, normalization, and receipt authority. | The producer has no scanner, API, discovery, acceptance, or receipt behavior. |
| **COV-002** | Wave-3 implementation and diagnostic execution require the tracked Wave-2 closure artifact at `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json`. | Before preflight, the `pull_request_head` source schema, canonical Git-blob source and receipt hashes, `release_intent: none`, and `accepted_candidate_sha == integration.head_sha` must validate. Fail-closed first-party PR evidence supplies the actual PR head and merge OIDs. The runner requires candidate-to-PR-head lineage, equal PR-head and merge trees, valid artifact-path history, and merge ancestry to runtime-derived `observed_main_sha`. The marker records the resolved identities and one `integrated_tree_sha`. |
| **COV-003** | The runner derives a pure head-bound `CoveragePlan` before scanner begin without writing files. | A focused test observes deterministic final and private-input paths without filesystem writes. |
| **COV-004** | The runner preflights `uv`, `bash`, and `dotnet`, plus the exact Coverlet VSTest tuple, before scanner begin and run-root claim. | Missing tools, `coverlet.msbuild != 10.0.1`, `Microsoft.NET.Test.Sdk != 17.12.0`, or active MTP fail at `PLANNED` with zero begin and claim calls. |
| **COV-005** | After scanner begin, the runner claims only a previously absent UUID root and canonical marker. | A pre-seeded root, altered marker, or path escape blocks without scanner end. |
| **COV-006** | Scanner begin receives exactly two runtime Cobertura properties: Python and the one final .NET report. | The arguments use slash-relative final paths and `sonar.cs.cobertura.reportsPaths`; XML contains no coverage path. |
| **COV-007** | Producer children receive no `SONAR_*` names or values. | Captured `uv`, Bash, pytest, restore, test, and test-host environments contain no Sonar variable. |
| **COV-008** | Python coverage uses the isolated locked `uv` route, `.coveragerc` source policy, and a deterministic non-live pytest workload without permanent dependency changes. Customer-mode critical, installed-wheel WPF, and real Windows owner gates remain separate verification surfaces and are not rerun inside coverage generation. | The Python final report has positive line and branch denominators and only valid `src/netcoredbg_mcp` mappings; excluded live gates retain their independent release/runtime evidence. |
| **COV-009** | The .NET producer accepts exactly five ordered `net8.0` VSTest projects with direct private `coverlet.msbuild` `10.0.1` and `Microsoft.NET.Test.Sdk` `17.12.0`. | The command and evaluated-project checks reject substitutions, duplicate IDs, wrong versions, broad inventory use, and MTP. |
| **COV-010** | Each fixed .NET producer restores and tests without `--no-build`, caller-supplied filters, exclusions, thresholds, or merge switches, and writes its planned private Cobertura input. The Preview producer receives a deterministic local source-run artifact. The Stateless producer alone applies the runner-owned fixed `Coverage!=Exclude` trait filter because all 12 `NetCoreDbgSessionProcessCollection` classes are timing-sensitive independent process/integration gates under Coverlet. | A zero-exit missing input blocks with `COVERAGE_REPORT_MISSING`; the local Preview fixture is hash-checked and removed with the run root; no project other than Stateless has a filter; every process-collection class is trait-marked and retains independent test evidence. |
| **COV-011** | The runner normalizes all five validated private .NET inputs into one deterministic final `.NET` Cobertura report. | The normalizer has a fixed input order, canonical source order, positive final denominators, and no scanner-visible private input identity. |
| **COV-012** | Only the Stateless producer receives the fixed `IncludeDirectory` and must preserve its production DLL/PDB bytes. | A changed binary or missing production Stateless mapping blocks before scanner end. |
| **COV-013** | The runner validates the Python final report for Cobertura shape, positive denominators, containment, and source-set uniqueness. | Missing, malformed, zero, absolute, URI, duplicate, test-only, symlinked, or escaping mappings block. |
| **COV-014** | The runner validates every private .NET Cobertura input and the normalized final .NET Cobertura output for source safety and positive denominators. | An invalid input, invalid normalized output, dropped source, added source, zero final denominator, or path escape blocks before scanner end. |
| **COV-015** | Scanner end is unreachable until marker, two final reports, five input records, normalization, source sets, hashes, host restoration, and post-producer head validate. | The event spy observes the required order and every prior injected failure has zero end calls. |
| **COV-016** | After scanner end, the runner binds the submitted analysis and all current-analysis observations to one captured head and analysis identity. | A changed analysis ID or revision blocks the result. |
| **COV-017** | The runner proves both final language source sets contributed server component measures. | Complete component paging finds positive mapped coverage and a branch measure for Python and .NET. |
| **COV-018** | Diagnostic receipts use unified schema version 3, remain secret-free, and have `release_intent: none`. | A diagnostic record allows only `DIAGNOSTIC_COMPLETE` or `BLOCKED`; it cannot be PASS. |
| **COV-019** | Candidate and post-merge PASS validation requires the unified schema-v3 coverage, identity, inventory, cleanup, and release-gate shape with no schema-v2 path. | Missing, stale, forged, diagnostic, incomplete, or v2 evidence cannot pass a release role. |
| **COV-020** | `DIAGNOSTIC_COMPLETE` requires complete paginated issue and hotspot inventories bound to an immutable, hash-checked artifact sufficient for Wave-4 routing. | `complete:false`, count-only data, hash mismatch, identity mismatch, missing records, or missing routing fields are rejected. |
| **COV-021** | Cleanup removes only the claimed root after foreground producers terminate and preserves the first causal failure. | The cleanup proof retains foreign content and reports cleanup failure only as secondary evidence. |
| **COV-022** | Global Quality Gate, findings, project identity, thresholds, New Code, exclusions, credentials, and release protocol authority remain unchanged. | The final source diff excludes immutable comparison surfaces and leaves global blockers explicit. |
| **COV-023** | The implementation satisfies the 15 behavior-first RED/GREEN rows in [tasks.md](tasks.md#binding-redgreen-matrix). | Every row has a caller-level RED reason, a nonzero GREEN oracle, an owner task, and a dependency. |
| **COV-024** | A Wave-3 acceptance receipt is delayed until exact-head review, independent acceptance judgment, verified Wave-2 entry evidence, and a complete diagnostic transaction succeed. | No real receipt exists before the final task. |
| **COV-025** | Every existing exact-head receipt consumer must migrate to the unified v3 contract in the same cutover. | `scripts/stateless_preview_artifact.py` rejects v2 and validates a v3 post-merge receipt without changing public artifact behavior. |
| **COV-026** | The hosted post-merge workflow must prove a clean checkout can resolve and validate the tracked Wave-2 entry without `.agent` provisioning. | The workflow derives canonical Git-blob hashes, actual PR-head and merge identities, `integrated_tree_sha`, and `observed_main_sha`. It validates the reviewed-head lineage, equal integration trees, artifact-path history, and merge-to-main ancestry before post-merge scan and artifact sealing. It fails absent, untracked, or invalid input. |

## Success criteria

| ID | Measurable outcome | Requirement links |
| --- | --- | --- |
| **SC-001** | Sonar receives exactly two deterministic project-root-relative Cobertura reports. | COV-003, COV-006, COV-011 |
| **SC-002** | Five fixed .NET test projects are private producer inputs, not scanner report identities. | COV-009 to COV-014 |
| **SC-003** | Every entry or toolchain failure leaves both scanner begin and root claim unreachable. | COV-002, COV-004, COV-015 |
| **SC-004** | A complete diagnostic binds a complete immutable issue/hotspot inventory usable for one-owner Wave-4 routing. | COV-016 to COV-020 |
| **SC-005** | Candidate and post-merge PASS plus their existing artifact consumer use the v3-only discriminated receipt contract. | COV-018, COV-019, COV-025 |
| **SC-006** | The matrix has exactly 15 rows and traceability is bidirectional. | COV-023 |
| **SC-007** | No receipt, tag, publication, or release claim is created while authoring this packet. | COV-024 |

## Requirement traceability

| Requirement range | Design and contract authority | Tasks | Future proof |
| --- | --- | --- | --- |
| COV-001 to COV-006 | [architecture.md](architecture.md), [coverage-run-marker.schema.json](contracts/coverage-run-marker.schema.json), [wave2-closure-entry-v1.schema.json](contracts/wave2-closure-entry-v1.schema.json) | T000 to T014 | V01, V02, V06, V07, V13 |
| COV-007 to COV-012 | [architecture.md](architecture.md#producer-commands), [coverage-evidence.md](contracts/coverage-evidence.md) | T003, T006 to T011, T015 to T016 | V03, V08 to V12 |
| COV-013 to COV-017 | [data-model.md](data-model.md), [coverage-evidence.md](contracts/coverage-evidence.md) | T001, T004, T013 to T019 | V04, V05, V11 to V14 |
| COV-018 to COV-020 | [exact-head-receipt-v3.schema.json](contracts/exact-head-receipt-v3.schema.json), [diagnostic-inventory-v1.schema.json](contracts/diagnostic-inventory-v1.schema.json) | T005, T018, T021 to T022 | V15 |
| COV-021 to COV-026 | [plan.md](plan.md), [tasks.md](tasks.md#slice-s4-release-role-enforcement-and-delayed-receipt) | T020 to T028 | Focused proof, review, judgment, consumer cutover, clean-clone entry verification, and delayed receipt |

## Receipt timing

This packet contains schemas and contracts, not a receipt. `acceptance-receipt.md` is absent. T028 is the first task that may create a diagnostic record, its complete inventory artifact, and the Wave-3 acceptance receipt. Any red condition leaves all three absent and Wave 3 open.

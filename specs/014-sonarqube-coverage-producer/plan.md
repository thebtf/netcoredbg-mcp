# Implementation plan: exact-head SonarQube coverage producer

**Branch**: `work/issue450-sonar-coverage-producer`
**Date**: 2026-08-31
**Spec**: [spec.md](spec.md)
**Design depth**: D2
**Parent**: `specs/011-issue450-sonar-release-program/`, Wave 3
**Source base**: `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
**Release intent**: `none`
**Execution status**: BLOCKED. Wave-2 PR #289 is open. No Wave-3 implementation or diagnostic task may start until a valid `Wave2ClosureEntryV1` names a verified accepted-main closure identity.

## Summary

Extend the retained exact-head runner into one parent-compatible coverage transaction. It verifies Wave-2 entry evidence and the producer toolchain before scanner begin, derives two final Cobertura paths, claims a root after begin, produces Python coverage and five private .NET Cobertura inputs, normalizes the .NET inputs into one final .NET Cobertura report, validates evidence, then ends the scanner. It binds analysis and complete issue/hotspot inventories to the captured head and emits a non-release diagnostic record.

The implementation changes the runner, focused runner tests, `.coveragerc`, `build/coverage.sh`, and five test project files. It adds no static scanner configuration, policy change, product behavior change, or release action.

## Technical context

| Concern | Constraint |
| --- | --- |
| Parent report contract | Exactly two deterministic project-root-relative Cobertura reports: Python and one merged .NET report. |
| .NET producer inventory | Five closed `net8.0` VSTest projects are private inputs in fixed order. They are not Sonar report identities. |
| Pre-begin gate | `uv`, `bash`, `dotnet`, Coverlet `10.0.1`, Test SDK `17.12.0`, and VSTest must validate before begin and claim. MTP is refused. |
| Scanner handoff | Runtime begin arguments name the two final Cobertura paths. `SonarQube.Analysis.xml` remains unchanged. |
| Normalization | The runner owns `cobertura-merge-normalize-v1`; it validates all inputs and emits only the final `.NET` report. |
| Diagnostic authority | `DIAGNOSTIC_COMPLETE` binds a create-new hash-checked inventory artifact with complete issue/hotspot records. |
| Release roles | Unified v3 schema supports diagnostic, candidate, and post-merge roles with discriminated legal outcomes. |

## Scope and rollback

### Planned mutation surfaces

| Surface | Change |
| --- | --- |
| `scripts/run_sonarqube_exact_head.py` | Add Wave-2 entry validation, preflight, plan/marker, runtime properties, producer call, normalizer, validators, inventory writer, unified v3 validation, and cleanup. |
| `tests/test_sonarqube_exact_head_runner.py` | Add the 15 behavior-first rows and fixture builders. |
| `.coveragerc` | Add Python branch, relative-path, and source configuration. |
| `build/coverage.sh` | Add strict enumerated producer commands for Python and five private .NET inputs. |
| Five fixed test `.csproj` files | Add direct private `coverlet.msbuild` `10.0.1` references. |

### Immutable comparison surfaces

`pyproject.toml`, `uv.lock`, `SonarQube.Analysis.xml`, `docs/RELEASE-PROTOCOL.md`, runtime product code, public routes, project key, thresholds, New Code, exclusions, credentials, finding dispositions, package version, tag, and publication remain unchanged.

Rollback removes the coverage transaction as one cohesive Wave-3 change. It never falls back to a branch-derived Wave-2 entry, an external report, an optional v2 validator, a policy reduction, or static report discovery.

## Slices and dependencies

```mermaid
flowchart LR
  Entry[Verified Wave-2 entry]
  S1[S1 behavior-first RED matrix]
  S2[S2 producers and fixed inputs]
  S3[S3 transaction, normalizer, inventory]
  S4[S4 v3 role enforcement and delayed receipt]
  Entry --> S1 --> S2 --> S3 --> S4
```

| Slice | Outcome | Exact work | Blocked by | Acceptance checkpoint |
| --- | --- | --- | --- | --- |
| **S1** | Current behavior cannot silently satisfy the entry, preflight, report, identity, inventory, or role contract. | Add 15 focused RED rows. | Verified Wave-2 entry | Each row has a caller-level RED result. |
| **S2** | The producer can create Python evidence and five private .NET inputs without permanent Python dependency changes. | Add `.coveragerc`, shell, five references, and producer proof. | S1 | Inputs have fixed paths and no scanner authority. |
| **S3** | The runner produces exactly two final reports, refuses unsafe pre-begin/input/normalization paths, and creates a complete diagnostic inventory. | Add entry/preflight, plan, marker, scanner args, normalizer, validation, analysis, inventory, and cleanup. | S2 | Focused proof is green and synthetic complete/blocked evidence validates correctly. |
| **S4** | Candidate and post-merge PASS cannot bypass unified v3 evidence. A real diagnostic remains delayed. | Remove v2 compatibility, review, judge, freeze, and final diagnostic. | S3 | The exact head binds all evidence. T028 is the first receipt-producing task. |

## Requirements-to-files map

| Requirements | Tasks | Planned files | Future proof |
| --- | --- | --- | --- |
| COV-001 to COV-006 | T000 to T014 | Runner, focused tests, entry/marker schemas | V01, V02, V06, V07, V13 |
| COV-007 to COV-012 | T003, T006 to T016 | Shell, config, five projects, runner, tests | V03, V08 to V12 |
| COV-013 to COV-017 | T001, T004, T013 to T019 | Runner and focused tests | V04, V05, V11 to V14 |
| COV-018 to COV-020 | T005, T018, T021 to T022 | Unified receipt and inventory schemas, runner, tests | V15 |
| COV-021 to COV-024 | T020 to T028 | Owned implementation files and evidence | Focused proof, review, judgment, delayed receipt |

## Verification plan

| Layer | Command or proof | What it proves |
| --- | --- | --- |
| Entry and preflight | Focused command/event spy | Missing Wave-2 entry, missing tools, invalid tuple, and MTP leave begin and claim unreachable. |
| RED/GREEN contract | `uv run --locked --extra dev pytest tests/test_sonarqube_exact_head_runner.py -q` | All 15 rows exercise the runner boundary. |
| Local producer | Runner-owned producer invocation | Five private inputs normalize into one final .NET Cobertura report and one Python report. |
| Diagnostic transaction | `python scripts/run_sonarqube_exact_head.py --role diagnostic --wave2-entry-evidence <verified-entry>` | Same-head evidence, two-report import, canonical identity, complete inventory, and unchanged coverage condition. |
| Release roles | Unified v3 receipt validators | Schema v2, diagnostic-as-PASS, missing linkage, incomplete inventory, or stale identity cannot PASS. |
| Independent review and judgment | Exact implementation SHA | No policy or comparison-surface mutation; receipt remains non-release evidence. |

No authoring command runs a formatter, linter, build, test suite, scanner, or release operation.

## Delayed receipt rule

`contracts/` contains schemas and contracts only. This packet contains no `acceptance-receipt.md` and no diagnostic inventory artifact. T028 may create a diagnostic record, its inventory artifact, and the Wave-3 acceptance receipt only after T000 through T027 succeed. A blocked result creates none of them.

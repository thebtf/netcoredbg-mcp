# Implementation Plan: Cross-Language Sonar Coverage Evidence

**Branch**: `work/sonarqube-coverage-design` | **Feature ID**: `010-sonarqube-cross-language-coverage-producer` | **Date**: 2026-08-24 | **Spec**: [spec.md](spec.md)

**Input**: Add a runner-owned coverage phase that binds Python and .NET coverage reports to the exact-head SonarQube analysis.

## Summary

The exact-head runner will clear stale generated artifacts, derive `.tmp/sonarqube-coverage` report paths, arm cleanup, and pass deterministic import properties to scanner begin. After begin succeeds, it will claim a fresh run-owned coverage directory and marker, build the current project inventory, run bounded Python and .NET coverage producers, terminate timed-out child trees, validate source mappings and fingerprint the marker and reports, then call scanner end. A successful receipt will bind report evidence to the current run, `captured_head`, and bracketing analysis coverage metrics. Any failure after cleanup is armed will publish a secret-free BLOCKED receipt after finally-owned cleanup.

The implementation does not alter the SonarQube quality gate, New Code policy, issue state, configured origin, or credential model.

## Technical context

**Languages**: Python 3.10 or later, C# projects targeting .NET 8, and the current workstation .NET SDK 10.0.111.  
**Primary dependencies**: Existing `pytest`, xUnit, and SonarScanner for .NET. Operator-approved additions are `coverage` 7.15.4 and `coverlet.msbuild` 10.0.1.  
**Storage**: Generated XML reports and a canonical run marker live under `.tmp/sonarqube-coverage/<run_id>`. The durable secret-free receipt and implementation evidence live under `<coordination-root>/.agent/`.
**Testing**: `tests/test_sonarqube_exact_head_runner.py`, controlled language producer probes, the documented full Python suite with bytecode/cache output disabled, and the closed five-project VSTest .NET inventory.  
**Target platform**: The current Windows exact-head scanner route. The source contract must keep report paths normalized for supported runner hosts.  
**Project type**: Repository-local release scanner.  
**Performance goal**: All coverage producers share the existing `CE_TIMEOUT_SECONDS` budget. Each child receives the remaining monotonic budget.  
**Constraints**: The scanner worktree remains clean before scanner begin. Test environments must not create `.venv`, `__pycache__`, `.coverage`, or `.pytest_cache` outside the runner-owned `.tmp/sonarqube-coverage/<run_id>` root. The runner never records report contents, credentials, or server configuration. A failure after pre-scan cleanup terminates owned producer children before generated-artifact cleanup.
**Scope**: One runner-owned coverage phase with separate Python and .NET report sets. No issue remediation, server mutation, generic report merge, release, or transport work.

## Constitution check

| Principle | Result | Plan response |
| --- | --- | --- |
| Evidence before claims | Pass | Every report is bound to a fresh run marker, a source mapping, a path, size, XML root, and SHA-256 before receipt publication. |
| Reproduce behavior before changing it | Pass | The first implementation ticket writes controlled RED tests for stale reports, command construction, timeout, cleanup, source mapping, and blocked receipt fields before runner code changes. |
| Keep evidence and secrets fail closed | Pass | Report defects block before scanner end. A finally path cleans all artifacts after pre-scan cleanup. Receipts retain metadata only. Child test environments receive no Sonar credentials. |
| Preserve release and review gates | Pass | The feature uses a branch and PR. The existing quality gate remains unchanged. |
| Add dependencies deliberately | Pass | The operator approved `coverage` 7.15.4 and `coverlet.msbuild` 10.0.1 on 2026-08-24. |
| Keep local agent state separate | Pass | Exact receipts and probe evidence remain under `<coordination-root>/.agent/`. Scanner-worktree reports are cleaned after every run. |

The plan has no constitution conflict. The dependency approval is recorded in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/probes.md`; implementation still requires the empirical producer proof and focused closure check.

## Architecture

The [architecture decision](architecture.md) selects a runner-owned phase. The [research record](research.md) fixes the language report formats and documents the external-environment probe.

### Execution order

1. Discover the closed five-project .NET VSTest inventory and derive a `.tmp/sonarqube-coverage` report path for each project.
2. Derive the Python report path and build exact scanner begin arguments: one Python report property and one comma-delimited, normalized .NET path property.
3. Clear prior generated artifacts and arm finally-owned cleanup.
4. Start the existing scanner transaction.
5. Claim the fresh `.tmp` coverage directory and write its canonical marker.
6. Build all maintained projects, then run the documented full Python suite with bytecode/cache output disabled and explicit Cobertura XML generation.
7. Run each member of the closed .NET VSTest inventory with the exact Coverlet OpenCover command.
8. Validate the marker and every report. Check XML roots, source mappings, nonzero denominators, paths, sizes, ordering, and final hashes.
9. Call scanner end only after all evidence sets validate. Bracket the fixed coverage-metric query with matching current-analysis readbacks and bind the result to the submitted analysis.
10. In the finally path, terminate timed-out or cancelled children and clean generated artifacts. Preserve the primary failure and record any cleanup failure separately.

## Project structure

```text
scripts/run_sonarqube_exact_head.py
    exact scanner lifecycle, report path derivation, producer execution,
    report validation, scanner arguments, and receipt binding

tests/test_sonarqube_exact_head_runner.py
    RED and GREEN coverage lifecycle, validation, and receipt tests

pyproject.toml
uv.lock
    approved Python coverage dependency and locked environment definition

host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj
host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj
host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj
host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj
tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj
    approved .NET coverage dependency and closed test inventory

SonarQube.Analysis.xml
    fixed project metadata only. Runtime scanner begin arguments own coverage import paths and reject a conflicting committed coverage property.

docs/SONARQUBE-ONBOARDING.md
docs/RELEASE-PROTOCOL.md
    supported invocation, evidence, and cleanup rules

specs/010-sonarqube-cross-language-coverage-producer/
    feature specification and technical planning artifacts
```

**Structure decision**: The feature extends the existing runner and its test suite. It does not create a coverage service or a second scanner command.

## Requirements-to-file map

| Requirement | Ticket | Files |
| --- | --- | --- |
| FR-001, FR-002, FR-004, FR-007, FR-009 | Coverage phase lifecycle and bounded child ownership | `scripts/run_sonarqube_exact_head.py`, `tests/test_sonarqube_exact_head_runner.py` |
| FR-003, FR-005, FR-006 | Fresh-run validation, receipt binding, and finally cleanup | `scripts/run_sonarqube_exact_head.py`, `tests/test_sonarqube_exact_head_runner.py`, `specs/010-sonarqube-cross-language-coverage-producer/contracts/coverage-evidence.md` |
| FR-008 | Language-specific import contract | `scripts/run_sonarqube_exact_head.py`, `SonarQube.Analysis.xml`, `tests/test_sonarqube_exact_head_runner.py` |
| FR-010 | Analysis coverage metric binding | `scripts/run_sonarqube_exact_head.py`, `tests/test_sonarqube_exact_head_runner.py`, `specs/010-sonarqube-cross-language-coverage-producer/data-model.md` |
| Approved Python producer | Python coverage dependency, external environment, and source configuration | `pyproject.toml`, `uv.lock`, `scripts/run_sonarqube_exact_head.py` |
| Approved .NET producer | Per-test-project coverage dependency and closed inventory | `host/*Tests/*.csproj`, `tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/*.csproj`, `scripts/run_sonarqube_exact_head.py` |
| Operator workflow | Exact-head coverage guidance and integration evidence | `docs/SONARQUBE-ONBOARDING.md`, `docs/RELEASE-PROTOCOL.md` |

## Milestone map

| Milestone | Value statement | Included vertical tickets | Release boundary |
| --- | --- | --- | --- |
| M1: exact coverage evidence | A release maintainer cannot prove coverage on the exact scanned commit because the runner emits no reports. The runner produces, validates, imports, and receipts Python and .NET coverage evidence in one transaction. | Dependency approval, producer probe, runner contract tests, lifecycle implementation, report validation, import arguments, receipt contract, exact-head scan evidence, review. | This milestone may enter a release candidate only after its own candidate and post-merge exact-head gates pass. |
| M2: baseline and violation remediation | A maintainer cannot distinguish current new-code policy from historical code debt. The project has an authorized New Code policy decision and bounded source repair packets. | Separate policy Input ticket, receipt `isNew` evidence extension, issue-bucket repair work. | Separate release map. It does not enter M1. |

## Complexity tracking

| Violation | Why needed | Simpler alternative rejected because |
| --- | --- | --- |
| Two language producers | The quality gate measures both Python and .NET source. | A one-language report leaves the other language without source coverage evidence and cannot establish the full release contract. |
| Per-project .NET reports | Multiple maintained test projects cover distinct assemblies. | A generic merged report loses producer provenance and adds an unnecessary conversion boundary. |

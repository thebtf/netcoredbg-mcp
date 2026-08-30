# Tasks: Cross-Language Sonar Coverage Evidence

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [coverage contract](contracts/coverage-evidence.md), and [quickstart.md](quickstart.md)  
**Prerequisites**: The current exact-head runner and a clean detached worktree.

## Dependency order

```text
T001 -> T002, T003 -> T004, T005, T006 -> T007 -> T008 -> T009 -> T010 -> T011, T012 -> T013 -> T014 -> T015 -> T016 -> T017 -> T018 -> T019 -> T020
```

## Phase 1: Approved dependency setup and producer probes

- [x] T001 Record approval for `coverage` 7.15.4 and `coverlet.msbuild` 10.0.1 in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/probes.md`. TYPE: Input. **Acceptance checkpoint:** the operator approved both pinned dependencies on 2026-08-24.
- [ ] T002 [P] Add `coverage` 7.15.4, `relative_files = true`, `branch = true`, and the `src/netcoredbg_mcp/*` include rule in `pyproject.toml` and `uv.lock`. TYPE: Code. **Acceptance checkpoint:** the locked external environment imports `coverage` without scanner-worktree residue.
- [ ] T003 [P] Add `coverlet.msbuild` 10.0.1 to exactly the five closed-inventory test projects in `host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj`, `host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj`, `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj`, `host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj`, and `tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj`. TYPE: Code. **Acceptance checkpoint:** every closed-inventory project restores with the approved package.
- [ ] T004 [P] Prove `UV_PROJECT_ENVIRONMENT=<coordination-root>/.agent/tmp/sonarqube-coverage-verify` can run `uv sync --locked --extra dev`, `uv run --no-sync python -m coverage run --branch -m pytest -p no:cacheprovider -q`, and `uv run --no-sync python -m coverage xml -o <run-dir>/python/coverage.xml` without scanner-worktree residue in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/python-producer.md`. TYPE: Explore. **Acceptance checkpoint:** the receipt names the command, `coverage` XML root, `lines-valid > 0`, Python source mapping, report path, and clean-worktree result.
- [ ] T005 [P] Prove the exact Coverlet command in `contracts/coverage-evidence.md` produces its explicit `coverage.opencover.xml` filename for every closed-inventory VSTest project in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/dotnet-producer.md`. TYPE: Explore. **Acceptance checkpoint:** the receipt lists VSTest mode, all five project paths, output paths, `CoverageSession` roots, `Summary/@numSequencePoints > 0`, and non-test source mapping.
- [ ] T006 [P] Prove the chosen owned-child execution method terminates a controlled descendant process before cleanup after the remaining coverage deadline expires in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/child-cleanup.md`. TYPE: Explore. **Acceptance checkpoint:** the receipt proves child-tree exit, typed BLOCKED fields, and generated-artifact absence.
- [ ] T007 Aggregate T004 through T006 into `<coordination-root>/.agent/runs/sonarqube-coverage-producer/probes.md`. TYPE: Review. **Acceptance checkpoint:** the summary identifies one command and one report-root contract per language, resolved VSTest status, and no unresolved probe contradiction.

## Phase 2: Coverage contract foundation

- [ ] T008 Add RED runner tests for the closed five-project inventory, VSTest/MTP compatibility guard, deterministic slug collision, canonical marker serialization, scanner-safe `.tmp/sonarqube-coverage` root, pre-seeded stale path, marker mismatch, scanner-begin failure cleanup, exact multi-path scanner argv, explicit Python XML output, cache suppression, timeout, cancellation, source mapping, evidence-set ordering, and typed BLOCKED receipt fields in `tests/test_sonarqube_exact_head_runner.py`. TYPE: Test. **Acceptance checkpoint:** every new case fails against the unchanged runner for the intended missing behavior.
- [ ] T009 Add coverage plan types, closed test-project discovery, VSTest/MTP pre-producer guard, scanner-safe `.tmp` run-directory claim, canonical marker validation, bounded child ownership, source mapping, typed BLOCKED outcome, and finally-owned cleanup in `scripts/run_sonarqube_exact_head.py`. TYPE: Code. **Acceptance checkpoint:** T008 passes after implementation and the runner preserves causal plus cleanup failure evidence.

## Phase 3: User Story 1 — Produce exact-head coverage evidence

**Goal**: Produce and import both language evidence sets inside one scanner transaction.

**Independent test**: A controlled runner scenario records marker-bound Python and .NET evidence sets, source mappings, and exact scanner argv for every path.

- [ ] T010 [US1] Extend `scanner_begin_command` in `scripts/run_sonarqube_exact_head.py` to pass one Python report property and one comma-delimited ordered .NET report property for `.tmp/sonarqube-coverage/<run_id>` as discrete argv elements. TYPE: Code. **Acceptance checkpoint:** T008 proves relative paths, commas, Windows-space-safe argv behavior, and no conflicting coverage property in `SonarQube.Analysis.xml`.
- [ ] T011 [US1] Run the approved Python coverage command with `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, and explicit `coverage xml -o <run-dir>/python/coverage.xml` after scanner build in `scripts/run_sonarqube_exact_head.py`. TYPE: Code. **Acceptance checkpoint:** controlled runner tests prove current-run binding, `lines-valid > 0`, normalized `src/netcoredbg_mcp/` source mapping, and scanner import configuration.
- [ ] T012 [US1] Run the exact per-project VSTest Coverlet OpenCover command with its explicit `.tmp` output filename for the closed .NET inventory after scanner build in `scripts/run_sonarqube_exact_head.py`. TYPE: Code. **Acceptance checkpoint:** controlled runner tests prove the VSTest guard blocks MTP before a producer starts, and every allowed report has `Summary/@numSequencePoints > 0`, maps a non-test source file, and is configured for scanner import.

## Phase 4: User Story 2 — Fail closed on bad report evidence

**Goal**: Reject coverage evidence defects before scanner end or PASS publication, then clean the disposable worktree.

**Independent test**: Every invalid report and producer termination condition emits a typed BLOCKED receipt, does not call scanner end, and leaves no generated scanner artifacts.

- [ ] T013 [US2] Add RED cases for stale `.tmp/sonarqube-coverage` paths, marker mismatch or byte drift, scanner-begin failure, missing, empty, malformed, escaping, symlinked, duplicate, unhashable, unmapped-source reports, producer timeout, cancellation, and cleanup failure in `tests/test_sonarqube_exact_head_runner.py`. TYPE: Test. **Acceptance checkpoint:** every case fails before its production repair.
- [ ] T014 [US2] Enforce report validation, process-tree termination, typed BLOCKED receipt precedence, and finally-owned cleanup in `scripts/run_sonarqube_exact_head.py`. TYPE: Code. **Acceptance checkpoint:** T013 passes; scanner end is not called on defects; primary and cleanup failures retain separate safe fields.

## Phase 5: User Story 3 — Bind coverage provenance in receipts

**Goal**: Let a reviewer verify language-specific report identity and analysis coverage on the exact scanned commit.

**Independent test**: PASS receipt validation rejects missing or inconsistent evidence sets, canonical markers, report identities, source mapping, current-analysis bindings, and coverage metrics.

- [ ] T015 [US3] Add RED receipt-schema cases for evidence-set ordering, canonical marker bytes and digest, project slug mapping, report sort order, canonical source-path-set bytes and digest, XML root, positive denominator, captured-head binding, fixed coverage-metric query, and before-or-after analysis mismatch in `tests/test_sonarqube_exact_head_runner.py`. TYPE: Test. **Acceptance checkpoint:** each forged PASS receipt fails before its production repair.
- [ ] T016 [US3] Add coverage evidence receipt binding, bracketed `/api/measures/component` coverage metric readback, and PASS receipt validation in `scripts/run_sonarqube_exact_head.py`. TYPE: Code. **Acceptance checkpoint:** T015 passes and existing credential-isolation, cleanup, and quality-gate regression checks remain green.

## Phase 6: Exact-head acceptance and documentation

- [ ] T017 Run `uv run --locked --extra dev python -m pytest tests/test_sonarqube_exact_head_runner.py -q -p no:cacheprovider` with external `UV_PROJECT_ENVIRONMENT` and record its nonzero denominator in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/verification.md`. TYPE: Test. **Acceptance checkpoint:** the focused suite passes on the implementation head without `__pycache__` or `.pytest_cache` scanner-worktree residue.
- [ ] T018 Run a disposable candidate exact-head scan and record canonical marker-bound report bindings, bracketed aggregate analysis coverage metrics, and sanitized scanner coverage-sensor evidence when available in `<coordination-root>/.agent/runs/sonarqube-coverage-producer/verification.md`. TYPE: Test. **Acceptance checkpoint:** the scan proves the runner supplied both report sets to the exact candidate. Do not claim a per-language server import from aggregate metrics alone.
- [ ] T019 Update supported coverage commands, VSTest guard, closed inventory, external environment rules, `.tmp/sonarqube-coverage` root, timeout and cleanup behavior, scanner argv contract, marker schema, receipt fields, and exact-head verification in `docs/SONARQUBE-ONBOARDING.md` and `docs/RELEASE-PROTOCOL.md`. TYPE: Code. **Acceptance checkpoint:** the instructions match runner arguments and do not expose credentials.
- [ ] T020 Run one independent fact check of report formats, process-tree cleanup, path safety, source mapping, receipt identity, metric binding, and scanner ordering, followed by one independent code review of the final diff. TYPE: Review. **Acceptance checkpoint:** both verdicts bind the exact candidate head and blocking findings are fixed before release acceptance.

## Implementation strategy

Run the three producer and process probes after approved dependency setup. Apply this frozen correction ledger once. Use a focused closure checker after the proof and correction. Do not reopen a fourth general challenger pass. Do not add baseline policy or the 137-violation remediation work to this feature.

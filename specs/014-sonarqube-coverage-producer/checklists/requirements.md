# Specification quality checklist: exact-head SonarQube coverage producer

**Purpose**: Review this Wave-3 packet before implementation. Marking a checkbox records a document review only. It does not create a diagnostic receipt, acceptance receipt, release, tag, or publication.

## Scope and authority

- [ ] The packet states source base `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`, parent `specs/011-issue450-sonar-release-program/`, Wave 3, D2 authority `agent://ArchitectWave3Coverage`, and `release_intent: none`.
- [ ] The packet uses the required directory `specs/014-sonarqube-coverage-producer/` and documents the older parent pointer without changing it.
- [ ] The packet names `scripts/run_sonarqube_exact_head.py` as the sole scanner, analysis, and receipt authority.
- [ ] `build/coverage.sh` is limited to fully enumerated producer execution and has no scanner token, API, report discovery, validation, or release authority.
- [ ] The packet leaves `SonarQube.Analysis.xml`, `docs/RELEASE-PROTOCOL.md`, `pyproject.toml`, `uv.lock`, public routes, version, tag, publication, project key, threshold, New Code, exclusions, and credential policy out of mutation scope.

## Plan and run layout

- [ ] The runner derives `CoveragePlan` before scanner begin without writes.
- [ ] A fresh UUID root is claimed exclusively only after begin and has a canonical marker bound to head, project key, inventory, versions, and hashes.
- [ ] The layout contains exactly one Cobertura XML and five ordered OpenCover XML files below `.tmp/sonarqube-coverage/<run-id>/`.
- [ ] Scanner begin gets one Python and one ordered .NET runtime report property with slash-relative paths.
- [ ] The packet prohibits static XML coverage properties, report globs, an alternate scanner, report merge, filters, source exclusions, threshold arguments, and `--no-build`.
- [ ] The exact five .NET projects and the Stateless-only `IncludeDirectory` are documented in one stable order.

## Producer and local evidence

- [ ] The Python route uses `uv run --isolated --locked --extra dev --with coverage==7.15.4` and does not make Coverage.py a permanent dependency.
- [ ] `.coveragerc` owns branch mode, relative paths, and `src/netcoredbg_mcp` with no exclusion.
- [ ] Each .NET test project carries direct private `coverlet.msbuild` `10.0.1` only.
- [ ] Python XML rules require positive line and branch denominators and unique tracked source mappings below `src/netcoredbg_mcp`.
- [ ] .NET XML rules require each positive direct sequence denominator, positive aggregate branch denominator, and a valid production `.cs` mapping per report.
- [ ] The Stateless report requires a production host mapping and equal pre/post DLL/PDB hashes.
- [ ] Invalid marker, root, file, XML, denominator, mapping, hash, or head evidence blocks scanner end.

## Server evidence, security, and receipt authority

- [ ] Submitted analysis and two current-analysis bookends must bind to the captured head.
- [ ] Aggregate coverage and lines-to-cover must be positive; the unchanged `new_coverage` condition must be `OK` at threshold `80`.
- [ ] Fully paginated server components must prove positive mapped coverage contributions from both language source sets. Aggregate-only evidence is rejected.
- [ ] Producer and descendants receive no `SONAR_*` names or values. Receipts contain no credential, environment dump, raw report body, or secret-bearing command line.
- [ ] Cleanup removes only the claimed root after foreground producers terminate and retains the first causal failure.
- [ ] The schema allows only diagnostic `DIAGNOSTIC_COMPLETE` or `BLOCKED` outcomes with `release_intent: none`. It cannot model a release pass.
- [ ] Candidate and post-merge PASS validation rejects schema-v2, missing, diagnostic, stale, forged, or incomplete coverage evidence.

## Behavior-first, task, and receipt completeness

- [ ] [tasks.md](../tasks.md#binding-redgreen-matrix) contains exactly 15 behavior-first RED/GREEN rows, R01 through R15.
- [ ] Every row has a current RED oracle, a nonzero future GREEN oracle, a V01 to V15 label, an owner task, and no import-time-only failure.
- [ ] Every COV-001 through COV-023 requirement appears in both `spec.md` traceability and `tasks.md` requirement coverage.
- [ ] The plan has four dependency-ordered slices, S1 through S4, and no fifth implementation slice.
- [ ] T024 requires an independent exact-head source review and T026 requires an independent exact-head acceptance judgment.
- [ ] T028 is the first task allowed to run the diagnostic role or create either a diagnostic or Wave-3 acceptance receipt.
- [ ] T028 leaves global blockers explicit and retains `release_intent: none`; it does not authorize release.

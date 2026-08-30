# Research: Cross-language coverage evidence

## Decision: keep coverage production inside the exact-head runner

The runner owns coverage production inside its existing serialized scanner transaction. It derives expected report paths before scanner begin, starts cleanup before scanner begin, builds, produces reports, validates and fingerprints reports, calls scanner end, binds analysis coverage metrics, and then publishes the receipt.

This keeps the report producer, report paths, scanner upload, and captured HEAD in one scanner worktree and one lock. The runner writes the durable secret-free receipt under `<coordination-root>/.agent/`, where every worktree can read it. The runner does not accept an artifact from another job.

### Alternatives considered

| Alternative | Decision | Reason |
| --- | --- | --- |
| A separate CI artifact produced before the runner | Rejected | It adds an artifact transfer contract for source SHA, retention, path safety, and hashes. It does not help the local release command. |
| A separate coverage orchestration command | Rejected | It duplicates the scanner lock, worktree checks, credential scrubbing, cleanup, and receipt rules. |
| One runner-owned phase for Python and .NET | Chosen | The existing runner already owns build, scanner lifecycle, cleanup, and receipt publication. |

## Decision: use a scanner-safe transient report root

SonarScanner for .NET clears `<worktree>/.sonarqube` during scanner begin. Coverage reports cannot live there before begin. The runner uses this ignored root instead:

```text
.tmp/sonarqube-coverage/<run_id>/
```

The runner derives paths before begin, then claims the fresh report directory and writes the marker after begin succeeds. The runner adds this exact child of the already ignored `.tmp` root to generated-artifact cleanup.

Sources:

- [SonarScanner workspace initialization](https://github.com/SonarSource/sonar-scanner-msbuild/blob/master/src/SonarScanner.MSBuild/BootstrapperClass.cs)
- [SonarScanner default `.sonarqube` path](https://github.com/SonarSource/sonar-scanner-msbuild/blob/master/src/SonarScanner.MSBuild/BootstrapperSettings.cs)

## Decision: use native report formats and deterministic scanner arguments

| Language | Producer | Report format | Scanner argument | Report path |
| --- | --- | --- | --- | --- |
| Python | `coverage run --branch -m pytest -p no:cacheprovider -q`, then `coverage xml` | Cobertura XML | `/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/<run_id>/python/coverage.xml` | `.tmp/sonarqube-coverage/<run_id>/python/coverage.xml` |
| .NET | `coverlet.msbuild` for each closed-inventory VSTest project | OpenCover XML | One `/d:sonar.cs.opencover.reportsPaths=<comma-delimited-relative-paths>` argv item | `.tmp/sonarqube-coverage/<run_id>/dotnet/<slug>/coverage.opencover.xml` |

The runner passes each property as one `subprocess.run` argv element. Relative paths use forward slashes. The .NET property joins normalized paths with commas in normalized-path order. `SonarQube.Analysis.xml` contains no coverage property, and the runner rejects a conflicting committed property.

For each closed-inventory .NET project, the runner invokes:

```text
dotnet test <project> --no-build --no-restore -nr:false /p:CollectCoverage=true /p:CoverletOutputFormat=opencover /p:CoverletOutput=<run-dir>/dotnet/<slug>/coverage.opencover.xml
```

The report denominator is direct-root `CoverageSession/Summary/@numSequencePoints`, parsed as an integer greater than zero. `slug` is the collision-checked deterministic value in [data-model.md](data-model.md#canonical-run-marker).

The runner sets `PYTHONDONTWRITEBYTECODE=1`, `COVERAGE_FILE=<run-dir>/python/.coverage`, and `-p no:cacheprovider` for Python producer children. These controls prevent `__pycache__`, `.coverage`, and `.pytest_cache` from making the strict post-scan cleanliness check fail.

Sources:

- [SonarQube .NET coverage](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/dotnet-test-coverage/)
- [SonarQube test coverage parameters](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/test-coverage-parameters/)
- [SonarQube Python coverage](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/python-test-coverage/)
- [Coverlet 10.0.1 MSBuild integration](https://github.com/coverlet-coverage/coverlet/blob/v10.0.1/Documentation/MSBuildIntegration.md)
- [Coverlet 10.0.1 driver compatibility](https://github.com/coverlet-coverage/coverlet/blob/v10.0.1/README.md)
- [Coverage.py XML reporting](https://github.com/coveragepy/coveragepy/blob/main/doc/commands/cmd_xml.rst)

## Dependency and test-platform decision

Implementation needs two approved development dependencies:

- `coverage` 7.15.4
- `coverlet.msbuild` 10.0.1

The operator approved both on 2026-08-24. `coverlet.msbuild` requires VSTest and is incompatible with Microsoft Testing Platform. Before every .NET producer, the runner must verify that the project is not MTP-enabled. The probe records the resolved VSTest mode. A discovered MTP project blocks with a safe coverage failure rather than silently switching drivers.

## Worktree environment and Python workload decision

The exact scanner worktree must remain clean before scanner begin. A direct Python runtime in the current workstation has neither `pytest` nor `coverage`. The coverage phase uses `UV_PROJECT_ENVIRONMENT` below `<coordination-root>/.agent/tmp/`, runs `uv sync --locked --extra dev`, and then runs `uv run --no-sync` children with Sonar variables scrubbed.

The authoritative Python workload is the documented full suite: `pytest -q`. Coverage.py uses `relative_files = true`, `branch = true`, and an include pattern for `src/netcoredbg_mcp/*`. A valid Cobertura report has root `coverage`, `lines-valid > 0`, and at least one normalized relative `.py` mapping below `src/netcoredbg_mcp/`.

## Report provenance, validation, timeout, and cleanup contract

The runner arms generated-artifact cleanup after pre-scan cleanup. After scanner begin succeeds, it creates the run directory with `exist_ok=False` and writes the canonical marker defined in [data-model.md](data-model.md#canonical-run-marker). A pre-seeded path or marker mismatch blocks the run.

Before scanner end, the runner validates each report.

1. Require a regular nonempty file below the claimed run directory.
2. Reject symlinks, escaping paths, malformed XML, duplicate normalized paths, and report sets that differ from the closed language inventory.
3. Require Python mappings below `src/netcoredbg_mcp/`. Require each .NET report to contain at least one resolved non-test, non-fixture `.cs` path inside the worktree.
4. Hash each report, source-path set, and canonical marker bytes with SHA-256. Record only the data-model fields.
5. Give all coverage producers one shared monotonic deadline equal to existing `CE_TIMEOUT_SECONDS`. Pass each child the remaining budget. On timeout or cancellation, terminate the owned process tree, wait for termination, and retain the safe primary failure.
6. In `finally`, clean generated artifacts after every scanner, producer, validation, API, quality-gate, or cleanup outcome. A cleanup failure remains under `coverage.cleanup.failure` and never replaces the causal failure.

After scanner end, the runner brackets the fixed coverage metric query with matching current-analysis bindings. It retains the receipt report bindings and `analysis_coverage` metric binding. It does not retain raw scanner output lines.

## Closed .NET inventory

1. `host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj`
2. `host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj`
3. `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj`
4. `host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj`
5. `tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj`

The runner tests assert this exact normalized ordered list. Fixture projects and production projects do not belong in the coverage test inventory.

## Required implementation proofs

| Question | Required proof before code is accepted |
| --- | --- |
| Does `coverlet.msbuild` 10.0.1 produce the explicit OpenCover filename for every closed-inventory VSTest project on this Windows .NET 10 SDK? | Run every listed project. Record VSTest mode, output path, `CoverageSession` root, `Summary/@numSequencePoints`, and source mapping. |
| Can the runner invoke the locked Python coverage command and XML generation without scanner-worktree residue? | Run the full pytest workload with external `UV_PROJECT_ENVIRONMENT`, bytecode disabled, and pytest cache disabled. Verify `lines-valid > 0`, Python source mapping, and clean worktree after cleanup. |
| Does timeout handling terminate descendants before cleanup? | Use a controlled producer that outlives the remaining deadline. Verify child-tree exit, BLOCKED receipt fields, and generated-artifact absence. |
| Does scanner end accept every configured report? | Run a disposable exact-head scan. Retain marker-bound report bindings, the bracketed aggregate analysis coverage metric binding, and sanitized scanner coverage-sensor evidence when the scanner emits it. Aggregate metrics alone do not prove per-language report import. |

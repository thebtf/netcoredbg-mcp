# Coverage evidence contract

## Caller

The only caller is `scripts/run_sonarqube_exact_head.py` inside a clean detached scanner worktree. The caller supplies the existing role, scanner override, captured Git context, and scrubbed child environment.

## Inputs

| Input | Requirement |
| --- | --- |
| Captured Git context | The worktree is detached, clean, and bound to one 40-character HEAD. |
| Run identifier | The runner derives the UUID path before scanner begin, arms cleanup, then claims `.tmp/sonarqube-coverage/<run_id>` with exclusive creation after scanner begin. It writes the canonical marker from [data-model.md](../data-model.md#canonical-run-marker). |
| Python test command | With `UV_PROJECT_ENVIRONMENT` outside the worktree, the runner invokes `uv sync --locked --extra dev`, `uv run --no-sync python -m coverage run --branch -m pytest -p no:cacheprovider -q`, then `uv run --no-sync python -m coverage xml -o <run-dir>/python/coverage.xml`. It sets `PYTHONDONTWRITEBYTECODE=1` and `COVERAGE_FILE=<run-dir>/python/.coverage`. Each child receives no `SONAR_*` variables. |
| .NET test inventory | Exactly five paths listed in [research.md](../research.md#closed-net-inventory). Fixture-only and production projects are excluded. |
| .NET test command | For each inventory project, the runner first requires VSTest mode. It invokes `dotnet test <project> --no-build --no-restore -nr:false /p:CollectCoverage=true /p:CoverletOutputFormat=opencover /p:CoverletOutput=<run-dir>/dotnet/<slug>/coverage.opencover.xml`. The required output is that exact filename. |
| Report paths | The runner derives every path below the fresh `.tmp` run directory before scanner begin. The canonical marker lists the run identifier, captured HEAD, language sets, projects, slugs, and expected paths after begin succeeds. |
| Scanner import arguments | The runner passes `/d:sonar.python.coverage.reportPaths=<relative-python-path>` and `/d:sonar.cs.opencover.reportsPaths=<comma-delimited-relative-dotnet-paths>` as discrete argv elements. |

## Outputs

| Outcome | Required behavior |
| --- | --- |
| Coverage phase succeeds | Return Python and .NET evidence sets. Pass every configured expected report location to scanner begin. Bind the canonical marker, report identities, source mappings, and analysis coverage binding before PASS validation. |
| Producer fails, times out, or is cancelled | Terminate the owned process tree, clean every generated artifact, and write the typed BLOCKED coverage outcome from [data-model.md](../data-model.md#blocked-coverage-outcome). Do not call scanner end. |
| Scanner begin fails | Clean generated scanner artifacts and write a typed BLOCKED coverage outcome. Preserve the begin failure as causal. |
| Report validation fails | Clean generated artifacts and write a typed BLOCKED coverage outcome with the affected language. Do not call scanner end. |
| Cleanup also fails | Preserve the first causal failure. Record typed cleanup details under `coverage.cleanup.failure`. Do not publish PASS. |
| Scanner end succeeds | Query and bind analysis coverage metrics using the before-and-after current-analysis contract in [data-model.md](../data-model.md#analysis-coverage-binding), then validate PASS. |

## Invariants

- The runner accepts only reports below a fresh `.tmp` UUID run directory created by the current transaction after scanner begin.
- The canonical marker must match the current `run_id`, captured HEAD, expected language sets, closed project inventory, deterministic slugs, and expected report paths.
- Python reports must map normalized relative source files below `src/netcoredbg_mcp/`. .NET reports must map at least one non-test, non-fixture `.cs` file inside the worktree.
- The runner validates report files before scanner end.
- The runner passes the full deterministic report lists in scanner begin argv. `SonarQube.Analysis.xml` does not own coverage paths.
- The runner gives every coverage producer the remaining shared deadline budget and waits for owned children to exit before cleanup.
- A finally path cleans generated artifacts after every outcome once pre-scan cleanup finishes, including scanner-begin failure.
- The runner binds exact analysis coverage metrics through bracketing current-analysis readbacks.
- The runner records report and marker identities but not report bodies.
- The runner keeps Python and .NET report sets separate.
- The runner does not change credentials, transport policy, quality-gate policy, New Code policy, or issue state.

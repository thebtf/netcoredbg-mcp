# Research: exact-head SonarQube coverage producer

**Status**: Planning research. It contains observed inputs and selected design conclusions. It does not claim a producer run, a diagnostic result, a green Quality Gate, or a release.
**Packet source base**: `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
**Authority**: `agent://ArchitectWave3Coverage`
**Primary local evidence**: `agent://Wave3CoverageSource`, `agent://Wave3CoverageTests`
**Parent evidence**: `specs/011-issue450-sonar-release-program/`, `.agent/runs/issue450-sonar-release-program/program-map.md`

## Evidence labels

- **OBSERVED** means the cited agent packet or repository artifact reports a read fact.
- **SELECTED** means the D2 authority chose the implementation shape after evaluating the observed facts.
- **INFERRED** means a future implementation must prove the stated property on its exact head.
- **UNKNOWN** means no current packet proves the fact. It is never treated as a pass.

## Observed source and test facts

| Classification | Observation | Evidence | Consequence |
| --- | --- | --- | --- |
| OBSERVED | `scripts/run_sonarqube_exact_head.py` is the only scanner caller. Its current sequence is cleanup, begin, build, end, Compute Engine and finding readback, then cleanup and receipt. | `agent://Wave3CoverageSource` | Extend the retained runner. Do not add a second scanner command. |
| OBSERVED | The runner has no Python or .NET coverage producer, report parser, branch check, source mapping check, report freshness binding, coverage metric query, or coverage receipt section. | `agent://Wave3CoverageSource`; `agent://Wave3CoverageTests` | The missing behavior needs caller-facing RED tests before implementation. |
| OBSERVED | Scanner begin currently has no coverage-report arguments. | `agent://Wave3CoverageSource` | Runtime begin arguments must carry the exact plan paths. |
| OBSERVED | `SonarQube.Analysis.xml` has project identity, `scanAll`, Python version, and existing exclusions but no coverage property. | `agent://Wave3CoverageSource` | Keep XML unchanged and reject a future static coverage property. |
| OBSERVED | `.coveragerc`, `build/coverage.sh`, `.config/dotnet-tools.json`, a Coverage.py dependency, and a Coverage.py configuration are absent from the inspected source. | `agent://Wave3CoverageSource`; `agent://Wave3CoverageTests` | Add only the selected shell/config files. Do not invent a global tool or persistent Python dependency. |
| OBSERVED | The Python package root is `src/netcoredbg_mcp`; tests run under `tests/`. | `agent://Wave3CoverageTests` | Python mappings must resolve only to tracked `.py` files under `src/netcoredbg_mcp`. |
| OBSERVED | Five closed .NET test projects have no direct `coverlet.msbuild` reference. | `agent://Wave3CoverageTests` | Add exact test-only `coverlet.msbuild` `10.0.1` references to only those five projects. |
| OBSERVED | The broader build inventory is not the preserved fixed coverage inventory. | `agent://Wave3CoverageSource`; `agent://Wave3CoverageTests` | Represent the five coverage projects as a closed ordered list. |
| OBSERVED | Stateless tests launch the Stateless host separately. Existing evidence needs an absolute `IncludeDirectory` for its host output. | `agent://Wave3CoverageSource` | Only the Stateless producer gets the `IncludeDirectory` argument and host DLL/PDB restoration proof. |
| OBSERVED | A fresh solution build followed by a zero-exit Coverlet `--no-build --no-restore` command can leave the expected report absent. | `.agent/runs/v02311-dotnet-no-build-report-missing/investigation.md`, summarized by `agent://Wave3CoverageSource` | No producer command may use `--no-build`. Report existence remains the final oracle. |
| OBSERVED | Existing runner tests use `unittest`, temporary directories, `ExitStack`, patches, fake API responses, Git context fixtures, and command/event capture. | `agent://Wave3CoverageTests` | Keep focused coverage tests in `tests/test_sonarqube_exact_head_runner.py` with local fixture builders. |
| OBSERVED | The retained exact-head baseline is valid but blocked. It records `new_coverage=0.0` against threshold `80`, `new_violations=172`, 1,076 blocking issue dispositions, and zero hotspots. | `agent://Wave3CoverageSource` | The unchanged global authority is red. No coverage design may waive, lower, or reinterpret it. |
| OBSERVED | The parent program assigns Wave 3 `release_intent: none` and requires a diagnostic receipt before Wave 4 can create a fresh remediation manifest. | `specs/011-issue450-sonar-release-program/spec.md`; `program-map.md` | A diagnostic closure is internal evidence, not a tag or public release. |

## Primary-source constraints carried by the D2 authority

The selected authority lists the following primary-source observations. This packet carries them as implementation constraints. A future implementation must revalidate version-specific behavior at its own exact head.

| Source family | Observed constraint | Selected use |
| --- | --- | --- |
| `uv run` documentation | `--isolated`, `--locked`, `--extra`, and `--with` support a locked external tool environment. | Run Coverage.py as `coverage==7.15.4` without editing `pyproject.toml`, `uv.lock`, or `.venv`. |
| Coverage.py configuration and XML documentation | Branch coverage, relative file mapping, `source`, and Cobertura XML fields can expose line and branch denominators. | `.coveragerc` owns branch mode, relative paths, and `src/netcoredbg_mcp`. |
| SonarQube Python coverage documentation | Python Cobertura import accepts a report path supplied during analysis. | Pass the exact runtime Python report property during scanner begin. |
| SonarQube .NET coverage documentation | OpenCover is an accepted .NET coverage format, and coverage reports must exist between scanner begin and end. | Pass the ordered OpenCover argument during begin and validate reports before end. |
| SonarQube metrics documentation | Coverage and new coverage are analysis metrics, not local test counts. | Bind measures to submitted/current analysis identity and retain the unchanged `new_coverage` condition. |
| Coverlet MSBuild integration documentation | VSTest MSBuild integration uses a direct package reference and accepts OpenCover output configuration. | Use direct private `coverlet.msbuild` `10.0.1` references in only the closed test inventory. |

The exact URLs and the observation date are recorded in `agent://ArchitectWave3Coverage`. This packet does not treat a documentation citation as a completed implementation test.

## Selected decisions

### Keep the runner as the one transaction owner

**SELECTED**: `scripts/run_sonarqube_exact_head.py` owns plan derivation, scanner arguments, claim, invocation, validation, scanner end, analysis binding, receipt construction, and cleanup.

**Reason**: The runner already owns exact worktree/head validation, secret scrubbing, scanner invocation, Compute Engine processing, current-analysis binding, finding paging, and receipt publication. A second scanner command would split the authority that determines whether a report is evidence.

### Keep run-specific paths out of static XML

**SELECTED**: `SonarQube.Analysis.xml` stays unchanged. The runner supplies two exact report properties during scanner begin.

**Reason**: The run ID makes each report path unique. A fixed static directory or wildcard could import stale or extra reports and would compete with the runner as a path authority.

### Keep Python coverage external and .NET integration direct

**SELECTED**: The runner invokes `uv run --isolated --locked --extra dev --with coverage==7.15.4` and the five test projects receive direct private `coverlet.msbuild` `10.0.1` references.

**Reason**: The Python route avoids a permanent application dependency and preserves lockfile bytes. The .NET route uses the documented VSTest integration instead of an ephemeral MSBuild injection.

### Reject the no-build command form

**SELECTED**: Each producer restores its exact test project and runs `dotnet test --no-restore` without `--no-build`.

**Reason**: The later fresh-worktree observation has stronger safety value than a prior success. A zero process exit without the planned report is a failed producer, not a successful shortcut.

### Treat aggregate coverage as necessary but insufficient

**SELECTED**: The runner queries aggregate project measures and fully pages file components. It intersects the server components with the validated Python and .NET source sets.

**Reason**: A project-level coverage number can be positive while one language report was not consumed. Per-language mapped components prevent that false conclusion.

### Keep diagnostic evidence non-authoritative for release

**SELECTED**: A diagnostic receipt uses schema version 3 with `release_intent: none` and outcomes `DIAGNOSTIC_COMPLETE` or `BLOCKED`. Candidate and post-merge PASS validation later requires v3 coverage evidence and has no v2 compatibility path.

**Reason**: Wave 3 supplies a denominator for Wave 4. It does not clear unrelated findings, weaken the gate, or authorize a release.

## Rejected and deferred designs

| Design | Disposition | Reason |
| --- | --- | --- |
| Use `build/coverage.sh` as a new public or release command | Rejected | The existing runner remains the only scan authority. |
| Store report paths in static XML or accept a report wildcard | Rejected | It permits stale or extra artifact reuse. |
| Merge Python and .NET reports | Rejected | It obscures language-specific validation and import proof. |
| Add Coverage.py to `pyproject.toml` or update `uv.lock` | Rejected | The selected isolated `uv` route needs neither persistent source edit. |
| Inject temporary Coverlet targets or properties | Rejected | Direct test-only package references are documented and deterministic. |
| Use the broad build inventory as coverage inventory | Rejected | It includes non-producer projects and cannot prove the closed test set. |
| Use filters, exclusions, a threshold property, or a report merge | Rejected | They alter the intended denominator or hide a producer defect. |
| Use `taskkill`, image name matching, or a new process owner to terminate a coverage child | Rejected | This design adds no timeout/tree-termination guarantee. Foreground commands must return before cleanup. |
| Switch to Microsoft Testing Platform or Coverlet collector now | Deferred | The current proof is VSTest-based. An MTP migration needs separate research. |

## Scope reconciliation

The parent packet names a generic Wave-3 coverage boundary and an earlier path spelling. The current task fixes this packet path and supplies the D2 decision. The implementation scope below is the selected child cut:

| Surface | Disposition | Reason |
| --- | --- | --- |
| `build/coverage.sh` | Add | Thin enumerated producer executor only. |
| `.coveragerc` | Add | Own branch, relative path, and Python source policy. |
| Five listed test `.csproj` files | Change | Exact private `coverlet.msbuild` references only. |
| `scripts/run_sonarqube_exact_head.py` | Change | Plan, runtime arguments, claim, producer invocation, validation, measures, diagnostic evidence, pass enforcement, and cleanup. |
| `tests/test_sonarqube_exact_head_runner.py` | Change | Behavior-first RED/GREEN contract tests with local fixture builders. |
| `pyproject.toml`, `uv.lock` | Unchanged | The external `uv --with` route owns Coverage.py. |
| `SonarQube.Analysis.xml`, `docs/RELEASE-PROTOCOL.md` | Unchanged | Runtime report properties and existing release commands remain the authority. |
| Runtime product code and public routes | Unchanged | The coverage transaction is release infrastructure only. |

## Unknowns and future proof

| Unknown | Required proof or next action |
| --- | --- |
| Live Sonar component-tree fields and pagination behavior | The exact diagnostic run must prove both-language intersections. Aggregate measures are not a substitute. |
| VSTest availability on the implementation head | The runner preflight and report-existence contract must reject incompatible MTP opt-ins. |
| Bash path handling on the Windows scanner host | The runner preflights `bash`; commands use argument vectors and absolute producer paths. No fallback script is planned. |
| Full-coverlet behavior of `NetCoreDbg.Mcp.Host.Tests` | The closed five-project run must execute without filters. A failure stays inside this packet's producer boundary. |
| Future bounded cancellation | A later design must use a proven owner capability. It must not add image-name or taskkill cleanup. |
| Stronger server import provenance | A future API may provide per-report import metadata. Until then, exact handoff plus source-set component intersections are required. |

## Research conclusion

The runner-owned transaction is the smallest selected change that makes stale, missing, zero-denominator, wrong-source, and one-language-only coverage unable to support scanner end or a later PASS. It retains the existing scan and release authorities, preserves the unchanged strict policy, and creates the fresh diagnostic denominator that Wave 4 needs.

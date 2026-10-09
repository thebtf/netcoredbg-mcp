# SonarQube Exact-Head Onboarding

This repository uses the fixed SonarQube project key
`thebtf_netcoredbg_mcp`. The tracked runner is
`scripts/run_sonarqube_exact_head.py`; it is the required release-scan command.
It writes only secret-free receipts and redacted logs.

## Mixed-language test ownership

`SonarQube.Analysis.xml` retains
`sonar.test.inclusions=tests/**,host/**/*.Tests/**` for both Python and native
test eligibility. Inclusion patterns are not a classifier: SonarQube also
applies them as
[source exclusions](https://docs.sonarsource.com/sonarqube-server/2026.1/project-administration/adjusting-analysis/setting-analysis-scope/excluding-files-based-on-patterns.md).
The completed `dcbdcab14e6f2fe36c1095e9f4b000b2e4e2ff4d` analysis retained
60 native `UTS` files but indexed no Python tests; the selected
`tests/test_windows_process_owner.py` component was absent. That inclusion-only
configuration did not restore Python test analysis.

The installed scanner 11.2.1.137242 identifies upstream commit
`57e91fb2a8ccbf247c3999311336bb02692bb7ab`. Its
[AdditionalFilesService](https://github.com/SonarSource/sonar-scanner-msbuild/blob/57e91fb2a8ccbf247c3999311336bb02692bb7ab/src/SonarScanner.MSBuild.Shim/AdditionalFilesService.cs)
puts auto-discovered Python files in `Sources`; its additional-file test-name
recognition covers JavaScript and TypeScript, not Python. Its
[ScannerEngineInputGenerator](https://github.com/SonarSource/sonar-scanner-msbuild/blob/57e91fb2a8ccbf247c3999311336bb02692bb7ab/src/SonarScanner.MSBuild.Shim/ScannerEngineInputGenerator.cs)
assigns files to the closest physically containing project directory. Files
without such an owner become root sources. Consequently, external `Content`
or `None` links from a nested `host/*.Tests/` project cannot give `tests/*.py`
TEST ownership. Native project categorization remains
[project-based](https://docs.sonarsource.com/sonarqube-server/2026.1/analyzing-source-code/dotnet-environments/specify-test-project-analysis.md).

`tests/SonarQube.PythonTests.Analysis.csproj` is a standard SDK analysis-only
project physically above the Python tests, with `SonarQubeTestProject=true`.
It associates real non-fixture Python files as `None` items, disables default
items (including recursive C# compilation and implicit `None` discovery), and
forbids output/publish copies, packing, and publishing. It adds no test SDK,
test framework, executable test code, or coverage producer. The existing
runner discovers and builds maintained projects omitted from the solution,
so no solution or runner change is needed. All five coverage producer IDs
and project paths remain unchanged; this project does not execute Python tests
or provide a coverage report. Existing nearer native test projects retain
their ownership. With `scanAll`, other supported files physically beneath
`tests/` can also receive this TEST owner, not just its explicit Python items.

Source/configuration preparation is not effective-scope proof. Before accepting
this correction, evaluate the actual project without running build targets:

```powershell
dotnet msbuild tests/SonarQube.PythonTests.Analysis.csproj -nologo -verbosity:quiet -nr:false -getProperty:TargetFramework,EnableDefaultItems,SonarQubeTestProject,IsTestProject,IsPackable,IsPublishable -getItem:Compile,None,Content,EmbeddedResource,PackageReference,ProjectReference
```

Require 146 physical non-fixture Python `None` items, no default C# inputs or
package/project references, and `Never` copy metadata. Then prove ownership
with the installed 11.2.1 generator and run a fresh full, unfiltered exact-head
diagnostic: all 146 Python paths, including `tests/test_windows_process_owner.py`,
must be `UTS`; all 60 native test identities and the mapped 130 Python plus
69 .NET product source identity hashes must remain unchanged. SDK evaluation,
generator inspection, and the new full scan are **NOT_RUN** at preparation.

This correction changes classification, not coverage inputs or release policy.
Existing fixture exclusions, quality profiles, the new-code period, strict 80%
new-code coverage, and zero OPEN findings remain unchanged. Proven indexing
alone does not satisfy the quality gate, predict a coverage improvement, or
attribute every denominator or findings change to this ownership correction.

## One-time local onboarding

Install the supported SonarScanner for .NET on `PATH`:

```powershell
dotnet tool install --global dotnet-sonarscanner
```

If the tool is already installed, use `dotnet tool update --global
dotnet-sonarscanner`. The runner discovers `dotnet-sonarscanner`,
`SonarScanner.MSBuild.exe`, or `SonarScanner.MSBuild`; an exceptional local
path can be supplied as a single executable with `--scanner <path>`.

The maintainer performing this one-time onboarding creates two **project-scoped**
SonarQube tokens for `thebtf_netcoredbg_mcp`: an analysis token with Execute
Analysis access and a separate non-admin Browse token. The maintainer writes
them, together with a declared credential-free HTTP(S) SonarQube origin, to the
primary repository-root `.env`. `SONAR_HOST_URL` is authoritative: the runner
accepts a pathless origin or a root `/` suffix and canonicalizes both to
`scheme://netloc`; it does not upgrade or rewrite the configured scheme or
authority. This is the durable local runtime source for the runner.

The runner derives that root instead of trusting its current working directory:

```text
coordination-root = parent(git rev-parse --git-common-dir)
dotenv             = <coordination-root>/.env
```

For a linked scanner worktree, this remains the primary repository root, not
the linked worktree. The runner loads no other dotenv file.

`<coordination-root>/.env` may contain exactly these three keys:

```text
SONAR_HOST_URL=https://sonarqube.example.invalid
SONAR_TOKEN=<project-analysis-token>
SONAR_READ_TOKEN=<project-browse-token>
```

The file is local-only: `.gitignore` must ignore `.env`, and no receipt or
log may contain its values. The runner reads the validated file object. On
Windows, it rejects a reparse point, a non-owner SID, a missing or unprotected
DACL, and any allow ACE for another SID. On other platforms, it requires the
current user to own a regular file with no group or other permission bits. Do
not place it in a linked scanner worktree. The runner rejects a scanner
worktree that contains `.env`, a symbolic link, or any reparse point, including
the root and an ignored `.env` link.

An explicitly supplied process environment value for any of the three allowed
keys overrides that key's value from `<coordination-root>/.env`. The key name
must use the exact canonical casing. This is the only supported temporary
override. The runner rejects `SONAR_ADMIN_TOKEN` in either source and rejects
every other or mis-cased `SONAR_` credential name. Administrative credentials
are outside this repository and never participate in its scripts or release
workflow.

`SONAR_TOKEN` is the project analysis credential. SonarScanner for .NET
requires it as `/d:sonar.token` on the scanner's `begin` and `end` child-process
arguments. The runner redacts the configured origin and both tokens from every
displayed command and captured output, but a same-host process observer can see
a live scanner process's argv. Run scans only on a trusted local account.
`SONAR_READ_TOKEN` is used only for the analysis-bound quality gate,
current-analysis bookends, issue inventory, and hotspot inventory.

The runner removes every case variant of `SONAR_*` from build and test child
environments. It supplies the analysis token only to the scanner `begin` and
`end` processes. It never writes a token, configured origin, or dashboard URL
to Git, a tracked file, a receipt, or an unredacted log.

## Release scans

Run both roles from a new clean detached linked worktree at the role's exact SHA.
That worktree must not contain `.env`; the runner obtains credentials only from
the coordination-root `.env` and explicit process overrides described above.
The runner checks clean status before and after scanning, uses the committed
`SonarQube.Analysis.xml`, sets `sonar.scm.revision` to the captured
40-character HEAD, builds the solution plus every maintained `.csproj` omitted
by it, and requires the submitted CE task, current-analysis bookends, observed
scanner/task metadata, analysis-bound quality gate, full issue disposition
inventory, and full hotspot inventory to match that SHA.
The runner appends `-nr:false` to every runner-owned `dotnet build` command so
MSBuild worker nodes cannot retain scanner-artifact handles after a build. Its
post-scan cleanup records a receipt-safe `cleanup` outcome: `PASS` includes
the deterministic repository-relative removal list; an `OSError` is `BLOCKED`
with only the failed relative path, operation, and error type. A cleanup block
retains the already-collected gate and finding diagnostics but cannot publish
post-cleanliness, final-analysis binding, or a passing receipt.

Claimed coverage scratch deletion on Windows pins verified namespace ancestors
and the claim before marker validation, enumerates retained directory handles, opens children relative to those handles, and
removes final files/directories through those same handles. Read-only files use
`FileDispositionInfoEx` with `IGNORE_READONLY_ATTRIBUTE`, not shared-attribute
mutation or pathname retries. Unsupported kernels/filesystems block that cleanup
effect with no fallback. Hardlink counts are observations, not alias exclusion;
even an alias created after the last observation retains its bytes and attributes.
An interrupted cleanup persists `cleanup.status: FAILED` and reached evidence in
a `BLOCKED` receipt before re-raising the identical interruption, without retry.

Private scanner POSIX claim cleanup is unavailable: descriptor-relative stdlib
operations cannot bind final `unlink`/`rmdir` to the verified object or exclude
namespace replacement. It fails closed before deleting anything. Re-entry
requires an implementation and proof retaining verified namespace/final-object
ownership through deletion; this limitation does not change product runtime
POSIX support or Windows release eligibility.

After aggregate/component reads, an actual current-analysis query must succeed
before `current_after_measures` becomes true. Until the real final query succeeds,
`BLOCKED` evidence uses `analysis.status: INCOMPLETE`, with unobserved bookends
explicitly null. Numeric/component facts remain available without implying a
completed binding. `DIAGNOSTIC_COMPLETE`/`PASS` still require all four slots true.


Because SonarQube's analysis item has no project field, project proof is the
recorded `project=thebtf_netcoredbg_mcp` analysis query together with the
scanner-submitted CE task's required `componentKey` equal to that key, correlated
to the same `analysisId`/analysis `key` and exact revision. The issue inventory
enumerates the live `OPEN`, `CONFIRMED`, `FALSE_POSITIVE`, `ACCEPTED`, `FIXED`,
and `IN_SANDBOX` states and records resolutions such as `WONTFIX` and
`FALSE-POSITIVE`; all non-`FIXED` dispositions block release.

The live issue search uses `components=thebtf_netcoredbg_mcp`; it does not use
the legacy `componentKeys` filter.

After the final pre-merge correction, create the candidate scanner worktree at
`CANDIDATE_SHA` and run:

```powershell
python scripts/run_sonarqube_exact_head.py --role candidate
```

After merge, fetch `origin/main`, create a new clean detached scanner worktree
at its exact commit, and run:

```powershell
python scripts/run_sonarqube_exact_head.py --role post-merge
```

The `post-merge` role additionally refuses unless `HEAD == origin/main`. A
candidate receipt never authorizes a tag. Only a passing post-merge receipt
allows the tag gate to proceed.

The runner serializes local scans for this project, requires a clean detached
linked worktree before and after scanning, polls only the scanner-submitted CE
task for at most 10 minutes, and queries the gate with that task's analysis ID.
It fails closed unless the gate status is `OK`; `WARN`, `ERROR`, `NONE`, API
denial, a head/revision/current-analysis mismatch, unexpected ignored state,
incomplete issue/hotspot paging, prohibited issue disposition, or any hotspot
blocks the release. The all-hotspot block is this runner's conservative release
policy; SonarQube's native REVIEWED outcomes remain recorded as facts.

The runner validates every reported gate condition. A condition needs a
nonempty `metricKey`, an `OK`, `WARN`, `ERROR`, or `NONE` status, and a `GT`,
`LT`, `EQ`, or `NE` comparator. If a warning threshold, error threshold, or
actual value is present, it must be a string. An empty condition list is valid,
but only a top-level `OK` status passes.

The current `/api/hotspots/search` compatibility endpoint is deprecated by
SonarQube. The runner retains its complete evidence while also classifying all
normal issue types, including security and vulnerability issues, through
`/api/issues/search`. The live 26.8 hotspot schema requires
`project=thebtf_netcoredbg_mcp`; an empty response to `projectKey` is not evidence
that that legacy-looking parameter scoped the request. Endpoint removal or an
inaccessible endpoint is incomplete evidence and blocks the release.

Receipts are atomically written beneath the Git common directory's parent so
all worktrees share one evidence root:

```text
<coordination-root>/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<sha>/candidate.json
<coordination-root>/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<sha>/post-merge.json
```

Receipts never record credential values, the configured origin, or raw
dashboard URLs. A failed receipt records only identifiers, statuses, and the
safe failure reason.

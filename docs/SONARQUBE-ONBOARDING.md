# SonarQube Exact-Head Onboarding

This repository uses the fixed SonarQube project key
`thebtf_netcoredbg_mcp`. The tracked runner is
`scripts/run_sonarqube_exact_head.py`; it is the required release-scan command.
It never accepts `SONAR_ADMIN_TOKEN`, never places credentials in argv, and
writes only secret-free receipts.

## One-time workstation setup

Install the supported SonarScanner for .NET on `PATH`:

```powershell
dotnet tool install --global dotnet-sonarscanner
```

If the tool is already installed, use `dotnet tool update --global
dotnet-sonarscanner`. The runner discovers `dotnet-sonarscanner`,
`SonarScanner.MSBuild.exe`, or `SonarScanner.MSBuild`; an exceptional local
path can be supplied as a single executable with `--scanner <path>`.

Keep the two project-scoped Sonar credentials in the existing secret Vault; the
Vault is the durable authority, not a file in this repository. In the
`netcoredbg-mcp` Vault project, materialize `sonarqube-analysis-token` as
`SONAR_TOKEN` and `sonarqube-read-token` as `SONAR_READ_TOKEN`. The administrative
credential remains separately owned in `nvmd-devops` as `sonarqube-admin-token`;
never materialize it here because the runner rejects `SONAR_ADMIN_TOKEN`. The
runner consumes only these runtime environment names:

```text
SONAR_HOST_URL
SONAR_TOKEN
SONAR_READ_TOKEN
```

Inject those values from the Vault directly into the runner's parent-process
environment without printing them. Do **not** create a `.env` in a scanner
worktree: the runner rejects any in-tree `.env`, including an ignored or
symlinked file, before it starts repository-controlled build code. Use the
credential-free SonarQube HTTP(S) origin for `SONAR_HOST_URL`.

`SONAR_TOKEN` is the project analysis credential. It is passed only to the
scanner's `begin`/`end` processes and the runner's submitted Compute Engine task
readback, which requires project Execute Analysis but not administration.
`SONAR_READ_TOKEN` belongs to a separate non-admin principal with project Browse
access and is used for the analysis-bound quality gate, current-analysis
bookends, issue inventory, and hotspot inventory. Any `SONAR_ADMIN_TOKEN` is
rejected.

Neither credential reaches Git, `dotnet build`, or test child processes. Never
put either token in a command line, a tracked file, a receipt, or a log.

## Release scans

Run both roles from a new clean detached linked worktree at the role's exact SHA.
The runner checks clean status before and after scanning, uses the committed
`SonarQube.Analysis.xml`, sets `sonar.scm.revision` to the captured
40-character HEAD, builds the solution plus every maintained `.csproj` omitted
by it, and requires the submitted CE task, current-analysis bookends, observed
scanner/task metadata, analysis-bound quality gate, full issue disposition
inventory, and full hotspot inventory to match that SHA.

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

A failed receipt records only identifiers, statuses, and the safe failure
reason—never credential values.

# SonarQube Exact-Head Onboarding

This repository uses the fixed SonarQube project key
`thebtf_netcoredbg_mcp`. The tracked runner is
`scripts/run_sonarqube_exact_head.py`; it is the required release-scan command.
It writes only secret-free receipts and redacted logs.

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

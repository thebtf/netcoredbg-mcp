# SonarQube Exact-Head Onboarding

This repository uses the fixed SonarQube project key
`thebtf_netcoredbg_mcp`. The tracked runner is
`scripts/run_sonarqube_exact_head.py`; it is the required post-release audit
command. It writes only secret-free receipts and redacted logs. User decision
of 2026-10-09: publish after installed consumer journeys, focused functional
checks, independent review, and non-Sonar release checks pass; run Sonar only
after actual publication, optionally asynchronously. Scanner or credential
availability, quality-gate status, coverage percentage, and findings do not gate
merge, tagging, publication, or release completion and do not create a release
Ask. Artifact-build workflow completion is not consumer publication.

## One-time local onboarding

The supported primary-owned output layout uses SonarScanner for .NET **11.2.1**.
One-time scanner installation is a separate, explicitly authorized maintainer
operation; the audit runner does not install or update tools or edit profiles.
Select the installed executable with `--scanner <absolute-path>` when PATH is
ambiguous. The runner resolves that executable's installed
`Targets/SonarQube.Integration.targets`, including the 11.2.1 .NET tool store.
Missing or ambiguous installed targets refuse before scanner begin.

The maintainer performing this one-time onboarding creates two **project-scoped**
SonarQube tokens for `thebtf_netcoredbg_mcp`: an analysis token with Execute
Analysis access and a separate non-admin Browse token. The maintainer writes
them, together with a declared credential-free **HTTPS** SonarQube origin, to the
primary repository-root `.env`. `SONAR_HOST_URL` is authoritative: the runner
accepts a pathless HTTPS origin or a root `/` suffix and canonicalizes both to
`scheme://netloc`; it does not upgrade or rewrite the configured authority.
Certificate verification remains enabled. HTTP is not a credential-bearing route.

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

## Post-release audit

After publication, use a clean detached linked source worktree **S** at the
published commit. It must contain no `.env`, symbolic link, reparse point, or
tracked/untracked/ignored residue. The committed `SonarQube.Analysis.xml` and
all project inputs come from S; the captured 40-character HEAD remains source
identity throughout the transaction.

The separate output root **O** is derived from the Git coordination owner:

```text
<coordination-root>/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<sha>/runs/<run-id>
```

Scanner `begin` and `end` run with **cwd=O**, `/s:<absolute-S-XML>`,
`sonar.projectBaseDir=S`, and `/install:false`. Every runner-owned .NET restore,
build, test, and preview publish uses `ArtifactsPath=O/artifacts`,
`SonarQubeTempPath=O/.sonarqube`, the installed integration targets as
`CustomBeforeMicrosoftCommonTargets`, disabled user ImportBefore wildcard hooks,
and `-nr:false`. Conflicting custom imports refuse. The real metadata and
`report-task.txt` are read from O, never copied from S.

Child-only environment overrides remove every case variant of `TF_BUILD`,
`TF_BUILD_BUILDDIRECTORY`, and `AGENT_BUILDDIRECTORY`. Python environments,
bytecode, uv/NuGet/.NET/scanner caches, temporary files, test scratch/results,
compiler output, coverage reports, and preview fixtures stay under O.
`DOTNET_ADD_GLOBAL_TOOLS_TO_PATH=false` prevents SDK first-run PATH registration;
the parent environment and user profile are unchanged.

The five fixed coverage producers, curated Python workload, positive coverage
denominators, Stateless production DLL/PDB restoration, exact normalization,
and bounded CE/current-analysis checks are unchanged. Coverage cleanup removes
only this run's consumed `O/coverage`; scanner metadata, receipts, and remaining
primary-owned outputs retain their actual provenance. Source cleanliness is
checked again before receipt publication.


Because SonarQube's analysis item has no project field, project proof is the
recorded `project=thebtf_netcoredbg_mcp` analysis query together with the
scanner-submitted CE task's required `componentKey` equal to that key, correlated
to the same `analysisId`/analysis `key` and exact revision. The issue inventory
enumerates the live `OPEN`, `CONFIRMED`, `FALSE_POSITIVE`, `ACCEPTED`, `FIXED`,
and `IN_SANDBOX` states and records resolutions such as `WONTFIX` and
`FALSE-POSITIVE`; all non-`FIXED` dispositions prevent a passing audit receipt,
not release. Confirmed substantial defects receive a subsequent corrective
patch; other findings go to the backlog. Retain original red receipts and their
source SHA. Do not suppress findings or fabricate a pass.

The live issue search uses `components=thebtf_netcoredbg_mcp`; it does not use
the legacy `componentKeys` filter.

Capture the actual published stable tag and its full commit SHA, then execute
from S (the variables below denote those captured values, not moving refs):

```powershell
python scripts/run_sonarqube_exact_head.py --role post-merge --published-tag $PublishedTag --published-commit $PublishedCommit
```

`--published-tag` and `--published-commit` are an inseparable pair, supported
only for `post-merge`. Before scanner begin, the existing GitHub CLI reads the
fixed repository on `github.com`: the release must exist, be nondraft and stable;
its tag must be annotated and peel to the supplied commit, which must equal
detached HEAD. The source `pyproject.toml` version must match the tag. A newer
`origin/main` does not change that identity. The receipt's `release_intent` is
the actual selected tag, not a pinned release version.

For existing no-pair callers, the `post-merge` role still requires
`HEAD == origin/main`; `candidate` retains its existing audit classification.
Their nondiagnostic intent comes from the actual source version. None of these
roles authorizes publication or creates a release gate.

The source's test drivers must support external artifacts and explicit compiled
source binding. Preparation refuses an incompatible immutable older checkout
with `COVERAGE_OUTPUT_INCOMPATIBLE` before scanner begin. Do not edit its source,
copy binaries into source `bin`, add links, or audit a newer SHA under the old
tag. Preserve the refusal; only a compatible, actually published target can take
the full route.

The runner serializes local scans for this project, requires a clean detached
linked worktree before and after scanning, polls only the scanner-submitted CE
task for at most 10 minutes, and queries the gate with that task's analysis ID.
It fails closed for a passing audit receipt unless the gate status is `OK`;
`WARN`, `ERROR`, `NONE`, API denial, a head/revision/current-analysis mismatch,
unexpected ignored state, incomplete issue/hotspot paging, prohibited issue
disposition, or any hotspot prevents a passing audit receipt, not release.
The all-hotspot block is this runner's conservative audit classification;
SonarQube's native REVIEWED outcomes remain recorded as facts. Retain real red,
failed, and blocked outcomes, including legacy `PROJECT_RELEASE_PROTOCOL_BLOCKED`
labels, without turning them into release blockers.

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
inaccessible endpoint is incomplete audit evidence and prevents a passing receipt.

New receipts use schema **4**, retaining the role, captured SHA, project,
coverage/analysis/inventory/cleanup fields and adding the verified
`published_release` binding (or null for a no-pair run). Artifact references are
relative to the coordination root. The same validator continues to read
historical schema-3 receipts with their original `.tmp/sonarqube-coverage` paths;
it does not migrate or rewrite them. New per-run receipts never replace old
role-named receipts:

```text
<coordination-root>/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<sha>/runs/<run-id>/post-merge.json
```

Receipts never record credential values, the configured origin, or raw
dashboard URLs. A failed receipt records only identifiers, statuses, and the
safe failure reason.

`SONAR_CREDENTIALS_UNAVAILABLE` and `SONAR_SCANNER_UNAVAILABLE` hold only the
audit. Record unavailable input names, never their values; do not infer a scan
or pass. Never replace original failure evidence with a stale/latest-project
result or treat a later corrective patch's analysis as proof for an older SHA.

Source qualification without a scan consists of the focused runner/receipt
tests and actual external-artifacts test-driver execution with outputs under
the coordination owner's `.agent`. It is not scanner, server, credential, CE,
coverage-import, or quality-gate proof. There is no new preflight-only CLI:
the command above is the real execution route and starts the scanner only after
its existing preparation succeeds. Preserve any unavailable tool or incompatible
source as an explicit no-scan result rather than fabricating a passing audit.

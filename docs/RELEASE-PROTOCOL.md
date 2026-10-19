# Release Protocol

## Applies When

This protocol applies to every versioned `netcoredbg-mcp` release published from
`main`, including milestone tags such as `v0.18.0`.

It is mandatory when a change affects any of these surfaces:

- Python package metadata or runtime package contents.
- MCP tools, prompts, resources, or CLI behavior.
- Runtime-smoke, UI automation, FlaUI bridge, or fixture behavior.
- GitHub Actions release or package-publish workflows.
- Public documentation, examples, release notes, or production testing
  playbooks used by package consumers.

## Release Decision Order

Release decisions use this order; later evidence cannot overrule an earlier failed gate:

1. **Primary UXDD consumer-mode release gate.** Build and install the release candidate, then exercise every user journey claimed by the release through the same public CLI/MCP entry point and packaging shape a consumer receives. Every claimed journey must reach `PRODUCT_WORKS`.
2. **Supporting — test protocols.** Run the required unit, integration, critical, runtime-smoke, build, and packaging checks. They are mandatory evidence, but green tests cannot turn `PARTIALLY_WORKS` or `BROKEN` consumer behavior into a releasable product.
3. **Supporting — independent review and release mechanics.** Resolve blocking review findings, prove version parity, and complete non-Sonar git, tag, publication, and post-publication checks.
4. **After publication — SonarQube audit.** User decision of 2026-10-09: run exact-SHA analysis afterward/asynchronously when the scanner and safe credentials are available. Quality-gate status, coverage percentage, findings backlog, scanner, and credentials are not merge/tag/publication gates and do not create a release Ask. Preserve real red or blocked receipts; confirmed substantial defects receive a subsequent corrective patch, and other findings go to the backlog. Never suppress findings or fabricate a pass.

A planned PATCH/MINOR release inside a legitimate run is autonomous. A legitimate run is a bounded spec, PRD, ADR, or active run contract with explicit acceptance criteria and release intent. User review, approval, and a separate `release` / `go ahead` command are not routine gates. If the governing artifact omits release intent or marks release out of scope, do not infer a release from implementation alone.

## Additional Release Surfaces

| Surface | Version source | Publish command | Verification |
| --- | --- | --- | --- |
| Python package `netcoredbg-mcp` | `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`, `uv.lock` | Push annotated tag `vX.Y.Z`; `.github/workflows/publish.yml` publishes to PyPI | `gh run view` for the tag workflow, `python -m pip install netcoredbg-mcp==X.Y.Z` or local wheel smoke when PyPI propagation is pending |
| GitHub Release | Annotated git tag `vX.Y.Z` | `.github/workflows/publish.yml` creates a release on `refs/tags/v*` | `gh release view vX.Y.Z` |
| TestPyPI dry run | Same package version | Manual `workflow_dispatch` with `target=testpypi` | `gh run view` and TestPyPI package page when used |
| CLI entry point | `src/netcoredbg_mcp/__init__.py` | Wheel install or editable local deploy | `netcoredbg-mcp --version` prints `X.Y.Z` |
| MCP server surface | Source package and bridge files included by `pyproject.toml` | Wheel build and installation | Critical suite MCP-surface test passes |
| FlaUI bridge sources | `pyproject.toml` `force-include` bridge paths | Wheel build and installation | Bridge build or compile-only gate passes on Windows when bridge files changed |
| Public docs and examples | `README.md`, `README.ru.md`, `CHANGELOG.md`, `RELEASE_NOTES.md`, `docs/` | Release-prep PR | Docs tests and production playbook checks pass or block release |

## A1 Preview-Channel Promotion (S4 Only)

This section governs only the opt-in Windows x64 Stateless preview prerelease
channel after explicit S4 approval. It does not modify the existing Python
package, PyPI, `vX.Y.Z` tag, or default-selector rules above, and grants no
authority to Program B or Program C.

S4 is a separate explicit external decision after the exact candidate's consumer proof, required independent reviews, and passing pre-decision Stage evidence exist; it is not a human-only gate. Within the accepted autonomous A1 mandate, Root may make that decision as a delegated executor without a new personal operator `go ahead`. The mandate permits the decision process, not its outcome: neither the mandate, `PASS`, an identity allowlist, nor GitHub access constitutes `APPROVE`. Record an explicit candidate-bound `APPROVE` or `DECLINE`, decision time, intended destination, and exact Stage/review evidence. Do not pre-approve unknown future bytes, extend the mandate to a different product objective or separately authorized scope, or bypass FR-006 for unresolved blocking findings or missing required lenses. Separate build, evidence, and promotion workflow runs remain within this A1 process; a retry still requires fresh current-attempt admission for the same approved candidate.

The existing operator principal is `thebtf`, numeric GitHub ID `7106373`, confirmed by the public [GitHub user identity](https://api.github.com/users/thebtf) and recorded in `.agent/runs/a1-artifact-20261016/run-metadata.json`; this is an identity binding, not a new authority grant. Derive `decision_author` from actual authenticated run/event actor metadata, never a caller assertion, and verify both numeric ID and login for the actor and any rerun initiator. An authenticated workflow call made by delegated Root must not be described as the operator's personal review or click. Keep the explicit `authorized_dispatcher` separate; a distinct closed identity is permitted only where the existing Decision contract allows it, not through a new team, service, nonce, or PAT authority. Numeric actor admission and evidence/promote execution remain implementation work; this interpretation records no current S4 Decision, approval, or promotion and adds no Program B/C authority.

The [Attempt 3.0 contract](../specs/010-a1-preview-artifact/contracts/promotion-attempt.schema.json) separates Decision authorization, both current dispatcher/rerun ID/login maps, and execution-token provenance. Both initiators must match the Decision dispatcher. `authorization.execution_policy` binds the independent current run/attempt/job, exact workflow SHA and SHA-256 of that tracked workflow blob, with model `GITHUB_JOB_TOKEN_SERVER_ENFORCED`. Reviewed source preserves top-level `permissions: {}`, exactly `actions: read` / `contents: write` for `promote`, and job-scoped `GITHUB_TOKEN: ${{ github.token }}`. This is configuration/source evidence, not observed effective grants, token introspection, or a guarantee of success. App/installation IDs stay `UNOBSERVED`/null. The former runner-log attestation, collector and `permission-probe` route are retired; removed requests and Attempt 1.0/2.0 fields refuse without aliases.

GitHub authorizes each intended operation. Observation 2.0 retains closed `operation_results` with original attempt/intent/target, response class and exact postcondition. Only a valid acknowledgement plus exact readback (`CONFIRMED`), or an ambiguous original response with complete exact read-only reconciliation (`RECONCILED`), permits advancement. A definite refusal remains `DENIED` and terminates the sending attempt even if another writer later creates matching state. Conflicts/collisions stop without clobber. A 403 rate limit does not prove missing permission; a 404 does not prove absence. Lost, malformed, truncated or potentially applied responses remain `UNKNOWN` until exact read-only reconciliation; never resend that uncertain operation under a new step, run or request identity. Retain partial resources and safe diagnostics, never compensate by deletion. Correct YAML can still be denied by token validity, policy, rulesets or endpoint requirements; no credential expansion is permitted. `contents: write` is repository-wide; reviewed exact-object guards, not token scope, restrict the approved tag and assets.

The approved preview candidate identity is the one exact tuple of build run ID,
source commit, preview tag, archive SHA-256, manifest SHA-256, executable
SHA-256, and prerelease destination. The build workflow source SHA, manifest
commit, T07 proof receipt, S4 approval, and promotion checkout target MUST be
byte-identical. Promotion downloads the retained source-run archive and
manifest only; it MUST NOT rebuild, substitute local bytes, or re-test a
different payload.

Before the first mutation, independently discover prior canonical attempts for this candidate/destination and classify complete remote state. `predecessor_attempt: null` requires complete evidence of no earlier admitted attempt. A retained Attempt without conclusive later results is potentially sent, not unstarted; missing/expired/ambiguous history holds dependent mutation. Resolve its original intent, known tag OID and last outcome read-only in the new O0; never erase the original `UNKNOWN` or claim that a denied sender performed a later observed operation. A later independently admitted attempt may retry a definitely-not-applied refusal only after its cause is resolved, under fresh context and safe exact state. Unresolved uncertainty cannot be retried.

An absent preview tag and release admit initial annotated-tag and draft-prerelease creation only after that predecessor admission. Matching interrupted state may receive only genuinely remaining operations: the annotated tag must peel to the approved commit and the release must be the exact matching draft or complete prerelease. A matching empty or partial draft may receive only missing approved assets; matching complete states receive no asset overwrite. Preserve create-only empty-lease tag push, exact release/asset IDs, `prerelease: true`, `make_latest: false`, three bounded advancement blocks, and immutable P0 → C0 → Attempt → O_N/C_N readback. No failed/denied/unresolved advancement or diagnostic upload authorizes a later mutation.

Any different tag target, release tag or destination metadata, non-draft
incomplete release, expected-asset name/size/SHA-256 mismatch, colliding asset
name, expired source artifact, or different candidate identity is a collision:
hard-refuse, record the evidence, and use a new preview version/tag for a
corrected source. Never delete, move, reuse, or overwrite a preview tag or
asset to recover from a collision.

Before publishing a matching complete draft, and again for a published complete
prerelease, freshly download each remote archive and manifest and compare its
size and SHA-256 with the approved manifest/T07 receipt. Remote asset metadata
or a prior upload acknowledgement is not byte proof. The post-publish receipt
MUST bind those fresh remote bytes to the approved identity and replay the
installed preview consumer, EOF, and unchanged Python rollback journeys.

### Reform the candidate after authorization-policy changes

The authorization amendment is an internal mechanism choice within Root's existing delegated A1 mandate, not an operator click or a new product approval. It changes workflow/publisher policy and two Catalog snapshots (`docs/RELEASE-PROTOCOL.md` and the A1 specification), so it requires a successor canonical-main candidate. Preserve original commit `5f96014b930e269752b47e878f45f8d220b6bfc7`, build `38960876498/1`, and its retained APPROVE unchanged as original-candidate-only evidence; do not rebind or upgrade them.

Before any new tag: finish and verify the source cutover; integrate exact canonical SHA S; run the canonical build at S for a new payload, manifest, Candidate and Catalog; replay complete retained downloaded-candidate consumer/denial/EOF/unchanged-Python proof; obtain S-bound seven-lens and distinct independent review records and passing pre-decision Stage; then retain Root's new explicit S4 `APPROVE` or `DECLINE`. Derive the next unused preview ordinal from current inventory; do not assume a tag is free. Promotion consumes those retained bytes without rebuilding. Source checks, simulated transport, unchanged executable bytes or publisher acknowledgements do not prove actual public release. Completion still requires fresh public downloads, consumer and Python rollback success, Remote Verification, post-publication Stage and non-authorizing Program B Handoff. Negative live branches not encountered remain `NOT_OBSERVED_LIVE`; do not induce a test publication or denial.


## Required Gates

Pre-publication gates must pass before annotated tag creation and push. Tag
creation and push depend only on these rows plus a clean tag-collision check;
they do not wait on remote tag visibility, workflow completion, GitHub Release,
PyPI publication, or post-publication local workstation deploy evidence. The
built-wheel install smoke row remains mandatory before tagging.

Post-publication verification rows confirm publication after the tag is pushed.
Any failed non-Sonar gate or verification row stops release completion; failed
pre-publication gates also block tag creation. Sonar audit outcomes are recorded
separately and do not reopen approval or invalidate a completed working release.

### Pre-publication gates

| Gate | Command / evidence | Blocks release when |
| --- | --- | --- |
| Release git readiness | `git fetch --prune origin`; release branch clean and equal to `origin/main`; stale worktrees/branches classified and cleaned preserve-first | Any dirty, unique, abandoned, stale, or unsynchronized release state remains |
| Release protocol presence | This file is read and every mandatory row is represented in the release report | Protocol missing, stale, or silent on an active release surface |
| Version parity | Check `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog, and release notes | Any public version surface disagrees with `X.Y.Z` / `vX.Y.Z` |
| Changelog and release notes | `CHANGELOG.md` has a dated `X.Y.Z` section; `RELEASE_NOTES.md` has user-facing notes | Either file missing or contains only generic placeholder text |
| Documentation refresh | README and README.ru reflect the current tool/test counts and release highlights; docs examples touched since last tag have matching tests | Public docs still describe an older released surface |
| Critical suite | `uv run --locked --extra dev pytest tests/critical -m critical` | Any `@critical` test fails or the suite cannot run |
| Runtime-smoke docs/examples | `uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py` or a narrower documented equivalent | Docs examples, diagnostic schemas, or v2 critical guards fail |
| Package build | `uv build` | Wheel or sdist build fails |
| Wheel install smoke | Reuse the disposable release-candidate environment created for the primary UXDD consumer-mode release gate; run `netcoredbg-mcp --version` plus an import smoke | Install, import, or CLI version smoke fails |
| Primary UXDD consumer-mode release gate | Build and install the release candidate; execute `docs/PRODUCTION-TESTING-PLAYBOOK.md` through the public package/CLI/MCP surface; enumerate every user journey claimed by the release | Any claimed journey is not `PRODUCT_WORKS`; `PARTIALLY_WORKS`, `BROKEN`, private-helper-only proof, or unit-test-only proof blocks release |
| MCP PR review | Release-prep PR summary reports zero unresolved blocking findings, and reviewer status is clean enough for merge | Any `fix_now` or unresolved mandatory review thread remains |
| Tag collision check | `git ls-remote --tags origin refs/tags/vX.Y.Z` returns empty before tag creation | Target tag already exists on origin |

### Post-release SonarQube exact-head audit (not a release gate)

Run this audit only after actual consumer publication, optionally asynchronously.
An artifact-build workflow is not publication and must not spend an audit job
before release merely by naming it post-release. Capture the intended published
source SHA as `HEAD_SHA` and use a clean detached scanner worktree at that SHA.
Neither a candidate receipt nor a post-merge receipt authorizes merge, tagging,
publication, a required-gate catalog entry, or a release seal/PASS receipt.

Any mismatch between the captured target and scanner worktree HEAD, scanner
metadata, analysis, receipt, or post-scan HEAD invalidates that analysis evidence.
Recreate a clean scanner worktree and rescan the captured target. The legacy
runner label `PROJECT_RELEASE_PROTOCOL_BLOCKED` holds only this audit, not the
release. Never substitute a stale/latest-project result or relabel an older red
receipt as passing evidence.

#### Repository runner

Install and credential ownership are defined in
[`SONARQUBE-ONBOARDING.md`](./SONARQUBE-ONBOARDING.md). The only runnable
exact-head audit command is `python scripts/run_sonarqube_exact_head.py`. It derives the
only dotenv path as `<coordination-root>/.env`, where `coordination-root` is
the parent of `git rev-parse --git-common-dir`. It verifies and reads the same
no-follow file object, requiring owner-only access. On Windows, this includes a
non-reparse file, the current user's owner SID, a protected DACL, and no allow
ACE for another SID. The declared credential-free HTTP(S) `SONAR_HOST_URL` origin
is authoritative. The runner accepts a pathless origin or a root `/` suffix and
canonicalizes both to `scheme://netloc`; it does not upgrade or rewrite the
configured scheme or authority. Explicit process
values override the file only when their names use exact canonical casing. The
runner rejects `SONAR_ADMIN_TOKEN`, every other or mis-cased `SONAR_*` credential
name, and a scanner worktree containing `.env`, a symbolic link, or any reparse
point, including the root. It scrubs every `SONAR_*` case variant from build/test
children, redacts the configured origin and tokens from scanner argv/output, and
writes the role-aware secret-free receipt under the coordination root.

After publication, create a clean detached worktree at the captured source SHA
and use the existing post-merge role. Its name is analysis metadata, not a tag
authorization. The unchanged STRICT runner requires `HEAD == origin/main` against
the current `origin/main`; a delayed audit of an older published tag can
therefore truthfully block. Retain that blocked audit rather than substituting
a different SHA or making it a publication gate:

```powershell
python scripts/run_sonarqube_exact_head.py --role post-merge
```

The historical candidate role remains analysis metadata only; neither role is
a release prerequisite. The unchanged runner and receipt validator hardcode
nondiagnostic `release_intent` to `v0.23.12`. Actual audit receipts MUST retain
the captured source SHA and existing real metadata; do not relabel that intent
or claim a version-correct audit for later releases. Both limitations remain
analysis-only debt in NCD7, next patch `immutable-published-sonar-audit`. Its
implementation and RED/GREEN acceptance contract is recorded in the primary
coordination receipt `.agent/reports/sonar-policy-runner-nextpatch-native.json`.
For each post-release scan, perform and record all of the following:

1. Create a new detached scanner worktree at the captured target SHA, outside the permanent release checkout, before scanner begin. It must contain the committed `SonarQube.Analysis.xml` from that exact head and no `.env`, symbolic link, or reparse point anywhere in the scanner tree, including the root. The primary coordination root, resolved from the Git common directory, retains the sole allowed `.env`. Before scanning, require `git rev-parse HEAD` to equal the captured SHA and require `git status --porcelain=v1 --untracked-files=all --ignored` to report no tracked, untracked, or ignored entry within the configured scan scope. Refuse the scan otherwise. Run the scanner only in this isolated worktree; never treat permanent root `.agent` coordination residue as scanner source. The XML exclusions complement this check but do not replace it.
2. Read `sonar.projectKey` from that worktree's `SonarQube.Analysis.xml`. The XML key, scanner metadata key, submitted task-report key, and the submitted Compute Engine task's required `componentKey` MUST all equal the fixed repository project key `thebtf_netcoredbg_mcp`. Because the returned analysis item has no project field, its recorded `project=thebtf_netcoredbg_mcp` request, `analysis.key == task.analysisId`, and `revision == HEAD_SHA` are the project/revision proof. A candidate must not redirect analysis by changing XML. Invoke the repository-configured .NET scanner with that XML, the credential-free `SONAR_HOST_URL` origin, and `sonar.scm.revision=$HEAD_SHA`; the runner provides `SONAR_TOKEN` to `begin` and `end` only, with redacted display, then builds the repository between them. Preserve scanner metadata and task-report evidence without token values.
3. Poll **only** the submitted Compute Engine task every five seconds for no more than **10 minutes**. A completed analysis must terminate as `SUCCESS` and resolve exactly one analysis ID for `thebtf_netcoredbg_mcp`; record its real analysis-bound quality-gate status. Only `OK` can support a passing audit receipt. A latest-project quality-gate result is not evidence. If the deadline expires, write the terminal timeout receipt with the target SHA, submitted task ID, polling start/deadline, and last observed task state (or no response). A failed task or any nonterminal result at that deadline is `PROJECT_RELEASE_PROTOCOL_BLOCKED` for this audit only.
4. Before scanner begin, create a baseline finding-key inventory by retrieving **every page** of current project findings. Record the fixed project key, query, total, page sequence, and every unique key; do not accept a first page, server result cap, or missing page as complete. After the exact-head analysis, **serialize all analysis activity for the fixed project** from the submitted Compute Engine task through the final receipt: no other scan may submit or complete for that project during this interval. Before and after the exact-head finding pagination, read the **current project analysis ID and revision** and require both to equal the **submitted analysis ID and `HEAD_SHA`**. Retrieve every finding page only between those matching readbacks. A mismatch, concurrent analysis, missing readback, or changed project analysis is `PROJECT_RELEASE_PROTOCOL_BLOCKED` for the audit; rescan the captured target after the project is quiescent. Preserve the union of baseline keys and keys discovered by any exact-head readback, their actual dispositions, and explicit empty-result and `pagination_complete=true` results when the inventory is complete. An unresolved finding makes the audit receipt non-passing, not the release. Live `OPEN`, `CONFIRMED`, `FALSE_POSITIVE`, `ACCEPTED`, and `IN_SANDBOX` statuses, plus `WONTFIX`, `FALSE-POSITIVE`, accepted risk, `NOSONAR`, issue suppression, and quality-profile exclusion do not establish `FIXED_IN_CURRENT_HEAD`. Do not suppress, accept, or mark a finding false-positive to fabricate a passing receipt. Route confirmed substantial defects to a subsequent corrective patch and other findings to the backlog; retain the original red evidence.
5. Write one secret-free receipt per scan containing the scan role, captured SHA, post-scan SHA, scanner-worktree cleanliness evidence, fixed project-key comparisons, task-report task evidence plus server-origin-match and dashboard-presence evidence, scanner revision, submitted task, bounded Compute Engine polling outcome, analysis ID, analysis-bound quality gate, serialized project-analysis ID/revision readbacks before and after the exact-head finding pagination, baseline/discovered finding-key inventories, complete page evidence, and actual empty-result/pagination-complete result. On failure, retain collected evidence and the real failure reason; do not claim missing evidence or a pass.

#### Exact analysis identity clarifications

- Do not treat `report-task.txt` as SHA evidence. The report identifies the submitted task; prove project scope through the recorded `project=thebtf_netcoredbg_mcp` analysis query and the submitted Compute Engine task's `componentKey`, then require its analysis key and revision to equal the submitted analysis ID and `HEAD_SHA`.
- The analysis key is unique analysis identity. The scanner metadata key, and submitted task-report key MUST all equal the fixed project key; the task ID equals the submitted Compute Engine task ID. Retain task-report task/server-origin comparisons in the receipt.
- Re-read `git rev-parse HEAD` after scanner end and before final receipt publication. A changed target invalidates the scan.
- Paginate the complete project issue inventory for `issueStatuses=OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX` (and the same status set with `inNewCodePeriod=true`) plus the project-scoped hotspot inventory. Require `pagination_complete=true`; a zero-total inventory must have `result_empty=true`. Treat an issue as `FIXED_IN_CURRENT_HEAD` only when it is absent from the post-scan inventory or has `issueStatus=FIXED` with omitted or `FIXED` resolution; every other current issue is `BLOCKING_DISPOSITION`, and every hotspot is `BLOCKING_HOTSPOT`.

`BLOCKING_DISPOSITION`, `BLOCKING_HOTSPOT`, and other runner blocker labels
describe the audit receipt only. Preserve their real values; no such label is
a merge, tag, publication, release-seal, or release-completion dependency.

`SONAR_HOST_URL` and `SONAR_TOKEN` are required analysis credential inputs. If either is missing, blank, inaccessible, rejected, unusable, or cannot be used without exposing it, record `SONAR_CREDENTIALS_UNAVAILABLE` with the missing/unavailable input name only (never its value), do not start a scan, and retain `PROJECT_RELEASE_PROTOCOL_BLOCKED` for the audit only. A missing or unusable configured scanner is separately `SONAR_SCANNER_UNAVAILABLE`; do not misclassify it as a credential failure. Neither condition permits an inferred scan or pass, and neither holds an otherwise working release. Likewise, a non-clean scanner worktree, task/analysis/project/server/revision mismatch, nonterminal or failed Compute Engine task, terminal timeout, non-`OK` quality gate, incomplete pagination, or incomplete finding disposition prevents a passing audit receipt—not merge, tagging, publication, or release completion.

`SONAR_READ_TOKEN` is also mandatory for the analysis-bound gate, current-analysis
bookends, full issue inventory, and full hotspot inventory. Its absence, rejection,
or incomplete readback holds only the audit and prevents an inferred passing receipt.
### Post-publication verification

| Verification | Command / evidence | Blocks release completion when |
| --- | --- | --- |
| Remote tag visibility | Fetch `refs/tags/vX.Y.Z` into a temporary non-tag ref (for example `git fetch origin refs/tags/vX.Y.Z:refs/tmp/verify-vX.Y.Z`); require `git cat-file -t refs/tmp/verify-vX.Y.Z` to equal `tag`; delete the temporary ref; and confirm `git ls-remote --tags origin refs/tags/vX.Y.Z` is non-empty | Tag is missing, lightweight, or not visible on origin |
| Tag workflow completion | Locate the exact `.github/workflows/publish.yml` run triggered by `event=push` for tag `vX.Y.Z` whose head SHA matches the annotated tag target; capture that run ID; require `gh run view <run-id>` to report `completed` / `success` | Workflow fails, is ambiguous, or cannot be verified |
| GitHub Release | `gh release view vX.Y.Z` | Release missing |
| PyPI publication | PyPI package is visible or propagation delay is explicitly recorded with workflow success evidence | Package missing after normal propagation, or verification cannot be performed |
| Local deploy smoke | Workstation installation is updated to the released package or released wheel; `netcoredbg-mcp --version` reports `X.Y.Z` | Local executable still reports the prior version |

## Release Autonomy

| Mutation class | Autonomy | Approval trigger | Evidence |
| --- | --- | --- | --- |
| Local release-prep branch and commit | Automatic | Sensitive content, incoherent diff, or unrelated dirty state | Git status, diff, and gate output |
| Release-prep PR creation | Automatic | Unreviewed broad product change outside release-owned files | PR URL and changed-file list |
| PR merge | Automatic after the primary UXDD consumer-mode release gate, independent MCP PR review, and required non-Sonar checks are clean | `PARTIALLY_WORKS`, `BROKEN`, `fix_now`, unresolved mandatory review threads, failed non-Sonar checks, or high-risk scope expansion | UXDD run report, MCP PR summary, GitHub merge state, non-Sonar status checks |
| Planned PATCH or MINOR tag and remote publication | Automatic only after the completed integration scope is on `main`, no dependent slice in the same integration wave remains active, every claimed consumer journey is `PRODUCT_WORKS`, and every non-Sonar pre-publication gate passes | MAJOR/breaking change, tag collision, failed non-Sonar gate, ambiguous scope, production/customer deployment outside this workstation, secrets, or destructive cleanup with unpreserved work | Governing run artifact, UXDD evidence, non-Sonar pre-publication evidence, annotated tag target, post-publication remote tag/workflow/release/package smoke |
| MAJOR or breaking release | Approval required | Always | Explicit user approval naming the version |
| Production/customer deployment outside this workstation | Approval required | Always | Named target, deploy plan, health checks |

Project default: `auto_planned_patch_minor_after_uxdd_verified_integration`. A
planned PATCH/MINOR release inside a legitimate run needs no separate user
review, approval, `release`, `go ahead`, or equivalent command. Autonomy begins
only when the governing spec, PRD, ADR, or active run contract contains explicit
release intent and acceptance criteria. The primary release criterion is the
installed consumer journey: every flow claimed as shipped must be
`PRODUCT_WORKS`. Required test protocols, independent review, and mechanical
release checks remain mandatory supporting gates, but none can override a
failed or partial UXDD result. Any failed pre-publication gate blocks tag
creation. Any failed post-publication verification blocks release completion;
a pushed tag enters Recovery After Tag Push. Same-tag publication repair/retry
there remains automatic when the tagged commit and release artifacts are unchanged;
new-patch correction also remains automatic unless an existing high-risk
approval trigger applies. Approval is not a substitute for green gates.

## Version Alignment

All of these must match the target version before the release commit:

- `pyproject.toml` project version.
- `src/netcoredbg_mcp/__init__.py` `__version__`.
- `uv.lock` editable package version.
- `README.md` and `README.ru.md` release headline.
- `CHANGELOG.md` release section.
- `RELEASE_NOTES.md` title and installation examples.
- Annotated git tag `vX.Y.Z`.

## Release Notes

`CHANGELOG.md` is the technical history. `RELEASE_NOTES.md` is the operator and
consumer summary used for PR review and GitHub release notes. It must include:

- Release version and date.
- Main user-visible changes.
- Upgrade notes and compatibility caveats.
- Release gates and known residual risks.

GitHub auto-generated release notes are allowed as an additional artifact, not
as the only release-note source for a milestone release.

## Publish / Smoke / Handoff

1. Prepare release-owned files on a branch named `work/release-vX.Y.Z-prep`.
2. Build and install the release candidate; run the primary UXDD consumer-mode release gate through the public package/CLI/MCP entry point; then run the remaining local pre-PR protocol gates and write evidence paths into the release report. PR review waits for step 3, and post-publication verification waits for step 6.
3. Open a PR, run MCP PR review, fix or resolve blocking findings, and run required non-Sonar checks. Merge automatically only after the primary UXDD consumer-mode release gate reports `PRODUCT_WORKS` for every claimed journey and all supporting non-Sonar gates are clean. Do not add a pre-merge Sonar gate.
4. Fast-forward local `main` to `origin/main`; capture the actual merge/tag target SHA for source and publication provenance. No Sonar receipt is required before tagging.
5. After the completed integration scope is on `main`, no dependent slice in the same integration wave remains active, and every non-Sonar pre-publication gate passes, create an annotated tag with `git tag -a vX.Y.Z -m "Release vX.Y.Z"` and push it with `git push origin vX.Y.Z`.
6. Run all five post-publication verification rows from Required Gates: remote tag
   visibility, exact tag workflow completion, GitHub Release, PyPI publication,
   and Local deploy smoke. Any failed row blocks release completion and the final
   verdict per Terminal Verdict.
7. For the required Local deploy smoke row: install the released wheel/package
   on this workstation, then verify `netcoredbg-mcp --version` reports `X.Y.Z`
   and a package import smoke.
8. After actual publication, run or schedule the exact-SHA Sonar audit above when the scanner and safe credentials are available. Retain red, failed, or unavailable-input evidence independently of release completion; do not rerun publication because of an audit outcome.
9. Update `.agent/CONTINUITY.md` and the live dashboard with the final release verdict and the separate Sonar audit status.

## Recovery After Tag Push

A pushed release tag is immutable. Never move, delete, or reuse it.

Same-tag publication repair and re-verification for `vX.Y.Z` remain within
PATCH/MINOR release autonomy when the tagged commit and release artifacts are
unchanged. When code, metadata, or artifacts must change, a new-patch correction
also remains automatic: bump to a new PATCH version, rerun every mandatory
pre-publication gate, publish a new annotated tag on the corrected `main`
commit, and run post-publication verification—unless an existing high-risk
approval trigger applies.

| Situation | Action |
| --- | --- |
| Post-publication verification fails; tagged commit and release artifacts are correct | Repair or retry only the failed publication step for the same `vX.Y.Z`, then re-run post-publication verification |
| Code, metadata, or artifacts must change | Create and merge a hotfix PR that bumps to a new patch version, rerun every mandatory pre-publication gate, publish a new annotated tag on the corrected `main` commit, and run post-publication verification for the new version |
| Target tag already exists on origin before creation | Stop; do not overwrite or reuse the existing tag |

## Terminal Verdict

- `PROJECT_RELEASE_PROTOCOL_PASS`: the primary UXDD consumer-mode release gate
  reports `PRODUCT_WORKS` for every claimed journey and all supporting mandatory
  non-Sonar rows have current passing evidence; Sonar audit status is separate.
- `PROJECT_RELEASE_PROTOCOL_BLOCKED`: a claimed journey is `PARTIALLY_WORKS` or
  `BROKEN`, or at least one supporting mandatory non-Sonar row is missing, stale,
  failed, or cannot be verified. A Sonar-only red or blocked result is not this
  release verdict.
- `PROJECT_RELEASE_PROTOCOL_DRY_RUN`: intended actions are fully described and
  no mutation was performed.

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
3. **Supporting — independent review, SonarQube, and release mechanics.** Resolve blocking review findings, obtain the exact-head SonarQube scanner/quality-gate/finding-remediation receipt, prove version parity, and complete git, tag, publication, and post-publication checks.

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

## Required Gates

Pre-publication gates must pass before annotated tag creation and push. Tag
creation and push depend only on these rows plus a clean tag-collision check;
they do not wait on remote tag visibility, workflow completion, GitHub Release,
PyPI publication, or post-publication local workstation deploy evidence. The
built-wheel install smoke row remains mandatory before tagging.

Post-publication verification rows confirm publication after the tag is pushed.
Any failed gate or verification row stops release completion; failed
pre-publication gates also block tag creation.

### Pre-publication gates

| Gate | Command / evidence | Blocks release when |
| --- | --- | --- |
| Release git readiness | `git fetch --prune origin`; release branch clean and equal to `origin/main`; stale worktrees/branches classified and cleaned preserve-first | Any dirty, unique, abandoned, stale, or unsynchronized release state remains |
| Release protocol presence | This file is read and every mandatory row is represented in the release report | Protocol missing, stale, or silent on an active release surface |
| Version parity | Check `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog, and release notes | Any public version surface disagrees with `X.Y.Z` / `vX.Y.Z` |
| Changelog and release notes | `CHANGELOG.md` has a dated `X.Y.Z` section; `RELEASE_NOTES.md` has user-facing notes | Either file missing or contains only generic placeholder text |
| Documentation refresh | README and README.ru reflect the current tool/test counts and release highlights; docs examples touched since last tag have matching tests | Public docs still describe an older released surface |
| SonarQube exact-head gate | Follow the exact-head SonarQube procedure below using `SonarQube.Analysis.xml`; retain the scanner, Compute Engine, quality-gate, and finding-disposition receipt | Credentials or scanner are unavailable; the scan does not bind the current release head; Compute Engine or quality gate is not `SUCCESS`/`OK`; any finding is open or not fixed on that exact head |
| Critical suite | `uv run --locked --extra dev pytest tests/critical -m critical` | Any `@critical` test fails or the suite cannot run |
| Runtime-smoke docs/examples | `uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py` or a narrower documented equivalent | Docs examples, diagnostic schemas, or v2 critical guards fail |
| Package build | `uv build` | Wheel or sdist build fails |
| Wheel install smoke | Reuse the disposable release-candidate environment created for the primary UXDD consumer-mode release gate; run `netcoredbg-mcp --version` plus an import smoke | Install, import, or CLI version smoke fails |
| Primary UXDD consumer-mode release gate | Build and install the release candidate; execute `docs/PRODUCTION-TESTING-PLAYBOOK.md` through the public package/CLI/MCP surface; enumerate every user journey claimed by the release | Any claimed journey is not `PRODUCT_WORKS`; `PARTIALLY_WORKS`, `BROKEN`, private-helper-only proof, or unit-test-only proof blocks release |
| MCP PR review | Release-prep PR summary reports zero unresolved blocking findings, and reviewer status is clean enough for merge | Any `fix_now` or unresolved mandatory review thread remains |
| Tag collision check | `git ls-remote --tags origin refs/tags/vX.Y.Z` returns empty before tag creation | Target tag already exists on origin |

### SonarQube exact-head gate

Run this gate **twice** for every release. The two independent scan targets are:

1. **Release-candidate pre-merge scan.** After the final release-prep/review correction, capture `CANDIDATE_SHA` with `git rev-parse HEAD` and produce a complete receipt for that exact candidate.
2. **Actual post-merge scan.** After merge, fetch `origin/main`, fast-forward local `main` to that commit, capture the actual `origin/main` SHA, and produce a fresh complete receipt for that SHA **before tag creation**.

A candidate receipt must never authorize a tag. Any mismatch between the captured target and the scanner worktree HEAD, scanner metadata, receipt, or post-scan HEAD is `PROJECT_RELEASE_PROTOCOL_BLOCKED`; recreate a clean scanner worktree and rescan the mismatched target. The post-merge `origin/main` receipt is the only SonarQube evidence eligible for the tag gate. The annotated tag's target must equal that receipt's captured SHA; otherwise tag creation blocks.

For each scan below, `HEAD_SHA` means that scan's captured target: `CANDIDATE_SHA` for the pre-merge scan or the captured `origin/main` SHA for the post-merge scan.
#### Repository runner

Install and credential ownership are defined in
[`SONARQUBE-ONBOARDING.md`](./SONARQUBE-ONBOARDING.md). The only runnable
exact-head gate is `python scripts/run_sonarqube_exact_head.py`. It derives the
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

Run the candidate role from the clean detached `CANDIDATE_SHA` worktree after
the final review correction:

```powershell
python scripts/run_sonarqube_exact_head.py --role candidate
```

After fast-forwarding/fetching `origin/main`, use a new clean detached worktree
at that exact commit. The runner also requires `HEAD == origin/main` for this
role:

```powershell
python scripts/run_sonarqube_exact_head.py --role post-merge
```

The candidate receipt never authorizes tag creation; only the completed
post-merge receipt does.

For **each** scan, perform and record all of the following:

1. Create a new detached scanner worktree at the captured target SHA, outside the permanent release checkout, before scanner begin. It must contain the committed `SonarQube.Analysis.xml` from that exact head and no `.env`, symbolic link, or reparse point anywhere in the scanner tree, including the root. The primary coordination root, resolved from the Git common directory, retains the sole allowed `.env`. Before scanning, require `git rev-parse HEAD` to equal the captured SHA and require `git status --porcelain=v1 --untracked-files=all --ignored` to report no tracked, untracked, or ignored entry within the configured scan scope. Refuse the scan otherwise. Run the scanner only in this isolated worktree; never treat permanent root `.agent` coordination residue as scanner source. The XML exclusions complement this check but do not replace it.
2. Read `sonar.projectKey` from that worktree's `SonarQube.Analysis.xml`. The XML key, scanner metadata key, submitted task-report key, and the submitted Compute Engine task's required `componentKey` MUST all equal the fixed repository project key `thebtf_netcoredbg_mcp`. Because the returned analysis item has no project field, its recorded `project=thebtf_netcoredbg_mcp` request, `analysis.key == task.analysisId`, and `revision == HEAD_SHA` are the project/revision proof. A candidate must not redirect release analysis by changing XML. Invoke the repository-configured .NET scanner with that XML, the credential-free `SONAR_HOST_URL` origin, and `sonar.scm.revision=$HEAD_SHA`; the runner provides `SONAR_TOKEN` to `begin` and `end` only, with redacted display, then builds the repository between them. Preserve scanner metadata and task-report evidence without token values.
   Do not treat `report-task.txt` as SHA evidence. The returned analysis **project** MUST equal that key and its **revision** MUST equal `HEAD_SHA`; its analysis key is unique analysis identity. The scanner metadata key, and submitted task-report key MUST all equal the fixed project key. The submitted task ID equals the submitted Compute Engine task ID, with task-report task/server-origin comparisons retained in the receipt. Re-read `git rev-parse HEAD` after scanner end and refuse any changed target.
3. Poll **only** the submitted Compute Engine task every five seconds for no more than **10 minutes**. It must terminate as `SUCCESS`, resolve exactly one analysis ID for `thebtf_netcoredbg_mcp`, and yield an analysis-bound `OK` quality-gate readback. A latest-project quality-gate result is not evidence. If the deadline expires, write the terminal timeout receipt with the target SHA, submitted task ID, polling start/deadline, and last observed task state (or no response), then stop as `PROJECT_RELEASE_PROTOCOL_BLOCKED`. A failed task or any nonterminal result at that deadline is also `PROJECT_RELEASE_PROTOCOL_BLOCKED`.
4. Before scanner begin, create a baseline finding-key inventory by retrieving **every page** of current project findings. Record the fixed project key, query, total, page sequence, and every unique key; do not accept a first page, server result cap, or missing page as complete. After the exact-head analysis, **serialize all analysis activity for the fixed project** from the submitted Compute Engine task through the final receipt: no other scan may submit or complete for that project during this interval. Before and after the exact-head finding pagination, read the **current project analysis ID and revision** and require both to equal the **submitted analysis ID and `HEAD_SHA`**. Retrieve every finding page only between those matching readbacks. A mismatch, concurrent analysis, missing readback, or changed project analysis is `PROJECT_RELEASE_PROTOCOL_BLOCKED`; rescan the captured target after the project is quiescent. The receipt must track the union of baseline keys and keys discovered by any exact-head readback. If either readback contains a current key, fix the code and repeat the affected scan; the final receipt requires every baseline or discovered key to be absent from current findings or explicitly `FIXED_IN_CURRENT_HEAD` by analysis of the captured SHA. It must state an explicit empty-result and `pagination_complete=true` result for the final current-findings query. Live `OPEN`, `CONFIRMED`, `FALSE_POSITIVE`, `ACCEPTED`, and `IN_SANDBOX` statuses, plus `WONTFIX`, `FALSE-POSITIVE`, accepted risk, `NOSONAR`, issue suppression, and quality-profile exclusion are not release dispositions and block release. Do not suppress, accept, or mark a finding false-positive to satisfy this gate.
   A candidate is not clean while any `OPEN`, `CONFIRMED`, or `REOPENED` finding remains. Every baseline or discovered key must be absent as `FIXED_IN_CURRENT_HEAD`; do not classify `WONTFIX`, `FALSE-POSITIVE`, accepted risk, `NOSONAR`, issue suppression, or quality-profile exclusion as remediation.
5. Write one secret-free receipt per scan containing the scan role, captured SHA, post-scan SHA, scanner-worktree cleanliness evidence, fixed project-key comparisons, task-report task evidence plus server-origin-match and dashboard-presence evidence, scanner revision, submitted task, bounded Compute Engine polling outcome, analysis ID, analysis-bound quality gate, serialized project-analysis ID/revision readbacks before and after the exact-head finding pagination, baseline/discovered finding-key inventories, complete page evidence, and final empty-result/pagination-complete result.

`SONAR_HOST_URL` and `SONAR_TOKEN` are required credential inputs. If either is missing, blank, inaccessible, rejected, unusable, or cannot be used without exposing it, record `SONAR_CREDENTIALS_UNAVAILABLE` with the missing/unavailable input name only (never its value), do not start a scan, and stop as `PROJECT_RELEASE_PROTOCOL_BLOCKED`. A missing or unusable configured scanner is separately `SONAR_SCANNER_UNAVAILABLE` blocker evidence; do not misclassify it as a credential failure. Do not infer a scan, pass, merge, tag, or publish from either blocker. Likewise, a non-clean scanner worktree, task/analysis/project/server/revision mismatch, nonterminal or failed Compute Engine task, terminal timeout, non-`OK` quality gate, incomplete pagination, or incomplete finding disposition is a fail-closed pre-publication blocker—not an inferred pass, merge, tag, or publish.

`SONAR_READ_TOKEN` is also mandatory for the analysis-bound gate, current-analysis
bookends, full issue inventory, and full hotspot inventory. Its absence, rejection,
or incomplete readback is the same fail-closed credential/evidence blocker.
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
| PR merge | Automatic after the primary UXDD consumer-mode release gate, independent MCP PR review, the exact `CANDIDATE_SHA` SonarQube receipt, and required checks are clean | `PARTIALLY_WORKS`, `BROKEN`, `fix_now`, unresolved mandatory review threads, missing/stale/non-`OK` candidate SonarQube receipt, unresolved candidate SonarQube finding, failed checks, or high-risk scope expansion | UXDD run report, MCP PR summary, candidate SonarQube task/analysis/quality-gate/finding receipt, GitHub merge state, status checks |
| Planned PATCH or MINOR tag and remote publication | Automatic only after the completed integration scope is on `main`, no dependent slice in the same integration wave remains active, every claimed consumer journey is `PRODUCT_WORKS`, every pre-publication gate passes, and the post-merge `origin/main` exact-head SonarQube receipt is clean | MAJOR/breaking change, tag collision, failed gate, missing/stale/non-`OK` post-merge Sonar receipt, unresolved finding, ambiguous scope, production/customer deployment outside this workstation, secrets, or destructive cleanup with unpreserved work | Governing run artifact, UXDD evidence, pre-publication evidence, post-merge Sonar receipt, annotated tag target, post-publication remote tag/workflow/release/package smoke |
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
3. Open a PR, run MCP PR review, fix or resolve findings, then run the pre-merge `CANDIDATE_SHA` SonarQube gate after the final source change. If SonarQube requires a code correction, re-run affected review/required checks and obtain a new candidate receipt. Merge automatically only after the primary UXDD consumer-mode release gate reports `PRODUCT_WORKS` for every claimed journey and all supporting gates, including the candidate SonarQube receipt, are clean.
4. Fast-forward local `main` to `origin/main`; capture the actual merge/tag target SHA and run the second exact-head SonarQube gate in a new clean scanner worktree. If it finds a defect or its SHA does not equal the intended tag target, fix through a new PR and restart the applicable release gate sequence.
5. After the completed integration scope is on `main`, no dependent slice in the same integration wave remains active, and every pre-publication gate passes—including the post-merge SonarQube receipt—create an annotated tag with `git tag -a vX.Y.Z -m "Release vX.Y.Z"` and push it with `git push origin vX.Y.Z`.
6. Run all five post-publication verification rows from Required Gates: remote tag
   visibility, exact tag workflow completion, GitHub Release, PyPI publication,
   and Local deploy smoke. Any failed row blocks release completion and the final
   verdict per Terminal Verdict.
7. For the required Local deploy smoke row: install the released wheel/package
   on this workstation, then verify `netcoredbg-mcp --version` reports `X.Y.Z`
   and a package import smoke.
8. Update `.agent/CONTINUITY.md` and the live dashboard with the final verdict.

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
  reports `PRODUCT_WORKS` for every claimed journey and all supporting mandatory rows
  have current passing evidence.
- `PROJECT_RELEASE_PROTOCOL_BLOCKED`: a claimed journey is `PARTIALLY_WORKS` or
  `BROKEN`, or at least one supporting mandatory row is missing, stale, failed,
  or cannot be verified.
- `PROJECT_RELEASE_PROTOCOL_DRY_RUN`: intended actions are fully described and
  no mutation was performed.

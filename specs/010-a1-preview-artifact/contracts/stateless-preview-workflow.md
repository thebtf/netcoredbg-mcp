# Stateless preview workflow contract

The current `.github/workflows/stateless-preview.yml` implements manual `build` and `evidence` dispatch for the A1 preview artifact. Evidence dispatch selects either nonpublishing retention or a same-current-job permission probe. It runs only through `workflow_dispatch`, never from `push`, tag push, `workflow_run`, a schedule, or a Python release event. It creates no tag, release, or release asset, and changes neither Python nor Program B/C. There is no `promote` input, job, placeholder, or remote-mutation path; future publication remains separate implementation work.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

`mode` is a choice with `build` and `evidence`, defaulting to `build`. The other five inputs are strings with `required: false` and `default: ""`: `preview_version`, `preview_tag`, `retention_days`, `evidence_operation`, and `evidence_request`. Conditional requirements belong to the helper's real `GITHUB_EVENT_PATH` admission, not YAML's unconditional input requirement. The closed input combinations are:

| Admitted mode / operation | Required nonempty inputs | Other inputs |
| --- | --- | --- |
| `build` | `preview_version`, `preview_tag`, `retention_days` | Evidence fields absent or empty. A missing `mode` preserves the legacy three-field build request; an explicitly empty or unknown mode refuses. |
| `evidence` / `retain` | `evidence_request` | Build fields absent or empty; retention belongs inside the request. |
| `evidence` / `permission-probe` | None | Request and build fields absent or empty; the helper admits a fixed seven-day diagnostic retention. |

Each helper invocation closes the actual event inputs and checks command/mode compatibility. It derives source, repository, workflow/run identity, and receipt locations from trusted Actions, API, and Git facts. Unknown fields, unsupported operations, mixed build/evidence fields, or missing required values refuse before upload or probe. No request JSON is interpolated into a shell command or supplied as a path, URL, or script.

The job routes deliberately include invalid-value fallbacks rather than allowing a successful all-skipped run:

| Job ID / exact display name | `if` expression | Permissions |
| --- | --- | --- |
| `build` / `Build canonical-main preview candidate` | `inputs.mode != 'evidence'` | `contents: read`, `actions: read`, `pull-requests: read` |
| `retain_evidence` / `Retain canonical-main preview evidence` | `inputs.mode == 'evidence' && inputs.evidence_operation != 'permission-probe'` | `contents: read`, `actions: read` |
| `probe_permissions` / `Probe current job permissions` | `inputs.mode == 'evidence' && inputs.evidence_operation == 'permission-probe'` | `contents: write`, `actions: read` |

Set top-level `permissions: {}`. The probe's declared write grant exists only to observe its own runner-reported permission map; no remote write or test mutation is connected. Retention uses the live job's artifact runtime credentials through `actions/upload-artifact@v7`, not a PAT or `contents: write`. Helper and diagnostic tokens are available only as `GITHUB_TOKEN` environment values, never arguments or record contents.

Concurrency uses `stateless-preview-${{ inputs.preview_tag || format('run-{0}-{1}', github.run_id, github.run_attempt) }}` and `cancel-in-progress: false`. Build requests with a tag share its lock; evidence without a tag has a run/attempt-specific group, not an artificial shared empty-tag lock. GitHub supplies no `queue: max` workflow key. Each admitted run recomputes its source and artifact facts.

## Build from canonical main

The build job must do the following in order:

1. Check out `github.sha` exactly and fetch live `origin/main`. Require `github.ref`, the canonical-main workflow ref, `github.workflow_sha`, `github.sha`, checkout `HEAD`, and the resolved `origin/main` target to bind one canonical `refs/heads/main` commit.
2. In a clean detached functional worktree at that commit, use the existing Sonar-free `build/coverage.sh` producer through its supported `run_coverage_producer` owner. Require the solution and standalone builds, Python functional suite, and all five .NET producer suites to pass; validate the reports and restored stateless binary hashes. Preserve exact-head, tracked Wave2 entry, source-cleanliness, and cleanup guards. Do not start a scanner, load Sonar credentials, or produce a Sonar receipt in this build job.
3. Publish only `NetCoreDbg.Mcp.Stateless.Preview` as a self-contained, single-file Windows x64 executable.
4. Create the archive and inherited manifest. Validate its equations and hash archive, manifest, and extracted executable.
5. Upload the payload as a new Actions artifact with `if-no-files-found: error`, bounded retention, and `overwrite: false`.
6. Discover its current-run artifact ID and expiry facts through the GitHub Actions API, then seal the Candidate Identity Record and static Release Gate Catalog through `scripts/stateless_preview_artifact.py` without a Sonar receipt.
7. Upload Candidate Identity and Release Gate Catalog as separate new immutable Actions artifacts with the same bounded retention and `overwrite: false`.

The build job must not build a Python wheel, call `publish.yml`, use PyPI tooling, change the default selector, accept an arbitrary source or receipt, or create a promotion input.

The build job sets `DOTNET_ADD_GLOBAL_TOOLS_TO_PATH: 'false'` before SDK setup or any SDK use. Its existing SDK setup, permissions, build/functional producers, runtime check, and three build-artifact uploads remain in place.

This workflow is not publication. Do not rename a pre-publication scan to
"postrelease" or run it asynchronously from this build. The existing standalone
`scripts/run_sonarqube_exact_head.py --role post-merge` route is a strict audit
only after actual publication, as described in
[`docs/SONARQUBE-ONBOARDING.md`](../../../docs/SONARQUBE-ONBOARDING.md). Preserve
its real red or blocked outcome; it does not authorize or block promotion.

## Evidence retention source

The `retain_evidence` job checks out the exact dispatched SHA, fetches live main, selects the existing Python 3.12 setup action, and runs the stdlib-only helper. It neither builds a payload nor mutates tags, releases, or release assets. The ordinary GitHub Artifacts REST API is not a local-file upload route; retention uses `actions/upload-artifact@v7` inside this live job. The source is wired; successful source fixtures and live retention still require Root's separate evidence.

1. Require the original build run to have concluded successfully before independent consumer proof or review. Do not wait for these records in that original build: the Candidate Identity requires a completed successful run first.
2. Require `evidence_request` to be a JSON object with exactly seven fields: `candidate_identity_reference`, `release_gate_catalog_reference`, `bundle_base64`, `record_sha256`, `decision`, `authorized_dispatcher`, and `retention_days`. The two references identify retained Candidate Identity/Catalog files; `record_sha256` binds each of the three fixed original files. `decision` is an explicit candidate-bound `APPROVE` or `DECLINE` intent, `authorized_dispatcher` is the separate closed `{id, login}` identity, and retention is 1 through 90 days. Candidate identity already binds the destination. The intent is conditional on successful validation, not a completed S4 Decision; no caller author, future Stage reference, or future decision time is accepted. Derive `decision_author` from authenticated run/event metadata. Both actor and rerun principals must match numeric ID `7106373` and login `thebtf`; this binding grants no new scope. Preserve original producers, checks, findings, and timestamps; retention does not rerun their work.
3. Transfer the three original byte sequences as one bounded base64 ZIP in string dispatch inputs. Enforce the ingress limits below and validate candidate/provenance/author/review semantics before retention/sealing; successful byte decoding alone does not establish those semantics.
4. Upload those three original files together as one immutable artifact with `overwrite: false`, `if-no-files-found: error`, and bounded retention. Only after upload, resolve the real artifact ID, repository/run ownership, and expiry, independently download the saved files, verify their raw hashes, and form `repository, run_id, artifact_id, path, sha256` references. Keep original evidence outside tracked source; artifact names/job outputs are not sealed references.
5. Revalidate the actual consumer proof, complete seven-lens aggregate, distinct independent PR review, and Catalog through the existing Stage sealer. Upload the passing pre-decision Stage as a separate immutable artifact; obtain its real reference/raw hash after upload.
6. Seal the author's explicit Decision against that existing Stage, preserve decision/stage time order, and upload the Decision as a third immutable artifact. `PASS` alone must never generate `APPROVE`; missing lenses or unresolved blocking findings cannot be synthesized away. Decision names the authorized dispatcher but no future promote run, current-job permission readback, or remote state.
7. Independently read back the third artifact and its nested Stage/original evidence references with `verify-evidence-retention`. Revalidate raw hashes, exact request decision/dispatcher, real author, provenance, and expiry while the current evidence run is still `in_progress`. The source build must instead be `completed/success` at its exact candidate attempt, workflow/ref/SHA, with the successful `Build canonical-main preview candidate` job. A future promotion run may consume the completed evidence run's retained Decision. No record requires its own or another not-yet-uploaded object's ID/hash, and there is no fourth evidence upload.

The [S4 author boundary](../../../docs/RELEASE-PROTOCOL.md#a1-preview-channel-promotion-s4-only) requires a separate after-evidence decision, not a human-only gate. Root may decide as a delegated executor within the accepted autonomous A1 mandate; its workflow call is not the operator's personal review or click. Neither the mandate, passing checks, identity allowlist, nor GitHub access generates `APPROVE`. `DECLINE` also requires a valid passing pre-decision Stage under the existing Decision contract. The explicit `authorized_dispatcher` may differ from the derived author only as that closed contract permits. This implementation creates no actual S4 record or publication authority by itself; missing lenses and unresolved blocking findings remain refusals.

### Fixed CLI / upload interface

All four commands take zero additional arguments:

```text
python scripts/stateless_preview_artifact.py admit-evidence
E0: upload exactly consumer-proof.json, s2-s3-review.json, independent-pr-review.json
python scripts/stateless_preview_artifact.py seal-evidence-stage
E1: upload exactly pre-decision-stage.json
python scripts/stateless_preview_artifact.py seal-evidence-decision
E2: upload exactly promotion-decision.json
python scripts/stateless_preview_artifact.py verify-evidence-retention
```

Admission emits exactly `records_directory`, `stage_path`, `decision_path`, `records_artifact_name`, `stage_artifact_name`, `decision_artifact_name`, and `retention_days` for retain. Every output path is helper-owned below `RUNNER_TEMP` in the current run/attempt namespace. E0 enumerates the three filenames explicitly; E1/E2 use the exact admitted file paths. No upload includes the whole scratch/download directory or tracked source.

Each of the three immutable uploads sets `overwrite: false`, `if-no-files-found: error`, and the admitted bounded retention. Upload output IDs and container digests reach later helpers only through the fixed environment keys below; there are no caller artifact IDs/URLs in shell commands:

| Uploaded object | Environment prefix (suffixes `_ID` and `_DIGEST`) |
| --- | --- |
| E0 originals | `A1_EVIDENCE_RECORDS_ARTIFACT` |
| E1 Stage | `A1_EVIDENCE_STAGE_ARTIFACT` |
| E2 Decision | `A1_EVIDENCE_DECISION_ARTIFACT` |

Stage reads back E0. Decision reads back E1 and resolves its E0 records. Final verification reads back E2 and all nested references independently. Names, ownership, expiry, container digest, raw file hashes, and candidate/source provenance are revalidated; outputs alone are not sealed references. Existing library sealers remain pure and do not acquire Actions/network behavior.

### Bounded original-byte ingress

`decode_evidence_bundle` remains the byte-ingress owner, not evidence of a live upload. It accepts exactly `consumer-proof.json`, `s2-s3-review.json`, and `independent-pr-review.json`; it preserves original bytes/raw hashes and requires strict finite JSON. Base64 is at most 60,000 characters; decoded ZIP is at most 45,000 bytes; each member is at most 128 KiB and their total at most 256 KiB.

The protected boundary is the actual three returned records, not universal ZIP-format conformance. Refuse directory-parsing failures, duplicate/unexpected members, unsafe file metadata or paths, unsupported codecs/flags, inconsistent supported local-header/descriptor metadata, and violated actual byte/CRC/hash/JSON bounds. Allow only `ZIP_STORED` or `ZIP_DEFLATED`; the supported general flags are UTF-8, data descriptors, and DEFLATE-only compression options. Require complete DEFLATE EOF and matching actual sizes/CRC; cross-check supported local/central fields and signed or unsigned 32-bit descriptors. Local ZIP64 size markers are unsupported. Read fixed members without arbitrary extraction or shell-code interpolation. Unused legacy EOCD disk/count declarations are not independently conformance-checked: the library's actual enumerated member set must still be exactly the three fixed names, and every returned member must pass all content bounds and identities. This input ZIP is not executed or redistributed.

The historical standalone owner before classifier integration passed `409/409` in `.agent/runs/a1-artifact-20261016/evidence-ingress-header-fixed.xml`; the subsequent integrated owner passed `533/533` in `classifier-ingress-integrated.xml`. The supported-flags correction has its own complete `544/544`, zero-error/failure/skip receipt in `evidence-ingress-supported-flags-fixed.xml`. Historical receipts do not prove later source changes. The actual API smoke in `evidence-ingress-supported-flags-api-smoke.json` exercises supported byte preservation and unsupported-flag refusal; earlier smoke separately covers immutable mapping and raw-hash tamper refusal. Synthetic `FAIL`/`INCOMPLETE` records are transported unchanged, not approved. These checks neither close independent review findings by themselves nor expand schemas or replace candidate/review admission.

Separately, the complete dispatch must fit the [65,535-character total input limit](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch). Admission conservatively measures the full escaped ASCII JSON serialization of actual `inputs`, including nested request escaping and every reference, hash, decision/dispatcher, retention, and mode field. The decoder's 60,000-character bound alone does not prove that whole budget. Oversize input refuses explicitly; do not truncate checks, rewrite evidence, or substitute a local path. Live retention remains pending until independent download resolves all three real uploads and reproduces the original raw hashes.

`reference.sha256` hashes the referenced raw file, not GitHub's `artifact-digest` container. The current helper's `candidate.build.artifact.sha256` is `SHA256(archive_bytes + manifest_bytes)`, another distinct quantity; do not compare it with a container digest or change its schema for this ingress.

## Source freeze and permission capability (pending)

The build/evidence routing, four-command retention adapter, and diagnostic job are source implementation, not a final candidate or live execution receipt. Root must run the focused integrated fixtures and the bounded live probe/retention routes. Complete all required future publisher/admission code before freezing a final canonical candidate. Bind candidate commit, workflow SHA, exact checkout, and canonical-main provenance; a newer workflow checking out old source does not satisfy this. Subsequent source changes require fresh candidate-bound consumer proof, all seven lenses, distinct PR review, and an explicit retained Decision. Source fixtures, classifier evidence, and historical consumer proof do not approve a held or final candidate.

Actual current-job permission capability remains `UNKNOWN`: probe run
`38907673130` returned `UNAVAILABLE`, not a qualified grant. Preserve that report
unchanged. The existing nonmutating route remains this workflow at `main`,
`mode=evidence`, `evidence_operation=permission-probe`, with empty request/build
fields. Source/actor admission, exact checkout/canonical workflow/main binding,
declared `actions: read`/`contents: write`, fixed
`GITHUB_TOKEN: ${{ github.token }}` wiring and admitted seven-day retention do not
change. No SDK/build/retention-chain/publication commands are connected.

For one new run at attempt 1, the probe step uses
`scripts/stateless_preview_permission_timing_diagnostic.py` under an owned-process
watchdog. At t=0 it calls the existing collector once, preserves its original
report/exit code and derives the job ID from its verified metadata binding.
One additional same-job metadata GET, bounded to ten seconds, must confirm that
binding, `status: in_progress` and `conclusion: null`. Only `UNAVAILABLE` /
`CURRENT_JOB_LOGS_UNAVAILABLE` with that confirmation permits a second collector
at t=60 seconds from the first start. Other statuses, parser/identity failures
and other refusals stop without a second sample. The second sample is followed
by the same bounded metadata GET for the original job, and both reports must
bind that job. Scheduler lateness beyond one second, unfinished metadata or the
overall 120-second observation deadline produces `INCOMPLETE`; no wait extends
the window. The one-use `scripts/stateless_preview_permission_watchdog.py`
reuses the private Windows owner to create a suspended root, verify its private
non-breakaway, kill-on-close Job admission, and only then resume. That retained
Job contains descendants across root exit and concurrent spawning, without a
PID/name census. The deadline terminates the Job. Bounded pre/post-termination
Job-member snapshots retain membership-verified handles and start identities;
the `TotalProcesses` delta must be covered by newly captured identities, with
no subsequent growth. The terminating Job refuses new assignments. Snapshot,
handle acquisition and waits share one five-second cleanup deadline. Only zero
active Job processes, every captured handle signaled and the retained root's
exit code allow timeout finalization, partial-report reads and
`cleanup_verified=true` for upload. Unknown/failed cleanup permits neither reads
nor finalization; failed finalization never authorizes upload. Root exit, zero
Job accounting or Job-handle close alone is not writer-closure proof.
There is no overlapping worker, third sample or automatic redispatch. The fixed
60/120-second experiment is not a timing SLA or production retry mechanism;
Root retires the driver/wrapper after consuming its one run.

The collector itself is unchanged: fixed `GITHUB_JOB` mapping
`probe_permissions` → `Probe current job permissions` or future `promote` →
`Promote canonical-main preview candidate`, no job-name override, native
step-log → same-job-log fallback, zero-based setup position, running-job guards,
45-second worker deadline, ten-second socket timeout, 1 MiB log bound and redirect
safety. Only the first job exists in YAML. Collector reports retain numeric
run/attempt/job IDs plus `job_key`/`workflow_sha`. The manual single-sample CLI
with `--output current-job-permissions.json` remains valid separately; it is not
the timed workflow sequence.

The existing immutable upload explicitly lists
`permission-timing-diagnostic/current-job-permissions-t0.json`,
`permission-timing-diagnostic/current-job-permissions-t60.json` and
`permission-timing-diagnostic/timing-summary.json` under artifact name
`stateless-preview-current-job-permissions-{run_id}-{run_attempt}`. Collector
reports remain distinct and unaltered; an unexecuted second sample creates no
report. The summary records planned/actual monotonic times, exact
repository/run/attempt/job/key/source/workflow SHA, terminal metadata facts,
observed collector exit codes and existing-report hashes. Unobserved exit codes
remain null. Available log bytes and actually parsed grants remain separate:
HTTP 200 with parser refusal is not permission qualification. A later qualified
sample never overwrites the first negative or generates promotion approval.
The admitted seven-day upload still runs only after probe success/failure and
`cleanup_verified=true`, uses `always()`, `overwrite: false` and
`if-no-files-found: error` without
`continue-on-error`, and excludes raw logs, tokens, signed URLs, internal payloads
and Diag. A complete unqualified or incomplete experiment remains nonzero and
leaves dependent admission/mutation unauthorized.

A successful probe shows only observed availability of that job's runner-reported grant, not universal API availability or token introspection. Every real attempt still needs its own actual readback. Missing, ambiguous, wrong-job/attempt, or insufficient grants refuse before mutation. YAML, repository defaults, endpoint-required permission headers, successful GETs, and another job's grant are not substitutes. A failed bounded probe does not prove global impossibility or authorize falling back to declarations.

Attempt 2.0 now separates the Decision author/authorized dispatcher, both current initiator ID/login maps, and execution-token provenance. Both initiators must equal the resolved Decision dispatcher; the evidence adapter's S4-author allowlist does not become a token principal or an extra dispatcher restriction. Trusted future wiring must use `GITHUB_TOKEN: ${{ github.token }}`; record `source: github.token`, `kind: GITHUB_APP_INSTALLATION_TOKEN`, `identity_observation: UNOBSERVED`, `app_id: null`, and `installation_id: null`. Never infer IDs, introspect token bytes, or fall back to a PAT/auth cache. Unknown live capability holds dependent admission/mutation, not independent evidence retention or classifier work.

## Source-only Attempt admission; future workflow promotion

The future `promote` mode in this workflow may admit promotion only after a retained Promotion Decision consumes a passing `pre-decision` Stage Gate Evidence record through the ordering above. The Decision must name one authorized GitHub dispatcher, but it must not name a historical consuming run or require future-stage evidence.

The pure `seal_promotion_attempt` API implements the Attempt 2.0 source seam. It takes the existing candidate/catalog/raw bytes/references, resolver and repository root, plus a closed independent `current_promotion_context`: `current_attempt_id`, unchanged eight-field `workflow_run`, positive decimal-string `job_id`, `job_key: promote`, `workflow_sha`, `checkout_head`, `origin_main_target`, `current_github_actor`, and `current_github_triggering_actor`. The future caller must derive those facts from trusted runtime admission, never dispatch or Attempt claims. Candidate source/build, main, checkout and workflow SHA agree. Resolve/hash the `APPROVE` Decision and both Stages with existing sealers; exact Decision authorization and pre-decision receipt must match. Resolve the closed `AVAILABLE`/`QUALIFIED` same-promote-job report, native log hash/source, timestamps, before/after HTTP evidence, full grants and exact two-right projection. Require current-attempt remote records and re-derive the complete `ADMITTED` classification. A different initiator/run/attempt/job, stale or colliding record, failed Stage, declaration-only grant, extra field, legacy 1.0/`authenticated_actor`, or changed source/workflow refuses. This adds no YAML mode/job, CLI route, publisher, live grant or approval.

The current `pre-publication` descriptor subset is empty. Its stage evidence
still binds the candidate and catalog. Sonar is not a seal or `PASS` dependency;
the fresh source, provenance, permission, and remote-state checks remain required.

The future attempt must not require `post-publication` evidence. That evidence can exist only after a prerelease is public.

## Future publisher recovery (not implemented)

The future fresh attempt must create a Remote Observation and a Remote Classification. The existing pure classifier admits only `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, or `collision`; it does not collect remote state or perform these effects.

The future publisher may create an annotated tag only for `unstarted`, and only the draft prerelease for `tag_only`. It may upload only missing matching assets for `draft_empty` or `draft_partial`, and must verify draft assets before publication. A retry needs a new attempt and fresh matching classification. None of these mutation commands is wired in the current YAML.

Neither the current workflow nor the future publisher may rebuild a retained candidate, use `--clobber`, overwrite/delete an asset, move/delete/reuse a tag, delete/replace a release, invoke Python packaging, or publish to PyPI.

## Future post-publication closure (not implemented)

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main mismatch, catalog policy drift, an omitted functional or review gate, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal. Sonar audit absence or a real red outcome must not reject an otherwise valid functional candidate.

The supplied `test_evidence_cli_*` fixtures exercise the four-command boundary, original-byte retention, full escaped-input budget, actor/rerun and source-build provenance, ordered E0/E1/E2 readback, request substitution, and no automatic `APPROVE`. Root's pre-implementation dispatch run failed the unknown-mode/unknown-input refusal assertions because the old helper returned `READY`/zero, as recorded in `.agent/runs/a1-artifact-20261016/evidence-cli-dispatch-red.xml`. This is the intended source RED, not an integrated PASS. Root must run the focused owner after implementation lands. Fixture success does not complete the same-job capability probe, live retention, final candidate, or publisher.
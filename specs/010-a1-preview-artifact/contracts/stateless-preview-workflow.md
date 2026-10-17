# Stateless preview workflow contract

The current `.github/workflows/stateless-preview.yml` implements manual `build`, nonpublishing `evidence` retention, and `promote` dispatch for A1. The retired `permission-probe` selector is rejected, not aliased. This single canonical publisher runs only through `workflow_dispatch`, never `push`, tag push, `workflow_run`, schedule or Python release events. Only an admitted promote job can create the approved annotated tag, matching draft and missing assets, then publish the prerelease. Source implementation is not live approval or an execution receipt. Python and Program B/C remain unchanged.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

`mode` is a choice with `build`, `evidence`, and `promote`, defaulting to `build`. The other six inputs are strings with `required: false` and `default: ""`: `preview_version`, `preview_tag`, `retention_days`, `evidence_operation`, `evidence_request`, and `promote_request`. Conditional requirements belong to the helper's real `GITHUB_EVENT_PATH` admission, not YAML's unconditional input requirement. The closed input combinations are:

| Admitted mode / operation | Required nonempty inputs | Other inputs |
| --- | --- | --- |
| `build` | `preview_version`, `preview_tag`, `retention_days` | Evidence and promote fields absent or empty. A missing `mode` preserves the legacy three-field build request; an explicitly empty or unknown mode refuses. |
| `evidence` / `retain` | `evidence_request` | Build and promote fields absent or empty; retention belongs inside the request. |
| `promote` | `preview_tag`, `promote_request` | Version, retention and evidence fields absent or empty. Retention comes from Candidate; the tag must equal its destination. |

Each helper invocation closes the actual event inputs and checks command/mode compatibility. It derives source, repository, workflow/run identity, and receipt locations from trusted Actions, API, and Git facts. Unknown fields, unsupported operations, mixed-mode fields, missing required values, or more than 65,535 characters in the full escaped input object refuse. No request JSON is interpolated into a shell command or supplied as a path, URL, or script.

The job routes deliberately include invalid-value fallbacks rather than allowing a successful all-skipped run:

| Job ID / exact display name | `if` expression | Permissions |
| --- | --- | --- |
| `build` / `Build canonical-main preview candidate` | `inputs.mode != 'evidence' && inputs.mode != 'promote'` | `contents: read`, `actions: read`, `pull-requests: read` |
| `retain_evidence` / `Retain canonical-main preview evidence` | `inputs.mode == 'evidence'` | `contents: read`, `actions: read` |
| `promote` / `Promote canonical-main preview candidate` | `inputs.mode == 'promote'` | `contents: write`, `actions: read` |

Keep top-level `permissions: {}` and promote permissions exactly `actions: read` / `contents: write`. Unspecified configurable permissions are none; this does not claim absence of implicit metadata access or artifact-runtime credentials. Retention uses the live job's artifact credentials through `actions/upload-artifact@v7`, not a PAT or a contents grant. Promote uses job-scoped literal `GITHUB_TOKEN: ${{ github.token }}`, with no step token override. Reviewed configuration is not an observed effective-grant map; GitHub policy, rulesets, expiry and endpoint requirements can still refuse a request. `contents: write` is repository-wide; the reviewed publisher's exact-object guards impose the narrower approved tag/asset boundary.

Concurrency uses `stateless-preview-${{ inputs.preview_tag || format('run-{0}-{1}', github.run_id, github.run_attempt) }}` and `cancel-in-progress: false`. Build and promote requests with the same tag share its group; evidence without a tag has a run/attempt-specific group, not an artificial shared empty-tag lock. No additional queue setting is configured. Each admitted run recomputes its source and artifact facts. This serializes our tag workflow, not writes made outside it.

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

The build job is not publication. Do not rename a pre-publication scan to
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

## Source freeze and successor candidate

The accepted authorization amendment replaces the former agent-selected running-job log attestation, not the operator's product or safety constraints. Attempt 3.0 binds reviewed exact-source execution policy; GitHub enforces each real request and Observation 2.0 retains its outcome. There is no replacement permission service, declaration report, probe, credential source or parallel publisher. Attempt admission PASS does not claim observed grants, token introspection or future server acceptance.

Root must finish integrated source verification and canonical-main integration before freezing successor SHA S. Then use the existing canonical build at S for new payload/manifest/Candidate/Catalog, complete retained downloaded-candidate consumer/denial/EOF/unchanged-Python proof, S-bound seven-lens and distinct independent review, passing pre-decision Stage and new explicit Root S4. Policy changes here affect the Catalog; even identical executable bytes would not preserve the source/build/manifest/catalog tuple. Derive a noncolliding preview ordinal from current inventory. Promotion never rebuilds. Internal mechanism selection needs no operator click; Root's exact-candidate decision remains separate and mandatory.

Original commit `5f96014b930e269752b47e878f45f8d220b6bfc7`, build `38960876498/1` and its retained APPROVE remain unchanged and original-only. No newer workflow may check out that source to borrow its approval. Source tests and simulated publication cannot close actual public consumer acceptance.

### Historical permission experiment — superseded admission requirement

Actual [run `38937481596`, attempt 1](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/attempts/1), source/workflow SHA `541b57007b0f56c0bfabe9bd1dd94a20daeac6df` (PR #333), [job `117096512282`](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/job/117096512282), sampled offsets 0.0/60.0 seconds. Both reports were `UNAVAILABLE` / `CURRENT_JOB_LOGS_UNAVAILABLE`, exit 1, with no grants or log hash. Metadata after each sample showed that job `in_progress` with null conclusion; observation completed at 62.062 seconds. These are negative samples, not server denial of publication, universal impossibility, completion-only semantics or an SLA.

Preserve immutable artifact `12061642440` (`stateless-preview-current-job-permissions-38937481596-1`), both original reports and timing summary under `.agent/runs/a1-artifact-20261016/permission-timing-live-38937481596/`, Root's parent `permission-timing-live-root-verdict.json`, earlier negative run `38907673130`, and original positive/negative receipts/private failed scratch. Consumed driver/watchdog source remains in PR #333 history. The collector, its obsolete tests and probe job are now retired after current-job selector migration. Their former requirement is superseded, not passed; run no replacement probe or identical redispatch. No log-attested grant is claimed by the new model.


## Complete publisher source and admission

`promote_request` is strict JSON with exactly `repository`, `run_id`, `artifact_id`, `path`, and `sha256`: a compact retained file reference. `path` must be `promotion-decision.json`; IDs are positive decimal strings and `sha256` is the lowercase raw-file SHA-256. It is not an arbitrary record/path or permission grant. The retained Decision resolves Candidate, Catalog, passing pre-decision Stage and original proof/review records. No actor, source SHA, grants, remote state, attempt/job ID, release ID, or additional artifact reference is accepted from dispatch. `preview_tag` supplies the concurrency group only; the helper compares it with the resolved candidate destination.

One fixed `promote` job uses exact `github.sha` checkout with `persist-credentials: false`, live main fetch, and existing Python 3.12 setup. It has no SDK, build, package installation, Python publication or rebuilt payload. Job-scoped `GITHUB_TOKEN: ${{ github.token }}` reaches every helper. The house transport strips overriding `GH_TOKEN` and enterprise/debug/Git settings, disables auth-cache fallback and protected-write redirects, and binds Git/HTTP authentication to that token. Do not inspect token bytes or infer App/installation IDs.

The caller independently derives repository/run/attempt, unique fixed-name current job ID/setup status, workflow/source SHA and both initiators from Actions, event metadata, native Git and fresh API reads. Candidate source/build, exact checkout, fetched/live main and workflow SHA agree; both initiators equal the Decision's dispatcher. Each command revalidates retained bytes, build success, retention and provenance. `admit-promote` seals a real pre-publication Stage even for an empty descriptor subset; it does not generate `APPROVE` or perform a release write.

Attempt 3.0 `authorization.execution_policy` contains model `GITHUB_JOB_TOKEN_SERVER_ENFORCED`, current `run_attempt`, `job_key: promote`, positive `job_id`, `workflow_sha`, tracked-byte-derived `workflow_blob_sha256` and closed `execution_token` provenance (`github.token`, `GITHUB_APP_INSTALLATION_TOKEN`, `UNOBSERVED`, null `app_id`/`installation_id`). No grants, report/log fields, projection, `permission_path` or policy-report artifact remains. Attempt 1.0/2.0 and obsolete fields refuse without a shim. Chronology is pre-decision ≤ Decision ≤ pre-publication ≤ O0 ≤ C0 ≤ A, before required expiry.

Independently discover complete earlier canonical run/artifact history for this candidate/destination. Null `predecessor_attempt` requires evidence of no earlier admitted Attempt. Otherwise bind its immutable reference and resolve original intent/last outcome; A with no conclusive later evidence is potentially sent. Positive retained step evidence may establish no-send; missing/expired/ambiguous history may not. Read-only reconciliation in new O0 preserves original attempt/operation and known tag OID, never rewrites historical UNKNOWN or resends it. Conflicting predecessor histories refuse. A later independently admitted attempt may retry only a definitely-not-applied refusal after its cause is resolved under fresh context and safe exact state; the original denial remains true.

### Fixed CLI and immutable retention order

```text
python scripts/stateless_preview_artifact.py admit-promote
python scripts/stateless_preview_artifact.py observe-promote
P0 upload: pre-publication-stage.json + remote-observation-0.json
           + pre-publication-verification-0.json only when actually emitted
python scripts/stateless_preview_artifact.py classify-promote --step 0
C0 upload: remote-classification-0.json
python scripts/stateless_preview_artifact.py seal-promote-attempt
A upload: promotion-attempt.json
```

Classification independently reads back P0; Attempt sealing reads back C0 and its nested P0 references. Every advance or finish independently reads back A and the required retained classification/observation chain. No protected write precedes that readback. P0, C0 and A use actual upload IDs/digests, so no artifact-ID cycle exists. A keeps its original O0/C0 references and is never rewritten.

The YAML contains exactly three fixed blocks, `advance-promote --step 1`, `--step 2` and `--step 3`, preserving their existing names for independent predecessor step discovery. Each requires successful immutable A/preceding classification upload and skips `published_complete`. An action creates O_N and optionally a genuine draft-byte fragment; successful upload/readback precedes `classify-promote --step N`, then retained C_N precedes the next action. A failed action may retain O_N, original intents and diagnostics but cannot create an admitted C_N or continue. No `continue-on-error`, loop, retry dispatcher, reusable publisher or service exists.

All uploads use `overwrite: false`, `if-no-files-found: error`, Candidate's validated 1–90-day retention and explicitly enumerated helper-owned files, never scratch-directory/glob uploads. Optional paths are empty when absent. Existing output names remain `stage_path`, `observation_path`, `classification_path`, `attempt_path`, `handoff_path`, `pre_publication_verification_path`, `admission_artifact_name`, `observation_artifact_name`, `classification_artifact_name`, `attempt_artifact_name` and `retention_days`; classification emits `state`/`allowed_action`, command diagnostics use `diagnostics_path`. `permission_path` is removed. Each advance emits `tag_intent_path`, `draft_intent_path`, `archive_intent_path`, `manifest_intent_path`, `publish_intent_path` immediately after create-only original intent creation and before transport. The existing final diagnostic upload lists all five outputs from each of the three blocks, alongside each command's safe `diagnostics_path`; it includes no raw logs, token, signed URL, payload cache or unrestricted directory. Intents are `write-{step}-{tag|draft|upload-archive|upload-manifest|publish}.json`; absent intents have empty outputs. Failure retention does not satisfy an advancement prerequisite.

| Uploaded object | Environment prefix, with `_ID` and `_DIGEST` suffixes |
| --- | --- |
| P0 | `A1_PROMOTE_ADMISSION_ARTIFACT` |
| Current C_N | `A1_PROMOTE_CLASSIFICATION_ARTIFACT` |
| Immutable A | `A1_PROMOTE_ATTEMPT_ARTIFACT` |
| Current O_N, N > 0 | `A1_PROMOTE_OBSERVATION_ARTIFACT` |

Every reference-consuming YAML step removes only the exact leading `sha256:` from v7's `artifact-digest` before invoking the helper. The helper still requires 64 lowercase hex digits; other prefixes, malformed digests or missing real upload IDs refuse. Container digests never replace raw record hashes or the distinct payload hash.

### Classified actions and unknown outcomes

| Retained state | Only permitted action in one fixed block |
| --- | --- |
| `unstarted` | Create the exact annotated tag, verify its known OID, then create the matching draft. |
| `tag_only` | Create only the matching draft. |
| `draft_empty` / `draft_partial` | Upload only missing approved archive/manifest bytes, checking each result. |
| `draft_complete` | Read back the retained pre-publication byte fragment, re-download matching assets, then publish the exact release ID. |
| `published_complete` | No mutation; finish with exact retained references. |
| `collision` or unreadable/mismatched facts | Refuse, never treat unreadability as absence. |

Before each protected write, the helper revalidates current context, retained A/payload, and remote state against the retained classification. The tag uses native Git's known object bytes and a create-only empty lease, not tag movement. Draft creation uses the complete authenticated release listing; binary uploads bind exact release IDs/names/bytes, not a tag-selected wrapper or clobber operation. Publication changes only draft/prerelease/latest metadata after genuine pre-publication byte evidence has been retained. No rebuild, overwrite/delete, tag move, release replacement, auth fallback, or Python/PyPI operation exists.

Each mutation is single-send. Observation 2.0 `operation_results` contains at most two closed results for that block (tag+draft, archive+manifest, or publication); O0 is empty without a predecessor. Results bind `origin_attempt_id`, `operation`, canonical credential-free `intent_sha256`, exact `target`, `sent_at`, `response`, `postcondition` and `outcome`. Expected valid acknowledgement plus exact known tag OID, draft ID/listing, downloaded asset ID/size/hash, or exact published release/assets yields `CONFIRMED`. Only ambiguous/unobserved original response plus complete exact read-only reconciliation yields `RECONCILED`, proving resulting state rather than exclusive sender attribution. Definite refusal remains `DENIED` even after matching foreign state; 409/422 conflict refuses, 403 rate limiting does not prove a missing grant, and 404 does not prove absence. Lost/truncated/malformed/potentially applied responses remain `UNKNOWN` until exact reconciliation; never resend across steps or runs. ACK without exact readback, partial/unreadable/mismatched state, `DENIED`, `COLLISION` or unresolved `UNKNOWN` stops dependent mutation. Preserve 502 starter assets and same-name collisions without deletion. GitHub provides no multi-resource transaction; external authorized writers remain a residual race.

### Exact publisher handoff, not remote consumer proof

`finish-promote --step N` accepts only 0–3, uses the last actually retained classification, revalidates A and fresh published identities, and requires `published_complete` plus admitted operation history. The job retains exactly `publisher-handoff.json` as `<attempt_artifact_name>-handoff`. It contains real compact references for Candidate, Catalog, Decision, both Stages, A, final O/C and any genuine predecessor pre-publication fragment. Execution policy is reached through `promotion_attempt`, not a separate permission-report locator. Bounded diagnostics/original intents are retained separately and cannot admit another action or claim a denied sender performed a later observed operation.

Initial `published_complete` recovery reaches finish at step 0 without any mutation. If no genuine predecessor fragment is available, the handoff's `pre_publication_verification` is null; a new published-byte download never fabricates an earlier PASS. Root must resolve actual predecessor evidence before closing Remote Verification. Missing evidence leaves remote closeout incomplete, not permission to republish or undo the release. T027–T029/T034–T035 still own fresh public downloads into a new directory, complete consumer/denial/EOF/Python rollback proof, Remote Verification, post-publication Stage and the separate non-authorizing Program B Handoff. Publisher caches and this locator file cannot replace those records.

## Post-publication closure — source APIs are not live evidence

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main mismatch, catalog policy drift, an omitted functional or review gate, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal. Sonar audit absence or a real red outcome must not reject an otherwise valid functional candidate.

The existing evidence fixtures preserve original-byte E0/E1/E2 ordering, whole-dispatch budgeting and no automatic `APPROVE`. Authorization fixtures must now prove no-log Attempt 3.0 admission, exact tracked workflow/job/source/token binding, Observation 2.0 outcomes, denial followed by matching foreign state, predecessor crash/missing-history no-resend, and obsolete-field/probe refusal. Root observed pre-implementation causal RED at the old qualified-grants gate and four REST-denial variants falsely succeeding after matching foreign state; existing Git-denial protection passed. These are retained regression evidence, not integrated GREEN. Root owns the combined focused checks after implementation; source fixtures do not establish canonical publication, fresh public consumer proof or unchanged installed-Python rollback.
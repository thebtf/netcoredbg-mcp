# Stateless preview workflow contract

The current `.github/workflows/stateless-preview.yml` implements manual `build`, `evidence`, and `promote` dispatch for the A1 preview artifact. Evidence dispatch selects either nonpublishing retention or a same-current-job permission probe; promote is a complete publisher source path, not an admission-only placeholder. The workflow runs only through `workflow_dispatch`, never from `push`, tag push, `workflow_run`, a schedule, or a Python release event. Only an admitted promote job can create the candidate's annotated tag, draft prerelease and missing assets, then publish that prerelease. Source implementation is not live approval or an execution receipt. Python and Program B/C remain unchanged.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

`mode` is a choice with `build`, `evidence`, and `promote`, defaulting to `build`. The other six inputs are strings with `required: false` and `default: ""`: `preview_version`, `preview_tag`, `retention_days`, `evidence_operation`, `evidence_request`, and `promote_request`. Conditional requirements belong to the helper's real `GITHUB_EVENT_PATH` admission, not YAML's unconditional input requirement. The closed input combinations are:

| Admitted mode / operation | Required nonempty inputs | Other inputs |
| --- | --- | --- |
| `build` | `preview_version`, `preview_tag`, `retention_days` | Evidence and promote fields absent or empty. A missing `mode` preserves the legacy three-field build request; an explicitly empty or unknown mode refuses. |
| `evidence` / `retain` | `evidence_request` | Build and promote fields absent or empty; retention belongs inside the request. |
| `evidence` / `permission-probe` | None | Request, build and promote fields absent or empty; the helper admits a fixed seven-day diagnostic retention. |
| `promote` | `preview_tag`, `promote_request` | Version, retention and evidence fields absent or empty. Retention comes from Candidate; the tag must equal its destination. |

Each helper invocation closes the actual event inputs and checks command/mode compatibility. It derives source, repository, workflow/run identity, and receipt locations from trusted Actions, API, and Git facts. Unknown fields, unsupported operations, mixed-mode fields, missing required values, or more than 65,535 characters in the full escaped input object refuse. No request JSON is interpolated into a shell command or supplied as a path, URL, or script.

The job routes deliberately include invalid-value fallbacks rather than allowing a successful all-skipped run:

| Job ID / exact display name | `if` expression | Permissions |
| --- | --- | --- |
| `build` / `Build canonical-main preview candidate` | `inputs.mode != 'evidence' && inputs.mode != 'promote'` | `contents: read`, `actions: read`, `pull-requests: read` |
| `retain_evidence` / `Retain canonical-main preview evidence` | `inputs.mode == 'evidence' && inputs.evidence_operation != 'permission-probe'` | `contents: read`, `actions: read` |
| `probe_permissions` / `Probe current job permissions` | `inputs.mode == 'evidence' && inputs.evidence_operation == 'permission-probe'` | `contents: write`, `actions: read` |
| `promote` / `Promote canonical-main preview candidate` | `inputs.mode == 'promote'` | `contents: write`, `actions: read` |

Set top-level `permissions: {}`. The probe's declared write grant exists only to observe its own runner-reported permission map; no remote write or test mutation is connected. Retention uses the live job's artifact runtime credentials through `actions/upload-artifact@v7`, not a PAT or `contents: write`. Helper and diagnostic tokens are available only as `GITHUB_TOKEN` environment values, never arguments or record contents.

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

## Source freeze and permission capability (pending)

The build/evidence routing, four-command retention adapter, and diagnostic job are source implementation, not a final candidate or live execution receipt. Root must run the focused integrated fixtures and the bounded live probe/retention routes. Complete all required future publisher/admission code before freezing a final canonical candidate. Bind candidate commit, workflow SHA, exact checkout, and canonical-main provenance; a newer workflow checking out old source does not satisfy this. Subsequent source changes require fresh candidate-bound consumer proof, all seven lenses, distinct PR review, and an explicit retained Decision. Source fixtures, classifier evidence, and historical consumer proof do not approve a held or final candidate.

The one-time permission timing experiment completed negatively in actual
[run `38937481596`, attempt 1](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/attempts/1),
source/workflow SHA `541b57007b0f56c0bfabe9bd1dd94a20daeac6df` (merged PR #333),
[job `117096512282`](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/job/117096512282).
Samples at observed offsets 0.0 and 60.0 seconds both returned `UNAVAILABLE` /
`CURRENT_JOB_LOGS_UNAVAILABLE`, exit 1, no parsed grants and no log hash. Bounded
metadata reads after each sample confirmed that same job `in_progress` with
null conclusion. The observation completed at 62.062 seconds; no qualified
current-job grant was obtained, and live capability remains `UNKNOWN`.

Root verified the original report hashes. Preserve immutable artifact
`12061642440` (`stateless-preview-current-job-permissions-38937481596-1`) and
its original two collector reports plus timing summary. Local bytes remain in
`.agent/runs/a1-artifact-20261016/permission-timing-live-38937481596/`, with
`permission-timing-live-root-verdict.json` in its parent directory. The earlier
negative run `38907673130`, positive/negative verification receipts, original
collector and private failed scratch remain unchanged. The consumed one-time
driver, watchdog and their two tracked tests are retired with source preserved
in PR #333 / the merge commit above. No experimental retry/wrapper remains in
the workflow; retirement is lifecycle completion, not permission or release PASS.

Re-entry requires a new provider contract/change or a source-backed supported
same-current-job interface that changes the capability basis. No identical
redispatch, third sample, extended observation window, PAT/auth-cache fallback
or declaration-only permission substitute is authorized. These observed results
prove neither universal impossibility, completion-only semantics nor an SLA.
Dependent real admission/mutation remains held until qualified same-current-job
readback; independent complete-publisher source work continues.

The canonical manual nonmutating route remains this workflow at `main`,
`mode=evidence`, `evidence_operation=permission-probe`, with empty request/build
fields. Availability of that route is not authority to repeat the consumed
experiment. Source/actor admission, exact checkout/canonical workflow/main
binding, declared `actions: read`/`contents: write`, fixed
`GITHUB_TOKEN: ${{ github.token }}` wiring and admitted seven-day retention are
unchanged. No SDK/build/retention-chain/publication commands are connected to
the probe job, and the separate complete publisher job remains unchanged.

The restored probe invokes exactly one unchanged collector:

```text
python scripts/stateless_preview_permissions.py --output current-job-permissions.json
```

The manual single-sample CLI remains valid only in its trusted Actions context.
The collector retains fixed `GITHUB_JOB` mappings `probe_permissions` →
`Probe current job permissions` and `promote` →
`Promote canonical-main preview candidate`, with no job-name override. Exact
workflow/ref/source/run/attempt/job binding, running-job guards before and after
log retrieval, native step-log → same-job-log fallback, zero-based setup position,
45-second worker deadline with kill/reap on timeout, ten-second socket timeout,
1 MiB log bound, redirect safety and exclusive output creation are unchanged.
DNS remains bounded by the outer worker deadline, not individually by ten
seconds. Reports retain numeric IDs, `job_key`, `workflow_sha`, actual HTTP/log
facts and parsed grants; raw logs, tokens and signed URLs are not retained.
Only actual `actions: read` plus `contents: write` grants yield `QUALIFIED` /
exit zero; unavailable, insufficient or parser-refused evidence remains nonzero
and cannot authorize admission or mutation. A probe-job report cannot qualify
the separate promote job.

The immutable upload again lists only `current-job-permissions.json` under
`stateless-preview-current-job-permissions-{run_id}-{run_attempt}`. It uses
`always()` only after actual probe success/failure, not skipped admission/probe,
and retains `overwrite: false`, `if-no-files-found: error` and admitted seven-day
retention, without `continue-on-error`. Only the retired watchdog's output gate
is removed; no collector bound or authority check is relaxed. Raw logs, tokens,
signed URLs, internal payloads and Diag remain excluded. The meaning remains
`RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION`, not approval or publication.

A successful probe shows only observed availability of that job's runner-reported grant, not universal API availability or token introspection. Every real attempt still needs its own actual readback. Missing, ambiguous, wrong-job/attempt, or insufficient grants refuse before mutation. YAML, repository defaults, endpoint-required permission headers, successful GETs, and another job's grant are not substitutes. A failed bounded probe does not prove global impossibility or authorize falling back to declarations.

Attempt 2.0 now separates the Decision author/authorized dispatcher, both current initiator ID/login maps, and execution-token provenance. Both initiators must equal the resolved Decision dispatcher; the evidence adapter's S4-author allowlist does not become a token principal or an extra dispatcher restriction. Trusted future wiring must use `GITHUB_TOKEN: ${{ github.token }}`; record `source: github.token`, `kind: GITHUB_APP_INSTALLATION_TOKEN`, `identity_observation: UNOBSERVED`, `app_id: null`, and `installation_id: null`. Never infer IDs, introspect token bytes, or fall back to a PAT/auth cache. Unknown live capability holds dependent admission/mutation, not independent evidence retention or classifier work.

## Complete publisher source and admission

`promote_request` is strict JSON with exactly `repository`, `run_id`, `artifact_id`, `path`, and `sha256`: a compact retained file reference. `path` must be `promotion-decision.json`; IDs are positive decimal strings and `sha256` is the lowercase raw-file SHA-256. It is not an arbitrary record/path or permission grant. The retained Decision resolves Candidate, Catalog, passing pre-decision Stage and original proof/review records. No actor, source SHA, grants, remote state, attempt/job ID, release ID, or additional artifact reference is accepted from dispatch. `preview_tag` supplies the concurrency group only; the helper compares it with the resolved candidate destination.

One fixed `promote` job uses exact `github.sha` checkout with `persist-credentials: false`, live main fetch, and the existing Python 3.12 setup. It has no SDK, build, package installation, Python publication or rebuilt payload. Job-scoped `GITHUB_TOKEN: ${{ github.token }}` reaches every helper/collector invocation. The house transport strips overriding `GH_TOKEN` and enterprise/debug/Git settings, disables auth-cache fallback and redirects for protected writes, and binds Git/HTTP authentication to that token. No PAT or token-owner inference is permitted.

The caller derives the current repository/run/attempt, job ID, workflow/source SHA and both initiators from Actions, native Git and fresh API reads independently of the permission report. Candidate source/build, exact checkout, fetched and live main, and workflow SHA must agree; both initiators must equal the retained Decision's authorized dispatcher. Each command revalidates the retained chain, raw hashes, build success, retention and provenance. `admit-promote` seals a real pre-publication Stage even for the empty descriptor subset. It does not generate `APPROVE`, qualify permissions or authorize a write by itself.

The collector runs once in this same promote job at the helper-owned `permission_path`. Only `QUALIFIED`/exit zero permits observation, C0, Attempt admission, or a protected action. Missing/ambiguous/foreign/insufficient/unavailable grants refuse. A separate `always()` upload may preserve the original failed collector report without C0, Attempt or release writes. Actions uploads use their artifact runtime credentials: they are neither protected release mutations nor evidence of a `contents: write` grant. Capability remains `UNKNOWN` until qualified live proof exists; that holds dependent effects, not source work. The earlier probe section describes its separate diagnostic lane, which remains unchanged by this publisher addition.

### Fixed CLI and immutable retention order

```text
python scripts/stateless_preview_artifact.py admit-promote
python scripts/stateless_preview_permissions.py --output <admit.permission_path>
python scripts/stateless_preview_artifact.py observe-promote
P0 upload: pre-publication-stage.json + current-job-permissions.json + remote-observation-0.json
           + pre-publication-verification-0.json only when actually emitted
python scripts/stateless_preview_artifact.py classify-promote --step 0
C0 upload: remote-classification-0.json
python scripts/stateless_preview_artifact.py seal-promote-attempt
A upload: promotion-attempt.json
```

Classification independently reads back P0; Attempt sealing reads back C0 and its nested P0 references. Every advance or finish independently reads back A and the required retained classification/observation chain. No protected write precedes that readback. P0, C0 and A use actual upload IDs/digests, so no artifact-ID cycle exists. A keeps its original O0/C0 references and is never rewritten.

The YAML contains exactly three fixed advancement blocks, `advance-promote --step 1`, `--step 2`, and `--step 3`. Each runs only after successful permission collection and the preceding successful classification upload, and skips once `published_complete` is reached. An action creates O_N and optionally a genuine draft-byte fragment; upload/readback precedes `classify-promote --step N`, then C_N is uploaded before the next action. A failed action may retain its diagnostic O_N but cannot create an admitted C_N or continue. There is no loop, retry dispatcher, redispatch, reusable action or service.

All uploads use `overwrite: false`, `if-no-files-found: error`, Candidate's validated 1–90-day retention and explicit helper-owned file paths, never a scratch directory/glob. Optional fragment paths are empty when no fragment exists. The exact output contract is `stage_path`, `permission_path`, `observation_path`, `classification_path`, `attempt_path`, `handoff_path`, optional `pre_publication_verification_path`, `admission_artifact_name`, `observation_artifact_name`, `classification_artifact_name`, `attempt_artifact_name`, and `retention_days`; classification also emits `state` and `allowed_action`, while command diagnostics use `diagnostics_path`.

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

Each mutation is sent once. A lost/error response allows only bounded read-only reconciliation: exact known tag OID, matching draft listing/ID, downloaded asset ID/size/hash, or exact published release and assets. Continue only when the complete postcondition is proven. Unknown, partial or changed state stops that attempt without another write; a retry requires a new run/run_attempt, independently derived attempt ID, fresh Stage/report/O/C/A and retained-byte validation. HTTP errors alone do not prove absence. GitHub provides no transaction across tag, release, assets and publication; concurrent changes by another authorized writer remain a residual risk.

### Exact publisher handoff, not remote consumer proof

`finish-promote --step N` accepts only 0–3, uses the last actually retained classification, revalidates A and fresh published identities, and requires `published_complete`. The job retains exactly `publisher-handoff.json` as `<attempt_artifact_name>-handoff`. It contains real compact references for Candidate, Catalog, Decision, both Stages, the permission report, A, final O/C and any retained predecessor pre-publication fragment. Bounded command diagnostics are retained separately; they cannot admit another action.

Initial `published_complete` recovery reaches finish at step 0 without any mutation. If no genuine predecessor fragment is available, the handoff's `pre_publication_verification` is null; a new published-byte download never fabricates an earlier PASS. Root must resolve actual predecessor evidence before closing Remote Verification. Missing evidence leaves remote closeout incomplete, not permission to republish or undo the release. T027–T029/T034–T035 still own fresh public downloads into a new directory, complete consumer/denial/EOF/Python rollback proof, Remote Verification, post-publication Stage and the separate non-authorizing Program B Handoff. Publisher caches and this locator file cannot replace those records.

## Future post-publication closure (not implemented)

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main mismatch, catalog policy drift, an omitted functional or review gate, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal. Sonar audit absence or a real red outcome must not reject an otherwise valid functional candidate.

The supplied `test_evidence_cli_*` fixtures exercise the four-command boundary, original-byte retention, full escaped-input budget, actor/rerun and source-build provenance, ordered E0/E1/E2 readback, request substitution, and no automatic `APPROVE`. Root's pre-implementation dispatch run failed the unknown-mode/unknown-input refusal assertions because the old helper returned `READY`/zero, as recorded in `.agent/runs/a1-artifact-20261016/evidence-cli-dispatch-red.xml`. This is the intended source RED, not an integrated PASS. Root must run the focused owner after implementation lands. Fixture success does not complete the same-job capability probe, live retention, final candidate, or publisher.
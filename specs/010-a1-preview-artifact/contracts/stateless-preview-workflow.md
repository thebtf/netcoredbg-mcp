# Stateless preview workflow contract

The current `.github/workflows/stateless-preview.yml` is one executable manual `build` surface for the A1 preview artifact. It runs only through `workflow_dispatch`, never from `push`, tag push, `workflow_run`, a schedule, or a Python release event. It creates no tag, release, asset outside retained Actions artifacts, Python change, or Program B/C change. The accepted target adds `evidence` and `promote` modes to this same workflow, not another workflow/controller. Neither mode is implemented in the current YAML; it has no promotion input, job, placeholder, or remote-mutation path.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

The current build-only workflow accepts exactly three caller inputs, all as strings. It validates every input before using it in a path, API call, artifact name, or record. It derives source, repository, workflow identity, run identity, and every receipt location from trusted Actions and Git facts. The following table describes only that executable surface, not the future multi-mode dispatch interface.

| Executable mode | Inputs |
| --- | --- |
| `build` | `preview_version`, `preview_tag`, and `retention_days`. The workflow derives the source from the current canonical `refs/heads/main` ref. It has no source-commit, receipt, artifact-ID, or promotion input. |

Set top-level `permissions: {}`. The current build job declares `contents: read`, `actions: read`, and `pull-requests: read`; it gets no Python publication, package registry, cross-repository, or write permission. The future evidence job requires `contents: read` and `actions: read`; grant `pull-requests: read` only for actual PR API reads. Artifact upload uses the live job's runtime credentials through the upload action, not a local PAT or `contents: write`.

The workflow has one concurrency group per preview tag and `cancel-in-progress: false`. GitHub Actions supplies no `queue: max` workflow key, so the executable workflow does not claim one; each admitted run still recomputes every source and artifact fact before sealing its candidate.

## Build from canonical main

The build job must do the following in order:

1. Fetch live `origin/main`. Require `github.ref`, the workflow ref, `github.sha`, checkout `HEAD`, and the resolved `origin/main` target to bind one canonical `refs/heads/main` commit.
2. In a clean detached functional worktree at that commit, use the existing Sonar-free `build/coverage.sh` producer through its supported `run_coverage_producer` owner. Require the solution and standalone builds, Python functional suite, and all five .NET producer suites to pass; validate the reports and restored stateless binary hashes. Preserve exact-head, tracked Wave2 entry, source-cleanliness, and cleanup guards. Do not start a scanner, load Sonar credentials, or produce a Sonar receipt in this build-only workflow.
3. Publish only `NetCoreDbg.Mcp.Stateless.Preview` as a self-contained, single-file Windows x64 executable.
4. Create the archive and inherited manifest. Validate its equations and hash archive, manifest, and extracted executable.
5. Upload the payload as a new Actions artifact with `if-no-files-found: error`, bounded retention, and `overwrite: false`.
6. Discover its current-run artifact ID and expiry facts through the GitHub Actions API, then seal the Candidate Identity Record and static Release Gate Catalog through `scripts/stateless_preview_artifact.py` without a Sonar receipt.
7. Upload Candidate Identity and Release Gate Catalog as separate new immutable Actions artifacts with the same bounded retention and `overwrite: false`.

The build job must not build a Python wheel, call `publish.yml`, use PyPI tooling, change the default selector, accept an arbitrary source or receipt, or create a promotion input.

This workflow is not publication. Do not rename a pre-publication scan to
"postrelease" or run it asynchronously from this build. The existing standalone
`scripts/run_sonarqube_exact_head.py --role post-merge` route is a strict audit
only after actual publication, as described in
[`docs/SONARQUBE-ONBOARDING.md`](../../../docs/SONARQUBE-ONBOARDING.md). Preserve
its real red or blocked outcome; it does not authorize or block promotion.

## Accepted evidence mode (not implemented)

The accepted target keeps three operations in this workflow: `build` produces the candidate, `evidence` retains proof/review and its explicit Decision, and `promote` consumes that retained Decision. Evidence mode does not build a payload or mutate tags, releases, or assets. The ordinary GitHub Artifacts REST API is not a local-file upload route; use the existing `actions/upload-artifact@v7` inside a live job.

1. Require the original build run to have concluded successfully before independent consumer proof or review. Do not wait for these records in that original build: the Candidate Identity requires a completed successful run first.
2. Accept exact Candidate Identity/Catalog artifact-file references, the original three records and raw hashes, an explicit candidate-bound `APPROVE` or `DECLINE` intent naming that evidence and destination, one authorized dispatcher, and bounded retention. The intent is conditional on successful validation; it is not the completed S4 Decision and must not contain a future Stage reference or future decision time. Only after the actual Stage exists may step 6 seal the explicit Decision with its actual Stage/review references, decision time, and destination. Derive `decision_author` from actual authenticated run/event actor metadata and applicable authority for this candidate, not a caller-supplied identity or GitHub write access alone. Bind the existing operator principal `thebtf`/`7106373` from actual run metadata and check both numeric ID and login for the actor and rerun initiator; this binding grants no new scope. Preserve the real producers, checks, findings, and timestamps; transport is not a rerun of their work.
3. Transfer the three original byte sequences as one bounded base64 ZIP in string dispatch inputs. Enforce the ingress limits below and validate candidate/provenance/author/review semantics before retention/sealing; successful byte decoding alone does not establish those semantics.
4. Upload those three original files together as one immutable artifact with `overwrite: false`, `if-no-files-found: error`, and bounded retention. Only after upload, resolve the real artifact ID, repository/run ownership, and expiry, independently download the saved files, verify their raw hashes, and form `repository, run_id, artifact_id, path, sha256` references. Keep original evidence outside tracked source; artifact names/job outputs are not sealed references.
5. Revalidate the actual consumer proof, complete seven-lens aggregate, distinct independent PR review, and Catalog through the existing Stage sealer. Upload the passing pre-decision Stage as a separate immutable artifact; obtain its real reference/raw hash after upload.
6. Seal the author's explicit Decision against that existing Stage, preserve decision/stage time order, and upload the Decision as a third immutable artifact. `PASS` alone must never generate `APPROVE`; missing lenses or unresolved blocking findings cannot be synthesized away. Decision names the authorized dispatcher but no future promote run, current-job permission readback, or remote state.
7. After the evidence run completes, a separate promote run may consume the already retained Decision reference. No record requires its own or another not-yet-uploaded object's ID/hash.

The [S4 author boundary](../../../docs/RELEASE-PROTOCOL.md#a1-preview-channel-promotion-s4-only) requires a separate after-evidence decision, not a human-only gate. Under the accepted autonomous A1 mandate, Root may decide as a delegated executor; its workflow call is not evidence of the operator's personal review or click. The mandate, allowlist, actor identity, and passing checks do not automatically create `APPROVE` or pre-approve unknown future bytes. A distinct explicit `authorized_dispatcher` remains possible only within the existing closed Decision contract. Actual numeric actor admission and evidence/promote execution are still pending; this interpretation creates no current S4 record, promotion authority for another run/scope, or Program B/C scope. Required review closure, including FR-006, remains unchanged.

The first ingress slice is byte ingress only: `decode_evidence_bundle` is implemented, not an executable evidence workflow, artifact resolver, or live retention proof. It accepts exactly `consumer-proof.json`, `s2-s3-review.json`, and `independent-pr-review.json`; it preserves original bytes/raw hashes and requires strict finite JSON. Base64 is at most 60,000 characters; decoded ZIP is at most 45,000 bytes; each member is at most 128 KiB and their total at most 256 KiB.

The protected boundary is the actual three returned records, not universal ZIP-format conformance. Refuse directory-parsing failures, duplicate/unexpected members, unsafe file metadata or paths, unsupported codecs/flags, inconsistent supported local-header/descriptor metadata, and violated actual byte/CRC/hash/JSON bounds. Allow only `ZIP_STORED` or `ZIP_DEFLATED`; the supported general flags are UTF-8, data descriptors, and DEFLATE-only compression options. Require complete DEFLATE EOF and matching actual sizes/CRC; cross-check supported local/central fields and signed or unsigned 32-bit descriptors. Local ZIP64 size markers are unsupported. Read fixed members without arbitrary extraction or shell-code interpolation. Unused legacy EOCD disk/count declarations are not independently conformance-checked: the library's actual enumerated member set must still be exactly the three fixed names, and every returned member must pass all content bounds and identities. This input ZIP is not executed or redistributed.

The historical standalone owner before classifier integration passed `409/409` in `.agent/runs/a1-artifact-20261016/evidence-ingress-header-fixed.xml`; the subsequent integrated owner passed `533/533` in `classifier-ingress-integrated.xml`. The supported-flags correction has its own complete `544/544`, zero-error/failure/skip receipt in `evidence-ingress-supported-flags-fixed.xml`. Historical receipts do not prove later source changes. The actual API smoke in `evidence-ingress-supported-flags-api-smoke.json` exercises supported byte preservation and unsupported-flag refusal; earlier smoke separately covers immutable mapping and raw-hash tamper refusal. Synthetic `FAIL`/`INCOMPLETE` records are transported unchanged, not approved. These checks neither close independent review findings by themselves nor expand schemas or replace candidate/review admission.

Separately, the complete dispatch must fit the [65,535-character total input limit](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch), including every reference, hash, decision/dispatcher field, retention value, and mode input. The decoder's 60,000-character bound does not prove that whole budget. Oversize input refuses explicitly; do not truncate checks, rewrite evidence, or substitute an inaccessible local path. Transport/live retention acceptance remains pending until independent download resolves all three uploads and reproduces original raw hashes.

`reference.sha256` hashes the referenced raw file, not GitHub's `artifact-digest` container. The current helper's `candidate.build.artifact.sha256` is `SHA256(archive_bytes + manifest_bytes)`, another distinct quantity; do not compare it with a container digest or change its schema for this ingress.

## Source freeze and permission capability (pending)

Complete and fixture-check all required pipeline code, including promote admission and explicit `github.workflow_sha` checks, before a new final canonical candidate. Bind candidate commit, workflow SHA, checkout, and required canonical-main provenance; a newer workflow checking out an old source does not satisfy this. Changes require fresh candidate-bound consumer proof, all seven lenses, distinct PR review, and an explicit retained Decision. Source fixtures, classifier evidence, and retained-candidate consumer proof each keep their own scope; none closes the whole attempt or approves a held candidate.

Actual current-job permission-readback availability before mutation is UNKNOWN. The accepted next step is one bounded nonmutating probe in a separate trusted manual non-fork job of this same workflow, before final freeze. Declare `actions: read` and `contents: write` for that probe, but connect no tag/release/asset commands and perform no test write. Resolve the exact current run/attempt/job and completed `Set up job` step; while that same job is `in_progress`, request `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/steps/{step_number}/logs` with the documented zero-based step position, then `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs` if step logs are unavailable. Retain result/time, run/attempt/job/SHA binding, and the extracted runner permission map, never tokens or signed download URLs.

A successful probe shows only observed availability of that job's runner-reported grant, not universal API availability or token introspection. Every real attempt still needs its own actual readback. Missing, ambiguous, wrong-job/attempt, or insufficient grants refuse before mutation. YAML, repository defaults, endpoint-required permission headers, successful GETs, and another job's grant are not substitutes. A failed bounded probe does not prove global impossibility or authorize falling back to declarations.

Keep dispatcher identity separate from the `GITHUB_TOKEN` GitHub App installation token. Validate both numeric ID and login from actual run/event metadata for `github.actor` and the rerun initiator `github.triggering_actor` against the authorized dispatcher; do not admit another person's rerun using only the original actor. The current Attempt schema's human-shaped `authenticated_actor` needs implementation-stage schema/code correction and exact job/run/attempt binding; do not fill it with a human as the token principal or invent unavailable App/installation IDs. That correction, `data-model.md`, `quickstart.md`, and recovery-contract alignment remain outside this documentation slice. Unknown permission capability holds dependent admission/mutation only, not evidence retention or independent classifier work.

## Future promotion boundary (not implemented)

The future `promote` mode in this workflow may admit promotion only after a retained Promotion Decision consumes a passing `pre-decision` Stage Gate Evidence record through the ordering above. The Decision must name one authorized GitHub dispatcher, but it must not name a historical consuming run or require future-stage evidence.

Before any later remote mutation, that future surface must create a fresh Promotion Attempt and verify current dispatcher and rerun initiator, run ID, run attempt, exact job, canonical main ref/SHA, workflow SHA, actual current-job permission readback, Decision, and pre-decision Stage Gate Evidence. It must also require passing `pre-publication` Stage Gate Evidence. A different dispatcher, old attempt, static-equivalent permission, arbitrary commit, failed stage record, or changed source/workflow must refuse before mutation.

The current `pre-publication` descriptor subset is empty. Its stage evidence
still binds the candidate and catalog. Sonar is not a seal or `PASS` dependency;
the fresh source, provenance, permission, and remote-state checks remain required.

The future attempt must not require `post-publication` evidence. That evidence can exist only after a prerelease is public.

## Recover matching remote state

The fresh attempt creates a Remote Observation and a Remote Classification. The classifier admits only `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, or `collision`.

The workflow creates an annotated tag only for `unstarted`. It creates only the draft prerelease for `tag_only`. It uploads only missing matching assets for `draft_empty` or `draft_partial`. It verifies draft assets before it publishes the prerelease. A retry uses a new attempt and fresh matching classification.

The workflow never rebuilds a candidate, uses `--clobber`, overwrites or deletes an asset, moves/deletes/reuses a tag, deletes/replaces a release, invokes Python packaging, or publishes to PyPI.

## Close after publication

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main mismatch, catalog policy drift, an omitted functional or review gate, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal. Sonar audit absence or a real red outcome must not reject an otherwise valid functional candidate.

Within the same owner, add evidence-ingress/retention cases for all byte and whole-dispatch limits, unsafe/duplicate/encrypted ZIP members, original-byte preservation, real post-upload references, expiry/provenance mismatches, explicit-author Decision ordering, and refusal of automatic `APPROVE`. Add missing/wrong-attempt/job/grant and dispatcher/rerun cases for the future admission correction. Fixture success does not complete the same-job capability probe or live artifact-retention proof.
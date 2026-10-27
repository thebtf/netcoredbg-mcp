# Stateless preview workflow contract

The current `.github/workflows/stateless-preview.yml` implements manual `build`, nonpublishing `evidence` retention, and `promote` dispatch for A1. The retired `permission-probe` selector is rejected, not aliased. This single canonical publisher runs only through `workflow_dispatch`, never `push`, tag push, `workflow_run`, schedule or Python release events. Only an admitted promote job can create the approved annotated tag, matching draft and missing assets, then publish the prerelease. Source implementation is not live approval or an execution receipt. Python and Program B/C remain unchanged.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## A2 successor proof generation

This existing channel also carries the A2 read-only family; it is not a new publisher or Python/default release. A2 introduced manifest/Candidate schema `2.0`, whose closed `consumer_contract` binds `scenario_catalog_id: a2-preview-readonly-family-v1`, the SHA-256 of that fixed canonical scenario catalog, and exactly `[find_code_symbol, find_code_references, get_source_context]`, in that order. Historical A2 identities retain these obligations. Current source producers follow the A3 legacy-diagnostic successor below; there is no build input or environment selector for proof generation.

Schema `1.0` identities and manifests always mean the historical `a1-preview-inherited-matrix-v1` ten-scenario, one-tool contract. Their original bytes and receipts are not rewritten. Manifest and Candidate schema generations must agree; unknown generations, mixed IDs/hashes/tool lists and schema-2 manifests without the fixed contract refuse. The no-argument `consumer_proof_scenario_catalog()` API remains A1 for active historical callers; the current consumer explicitly selects the generation from the verified immutable candidate. That compatibility remains until historical A1 verification is explicitly retired; no A1 proof is valid for a successor identity.

Receipt schema `1.0` and reference-bundle schema `1.0` retain their existing shapes. Both retained and public/remote sealers first verify the raw Candidate identity against its downloaded reference, then derive the only allowed catalog ID/hash, ordered scenario matrix and exact runtime tool list from that Candidate. Receipt fields cannot choose a validator. Public acquisition, stage/review, promotion and closeout preserve their existing exact-byte/provenance paths; they admit the new Candidate generation without reconstructing it as A1.

The A2 catalog inherits the A1 authority, stdout, protocol, containment, EOF/cancellation and unchanged installed-Python rollback obligations. It adds real reference/context value journeys, invalid input, direct excluded/ignored/unsupported/ambiguous/outside/reparse/locked source paths, strict result/file budgets and route-attributed complete-frame refusals. Rollback runs last. The actual discovery catalog has exactly three closed read-only/idempotent tools. `find_code_references` defaults `max_results` to 1000, but strict admission still caps results at 128: a request below 128 can stop early; a request at/above 128 refuses if a 129th match exists. `get_source_context` uses a positive int32 `line` and nonnegative int32 `radius` (default 10), preserves indentation and returns numbered, file-clipped lines. Explicit null, wrong types and unknown properties refuse on their requested route. Success/error text must parse to the exact structured payload; no partial source may accompany a refusal. The raw full JSON-RPC response including the request ID must fit 256 KiB. Route shapes remain owned by [`specs/016`](../../016-a2-native-readonly-family/spec.md#route-contracts).

The downloaded A2 consumer compares every advertised input/output schema with the complete source-frozen `tests/preview/a2-tool-schemas.json` contract before its valid tool calls. It validates all three valid-journey inputs and successful structured values using the advertised schemas; the remaining A2 matrix successes use those same frozen output schemas. This includes nested types, required fields, bounds, enums, defaults and closed-property rules, not only catalog names or top-level flags. Existing text/structured equality and exact value assertions remain independent checks. JSON Schema validation uses the project's existing `jsonschema` dependency (locked through `mcp`); no install or dependency change is performed by the consumer. That import is lazy and A2-only, so standalone help and historical A1 schema semantics do not acquire a new requirement. Schema/dependency refusals remain redacted and cannot create a passing receipt.

### Exact producer and consumer commands

Root runs these only after integrated source checks. The canonical Actions build continues to use `python scripts/stateless_preview_artifact.py admit-build`, the existing self-contained publish/runtime check, `python scripts/stateless_preview_artifact.py prepare-payload`, immutable payload upload and `python scripts/stateless_preview_artifact.py seal-build-records`; all source/build/main checks remain mandatory. The consumer generation is fixed by the helper bytes at that admitted source commit, not preview ordinal or `serverInfo.version`.

For the existing local source-run artifact proof, use fresh publish/artifact directories and the exact source SHA. These are PowerShell commands with caller-owned paths substituted before execution:

```text
dotnet publish host/NetCoreDbg.Mcp.Stateless.Preview/NetCoreDbg.Mcp.Stateless.Preview.csproj --configuration Release --runtime win-x64 --self-contained true -p:PublishSingleFile=true -p:PublishTrimmed=false --output <fresh-publish>
Rename-Item -Path <fresh-publish>/NetCoreDbg.Mcp.Stateless.Preview.exe -NewName netcoredbg-mcp-stateless-preview.exe
python build/prepare_preview_fixture.py --executable <fresh-publish>/netcoredbg-mcp-stateless-preview.exe --output <fresh-source-artifact> --commit <exact-source-sha> --version <noncolliding-preview-version>
```

Set `NETCOREDBG_PREVIEW_ARTIFACT_ROOT` only in the child environment for the existing `PreviewArtifactConsumerTests` process proof, pointing to that fresh source artifact. Set `DOTNET_ADD_GLOBAL_TOOLS_TO_PATH=false` in the SDK child environment before any SDK invocation. This local fixture is source-run evidence, never retained/published Candidate authority or a release receipt.

After canonical build completion, the real retained and fresh public consumer reentry is unchanged:

```text
python tests/preview/validate_preview_artifact.py candidate --references <candidate-references.json> --download-root <fresh-existing-download-parent> --fixture-root tests/fixtures/PreviewSearchApp --receipt-output <new-retained-receipt.json> --receipt-id <opaque-id> --recorded-at <UTC-timestamp> --python-rollback-command <installed-unchanged-Python-oracle> [--python-rollback-argument=<argument> ...]
python tests/preview/validate_preview_artifact.py remote --references <same-exact-candidate-references.json> --release-id <published-release-id> --download-root <fresh-existing-download-parent> --fixture-root tests/fixtures/PreviewSearchApp --receipt-output <new-remote-receipt.json> --receipt-id <new-opaque-id> --recorded-at <UTC-timestamp> --python-rollback-command <installed-unchanged-Python-oracle> [--python-rollback-argument=<argument> ...]
```

References are the existing closed Actions reference bundle, not caller paths to an executable or payload. The remote command downloads the public archive/raw manifest afresh and cannot fall back to Actions payload bytes. Its rollback executable must emit the existing `PRODUCT_WORKS` oracle marker after the real installed Python journey; a successful exit code alone is not proof. Neither command changes protected public environments, retained refs/data, Python installation/defaults or publication authority. A new exact-candidate promotion decision and fresh remote-byte proof remain required.

## Historical A3 optional client information successor

The source successor specified in [A3 optional client info](../../017-a3-optional-client-info/spec.md) keeps manifest/Candidate schema `2.0`, receipt/reference schema `1.0` and the same three tools. Its exact closed contract is `a3-preview-optional-client-info-v1`, the canonical hash of its fixed 16 scenarios, and the ordered tool list. Producers at that historical source required the complete optional-A3 contract and refused historical A1 and A2 payloads even when their hashes were valid.

Historical schema-1 A1 and schema-2 A2 definitions, hashes and receipts retain their original meaning. Candidate metadata, not receipt fields or preview ordinal, chooses the exact known A1/A2/A3 proof obligations. Retained and remote sealers refuse cross-generation substitution, missing/extra/reordered cases, incorrect counts/outcomes and mixed ID/hash/tool fields. Old A2 proof never establishes optional-clientInfo support. The default no-argument catalog API still selects A1 for historical callers.

The new scenario is inserted immediately before rollback. It exercises a literal-first symbol call without `clientInfo`; another fresh process performs discovery/list/call, with-then-without metadata and existing negative boundaries; a third fresh process sends an unsupported version first, requires exact `-32022`, recovers with a valid call and closes cleanly. All three processes must complete clean EOF before PASS. Required version/capability fields and root/argument/frame checks remain independent of optional client information. The unchanged installed Python journey still runs last.

At that predecessor source, the pinned SDK's supported-then-unsupported session-version guard and missing-required-metadata errors remained explicit A3 protocol debt, not validated conformance or a fix in the optional-clientInfo slice. The initial composed-runner failure remains evidence; `-32600` is not accepted in place of `-32022`. Its delivery remains bound to its original canonical build, retained proof, exact-candidate S4 decision, immutable same-byte promotion and fresh public 16-scenario/Python-rollback proof. Later source does not rewrite that historical evidence or preapprove another candidate.

## Historical A3 required metadata and version successor

The source contract in [A3 required metadata/version](../../018-a3-required-metadata-version/spec.md) adds only `required-metadata-version-recovery` after `optional-client-info-request-local` and before `rollback-python-default`. The fixed catalog has 17 ordered scenarios, ID `a3-preview-required-metadata-version-v1` and canonical SHA-256 `1e737db53dda4557b04616f6fc114c71fa0136c7c11693143772d7b642f5947a`. Manifest/Candidate schema `2.0`, receipt/reference schema `1.0`, the three ordered tools and publisher stay unchanged.

At that source, `prepare-payload`, `seal-build-records` and `build/prepare_preview_fixture.py` required that exact 17-case contract and refused valid historical optional-A3 16-case payloads as well as A1/A2 payloads. Historical catalog definitions, hashes and default A1 catalog API remain unchanged. Retained and remote consumers select their exact known proof generation from the immutable Candidate; old or mixed proof cannot satisfy a new Candidate. Semantic sealers require the complete ordered matrix, not only counts or a declared PASS.

The new real-process runner requires exact correlated `-32602` responses for absent/non-object required metadata and absent/null/wrong-type version or capabilities, before and after same-process success. It proves correction to exact `PreviewMarker` values with text/structured/schema parity, omitted `clientInfo`, empty capabilities, numeric/string IDs and a representable escaped ID. Missing metadata or malformed required metadata must precede invalid tool arguments; valid metadata retains the existing typed argument refusal. Unsupported strings, including empty, whitespace and padded versions, must return exact `-32022` requested/supported data both before and after a valid call, then recover in the same process. Every required recovery process must complete bounded clean EOF before PASS.

Present-invalid optional `clientInfo` remains a separate refusal boundary: a correlated metadata error or a typed EOF followed by the existing bounded clean-close check is acceptable. Required-metadata/version cases never accept EOF, timeout, arbitrary exceptions, wrong IDs or result/error mixtures. The runner reuses the existing request, response-frame, EOF and abort owners; omitted `params` is emitted without a `params` property, not as JSON null. This is not a new full-envelope or parse-error policy.

The required-metadata/version successor was delivered as `stateless-preview-v0.23.17-preview.2` from canonical source `cf39c85cfd99cc2a51f67b7d2686f29e3e8080c0`: retained and fresh public consumers passed 17 cases plus unchanged installed-Python rollback, with candidate-bound review, explicit Root S4, same-byte publication and R0-R3 closeout. Its original bytes and receipts remain immutable; this proves neither a later candidate nor broader A3.

## Current A3 legacy initialize diagnostic successor

The [legacy diagnostic slice](../../019-a3-legacy-initialize-diagnostic/spec.md) adds `legacy-initialize-supported-version-diagnostic` immediately before rollback. Its fixed 18-case contract is `a3-preview-legacy-diagnostic-v1`, canonical SHA-256 `31589f89efc7c0453357bd3cee7216448ec1332e57d135d555b2c897a0bc855d`. Schema versions, three ordered tools and publisher stay unchanged. Current producers require this complete generation and refuse historical required17 as well as older payloads. Historical identities retain their own definitions and proof; neither receipt fields nor preview ordinal select the validator.

The real new runner uses numeric/string initial IDs without modern metadata, requires correlated `-32601` with the exact supported-version diagnostic, then proves modern discovery, the closed tools-only capabilities/catalog and schema-validated exact `PreviewMarker` with text parity. An established-modern initialize must retain the SDK diagnostic; a corrected call and bounded clean EOF must succeed in that same process. The initial local path and later SDK-first path are distinct; no legacy handshake or fallback is enabled. Required-metadata/version recovery remains the unchanged preceding scenario.

This successor was delivered as `stateless-preview-v0.23.17-preview.3` from canonical source `973648067cbcc2ad0b0bdf810cfc1cf5725e0de4`: retained and fresh public consumers passed all 18 cases plus unchanged installed-Python rollback; exact-candidate reviews, explicit Root S4, same-byte publication and R0-R3 closeout completed. Complete error-frame tests separately prove initial and generic excluded exact-cap/cap+1 paths; JSON-byte bounds exclude LF. Historical receipts remain immutable. Broader parser/capabilities policy, runtime servicing, Python/default selection and Program B/C remain unchanged.



## Dispatch and permissions

`mode` is a choice with `build`, `evidence`, and `promote`, defaulting to `build`. The other six inputs are strings with `required: false` and `default: ""`: `preview_version`, `preview_tag`, `retention_days`, `evidence_operation`, `evidence_request`, and `promote_request`. Conditional requirements belong to the helper's real `GITHUB_EVENT_PATH` admission, not YAML's unconditional input requirement. The closed input combinations are:

| Admitted mode / operation | Required nonempty inputs | Other inputs |
| --- | --- | --- |
| `build` | `preview_version`, `preview_tag`, `retention_days` | Evidence and promote fields absent or empty. A missing `mode` preserves the legacy three-field build request; an explicitly empty or unknown mode refuses. |
| `evidence` / `retain` | `evidence_request` | Build and promote fields absent or empty; retention belongs inside the request. |
| `evidence` / `closeout` | `evidence_request` | Build and promote fields absent or empty; retention is derived from Candidate, never supplied by the request. |
| `promote` | `preview_tag`, `promote_request` | Version, retention and evidence fields absent or empty. Retention comes from Candidate; the tag must equal its destination. |

Each helper invocation closes the actual event inputs and checks command/mode compatibility. It derives source, repository, workflow/run identity, and receipt locations from trusted Actions, API, and Git facts. Unknown fields, unsupported operations, mixed-mode fields, missing required values, or more than 65,535 characters in the full escaped input object refuse. No request JSON is interpolated into a shell command or supplied as a path, URL, or script.

The job routes deliberately include invalid-value fallbacks rather than allowing a successful all-skipped run:

| Job ID / exact display name | `if` expression | Permissions |
| --- | --- | --- |
| `build` / `Build canonical-main preview candidate` | `inputs.mode != 'evidence' && inputs.mode != 'promote'` | `contents: read`, `actions: read`, `pull-requests: read` |
| `retain_evidence` / `Retain canonical-main preview evidence` | `inputs.mode == 'evidence' && inputs.evidence_operation != 'closeout'` | `contents: read`, `actions: read` |
| `retain_closeout` / `Retain canonical-main preview closeout` | `inputs.mode == 'evidence' && inputs.evidence_operation == 'closeout'` | `contents: read`, `actions: read` |
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

The closeout adapter and any publisher repair must enter that new canonical S before its first successful publication. Earlier Candidate/build/review/Decision receipts, including source `526df406fb3225a4d23686ac4de920b8067698d2`, remain historical-only; a successor cannot borrow their approval.

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

## Post-publication closeout retention

The existing workflow accepts `mode=evidence`, `evidence_operation=closeout` through `retain_closeout` / `Retain canonical-main preview closeout`. The old retain job intentionally admits every other evidence operation to its existing fail-closed decoder; an unknown operation must not yield an all-skipped success. Closeout is not another publisher, Decision creator, consumer runner, or Program B/C entry point.

### Closed request and original remote proof

`evidence_request` contains exactly these three fields:

| Field | Required value |
| --- | --- |
| `publisher_handoff_reference` | Real compact five-field Actions reference whose `path` is `publisher-handoff.json`. |
| `bundle_base64` | Canonical base64 ZIP containing exactly `remote-consumer-proof.json`, preserving the original remote-command receipt bytes. |
| `record_sha256` | Object with exactly `remote-consumer-proof.json` mapped to its original lowercase raw-file SHA-256. |

The actual `remote` command in the [quickstart](../quickstart.md#run-public-remote-proof) runs outside CI on Windows x64 after publication. Root supplies its original `remote_release` PASS receipt, complete consumer/denial matrix, clean EOF and unchanged installed-Python rollback. Closeout checks that its timestamp is no earlier than the final published Observation and not in the future. CI retains that receipt without claiming to have executed its consumer journey or Python rollback. It separately downloads fresh anonymous public archive/manifest bytes with `download_public_release`, checks them and the extracted executable with `verify_and_extract_retained_artifact`, and only then creates the genuine `POST_PUBLICATION` fragment. Publisher caches or retained payload bytes are not public-download substitutes.

`preview_version`, `preview_tag`, `retention_days` and `promote_request` are absent or empty. No caller context, candidate body, actor, timestamp, execution path/URL, future artifact ID, ready-made Stage/Verification/Handoff or additional input is accepted. Reuse the closed byte-ingress checks: 65,535 characters for the full escaped dispatch object, 60,000 base64 characters, 45,000 ZIP bytes, 128 KiB per original record, strict JSON, base64, member/path/duplicate/metadata/CRC/EOF/hash checks. The source-owned closeout route selects its one filename; the public retain decoder still accepts exactly its original three files. No managed-record schema changes.

### Independently bind publisher P and current closeout Q

Q is the real current `in_progress` closeout run/attempt and unique named job. Its event, repository, both initiators, Actions/API identity, exact checkout, fetched/live main and workflow SHA are independently admitted. Both initiators must match the Decision's authorized dispatcher. The job checks out `github.sha` with `actions/checkout@v6`, `fetch-depth: 0`, `persist-credentials: false`, fetches live main, uses `actions/setup-python@v6` with Python 3.12 and job-scoped `GITHUB_TOKEN: ${{ github.token }}`. There is no SDK setup, build, product installation, credential fallback or CI Python rollback.

P is the distinct actual `completed/success` publisher. Its handoff is only a locator: exact-attempt API, full bounded jobs/artifacts discovery, successful finish/handoff upload steps, original artifacts/raw bytes and the tracked workflow blob at S establish P's provenance. Its artifact name is `stateless-preview-promote-attempt-{P_run}-{P_attempt}-{P_job}-handoff`; name components guide discovery but prove nothing by themselves. Candidate/Catalog/Decision and all nested original references must resolve, including a genuine matching `PRE_PUBLICATION` fragment; null, missing or reconstructed fragments refuse.

Reconstruct P's run/attempt/job, actors, tracked execution policy and complete predecessor history independently, with discovery anchored before P, not Q. Pass that P context as the existing pure sealers' `current_promotion_context`; never copy trusted context from the records, spoof Q as P, or select a completed publisher as the live current job. Attempt/Observation/Classification and `promotion_attempt_id` retain P; new closeout artifact references name Q. Candidate source/build, Catalog source, P/Q workflow SHAs, checkout, fetched main and fresh GitHub main target all equal S at `refs/heads/main`. This narrow historical readback does not weaken live publisher admission or grant release-write authority. DENIED remains denied; UNKNOWN never triggers resend; closeout performs no release mutations.

History ordering uses the unique P row in the complete workflow-run listing, independently bound to P's repository/head repository IDs and names, run ID, workflow, event, ref and source SHA. Its `(created_at, run_id)` orders discovery against the other listing rows; missing, ambiguous or foreign P rows refuse. The listing's latest attempt does not replace the exact historical P attempt selected through its retained handoff. The exact-attempt API's `created_at` separately bounds P's actual execution and immutable Attempt chronology; it need not equal the listing's timestamp.

### Fixed CLI, four upload barriers and final readback

All five commands take zero additional arguments and are admitted only for closeout:

```text
python scripts/stateless_preview_artifact.py admit-closeout
R0 upload: remote-consumer-proof.json + post-publication-verification.json
python scripts/stateless_preview_artifact.py seal-closeout-verification
R1 upload: remote-verification.json
python scripts/stateless_preview_artifact.py seal-closeout-stage
R2 upload: post-publication-stage.json
python scripts/stateless_preview_artifact.py seal-closeout-handoff
R3 upload: program-b-handoff.json
python scripts/stateless_preview_artifact.py verify-closeout-retention
```

The command steps and their exact output keys are:

| Step ID / command | Output keys |
| --- | --- |
| `admit` / `admit-closeout` | `remote_consumer_proof_path`, `post_publication_verification_path`, `closeout_records_artifact_name`, `retention_days` |
| `verification` / `seal-closeout-verification` | `remote_verification_path`, `closeout_verification_artifact_name`, `retention_days` |
| `stage` / `seal-closeout-stage` | `post_publication_stage_path`, `closeout_stage_artifact_name`, `retention_days` |
| `handoff` / `seal-closeout-handoff` | `program_b_handoff_path`, `closeout_handoff_artifact_name`, `retention_days` |
| `verify` / `verify-closeout-retention` | `program_b_handoff_reference` |

The helper owns every output path below its current `RUNNER_TEMP` namespace. The four uploads enumerate only the files above and use `actions/upload-artifact@v7`, `archive: true`, `overwrite: false`, `if-no-files-found: error`, and Candidate-derived validated 1–90-day retention. No scratch/download/log glob or extra public asset is uploaded.

| Upload step / object | Artifact name | Environment prefix, with `_ID` and `_DIGEST` suffixes |
| --- | --- | --- |
| `upload_records` / R0 | `stateless-preview-closeout-records-{Q_run}-{Q_attempt}-{Q_job}` | `A1_CLOSEOUT_RECORDS_ARTIFACT` |
| `upload_verification` / R1 | `stateless-preview-closeout-verification-{Q_run}-{Q_attempt}-{Q_job}` | `A1_CLOSEOUT_VERIFICATION_ARTIFACT` |
| `upload_stage` / R2 | `stateless-preview-closeout-stage-{Q_run}-{Q_attempt}-{Q_job}` | `A1_CLOSEOUT_STAGE_ARTIFACT` |
| `upload_handoff` / R3 | `stateless-preview-closeout-handoff-{Q_run}-{Q_attempt}-{Q_job}` | `A1_CLOSEOUT_HANDOFF_ARTIFACT` |

Each consuming step receives all preceding real upload ID/digest pairs. YAML removes only the exact leading `sha256:`; the helper still requires 64 lowercase hex digits. Raw JSON, ZIP-container, ordered payload-pair and individual archive/manifest/executable hashes remain distinct. A new house `ArtifactDownloader` at every upload boundary independently checks actual metadata, repository/run/source/name/expiry, container bytes and original-member hashes. A local staging map, invented ID or artifact name cannot stand in for retained bytes.

R0 readback precedes `seal_remote_verification`: R1 binds the original P references, genuine pre-publication fragment, fresh R0 post-publication fragment and R0 remote-proof reference. R1 and dependencies are read back before `seal_stage_gate_evidence` creates R2 with the exact ordered post-publication Catalog subset, currently the one `remote-downloaded-consumer-proof` reference from R0. R2 has no invented Verification-reference field; its dependency on R1 is adapter order, not a new schema. R2/R1 and the predecessor graph are read back before `seal_program_b_handoff` creates R3 with all three passing Stages and matching published proof. These existing sealers, schemas and signatures remain unchanged and pure.

Final verification downloads R3 into a new directory, recursively resolves every reference, revalidates the existing sealers, and rechecks Q/live-main equality. Only after success does it emit the real compact `program_b_handoff_reference` to action output and `GITHUB_STEP_SUMMARY`. Every durable reference has exactly `repository`, `run_id`, `artifact_id`, `path`, `sha256`; no future-ID cycle, self-reference or extra closeout artifact is needed. The handoff keeps `completion_scope: A1_OPT_IN_PREVIEW_COMPLETE` and both Program B/C authorization-required flags true. It proves A1 only and grants no authority.

A failure stops dependent steps without delete, overwrite, automatic retry, republish or rollback of public state. Preserve uploaded artifacts; their dependencies still expire at their original times, so the chain lasts no longer than its earliest expiry. A separately admitted later retention attempt has its own Q and must still satisfy canonical-main equality with S. Source fixtures or a green job alone do not complete actual A1 closeout: Root must obtain actual publication, original public consumer/Python-rollback proof and independent readback of all four real uploads.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main mismatch, catalog policy drift, an omitted functional or review gate, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal. Sonar audit absence or a real red outcome must not reject an otherwise valid functional candidate.

The existing evidence fixtures preserve original-byte E0/E1/E2 ordering, whole-dispatch budgeting and no automatic `APPROVE`. Authorization fixtures must now prove no-log Attempt 3.0 admission, exact tracked workflow/job/source/token binding, Observation 2.0 outcomes, denial followed by matching foreign state, predecessor crash/missing-history no-resend, and obsolete-field/probe refusal. Root observed pre-implementation causal RED at the old qualified-grants gate and four REST-denial variants falsely succeeding after matching foreign state; existing Git-denial protection passed. These are retained regression evidence, not integrated GREEN. Root owns the combined focused checks after implementation; source fixtures do not establish canonical publication, fresh public consumer proof or unchanged installed-Python rollback.
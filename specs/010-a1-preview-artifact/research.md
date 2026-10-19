# Research — A1 Opt-In Preview Artifact Runway

## Scope guardrails

This work makes the existing A1 source-run preview distributable and verifiable as an opt-in Windows x64 artifact. It does not alter the published Python package, `netcoredbg-mcp` command, default route, or rollback oracle; it does not transfer Program B stateful/UI/DAP behavior or begin Program C cutover.

## Decision 1 — Make the retained archive and manifest the candidate authority

**Decision:** Use the existing A1 manifest contract as the byte-identity core: `schema_version`, preview `version`, `stateless-preview-v<version>` tag, full source `commit`, fixed `win-x64` RID, and archive/executable `name`, `size_bytes`, and lowercase SHA-256 values. Add one immutable Candidate Identity Record that binds those fields to the build `run_id`, Actions artifact ID/digest, retention metadata, and GitHub prerelease destination. The ZIP archive is the retained candidate; an extracted executable is disposable verification material that must be re-hashed against the archive member and manifest before use.

**Rationale:** The preview project currently defines a `net8.0` executable and its MCP/core dependencies but no RID, self-contained, single-file, reproducibility, or version identity properties. The build therefore must pin the source checkout, state every publish property, and hash the resulting archive/member bytes rather than trusting a local build or assembly version. The inherited manifest schema already supplies the minimal authoritative identity surface without adding ungrounded compiler or timestamp fields.

**Alternatives considered:**

- Archive hash alone — rejected because the inherited contract separately identifies the executable that consumer proof launches.
- A later local rebuild for proof or promotion — rejected because FR-003 and the parent T07 contract require the retained downloaded bytes.
- Adding compiler, SDK, timestamp, throughput, or archive-size identity fields — rejected because no active contract makes them authoritative.

**Evidence:** `host/NetCoreDbg.Mcp.Stateless.Preview/NetCoreDbg.Mcp.Stateless.Preview.csproj`; `specs/005-stateless-preview/contracts/preview-manifest.schema.json`; `specs/005-stateless-preview/research.md`; `specs/005-stateless-preview/contracts/promotion-state-machine.md`; `docs/PRODUCTION-TESTING-PLAYBOOK.md` §10.1.

## Decision 2 — Use a dedicated manual preview workflow, not the Python publish workflow

**Decision:** Use one dedicated manual `.github/workflows/stateless-preview.yml` with `mode=build|evidence|promote`. `build` derives its source from canonical `refs/heads/main`, verifies live origin-main and trusted build provenance, then publishes the self-contained Windows x64 executable, archive/manifest and identities into immutable Actions artifacts. Nonpublishing `evidence` retains original proof/review bytes, Stage and explicit Decision. `promote` accepts only an exact retained `APPROVE` and fresh current-attempt admission by its named dispatcher, downloads the retained build artifact, validates stage-appropriate gates and never rebuilds. The former permission-probe selector is removed and rejected, not aliased.

The namespace is fixed by the inherited manifest schema:

- version: `<major>.<minor>.<patch>-preview.<n>`;
- tag: `stateless-preview-v<version>`;
- archive: `netcoredbg-mcp-stateless-preview-win-x64-<version>.zip`;
- executable: `netcoredbg-mcp-stateless-preview.exe`;
- sibling release asset: the matching manifest JSON.

**Rationale:** The current `.github/workflows/publish.yml` triggers on Python `v*` tags, creates a non-draft release independently of its build/test jobs, builds only `dist/`, and routes to PyPI environments. Extending it would couple a preview artifact to the default Python channel and would make a tag-triggered release precede exact artifact proof and S4 authorization. GitHub Actions artifacts provide an immutable, addressable build-run transport; GitHub prerelease assets provide the consumer-durable distribution state.

**Alternatives considered:**

- Extend `publish.yml` — rejected because its trigger, artifacts, permissions, and PyPI behavior belong exclusively to the Python channel.
- Split build and promotion into separate workflows — rejected for this amendment; it adds another publisher identity and would not let newer tooling inherit an old candidate's approval.
- Automatically promote on `push` or `workflow_run` — rejected because publication must follow an explicit exact-candidate S4 decision.
- Publish a preview through PyPI or alter default selection — rejected as Program C work.

**Evidence:** `.github/workflows/publish.yml`; `specs/005-stateless-preview/plan.md`; `specs/005-stateless-preview/contracts/preview-manifest.schema.json`; GitHub Actions [`upload-artifact`](https://github.com/actions/upload-artifact#not-uploading-to-the-same-artifact) and [`download-artifact`](https://github.com/actions/download-artifact#download-artifacts-from-other-workflow-runs-or-repositories) documentation.

## Decision 3 — Prove only independently downloaded artifact bytes

**Decision:** Add a dedicated artifact-consumer harness that accepts a candidate archive, manifest, expected identity, and fixture root. It verifies archive, manifest, and extracted executable hashes before any MCP traffic; launches only the extracted archive executable with exactly `--project <fixture-root>`; reuses the existing official MCP stdio exchange shape; executes the positive route and every existing denial family; then removes only preview selection/process state and replays the unchanged installed Python consumer oracle in the same verification session. Post-promotion verification repeats the same byte and behavioral proof against newly downloaded public prerelease assets.

**Rationale:** `PreviewOutputPathResolver.ResolveProcess()` deliberately runs repository `bin/**` output and is therefore a source-run behavioral test helper, not artifact proof. The source contract suite already owns modern protocol, containment, resource, and lifecycle semantics; the new harness adds the missing byte-identity boundary without duplicating product behavior or accepting a local substitute.

**Alternatives considered:**

- Reuse source-tree process tests unchanged — rejected because their local build output cannot prove FR-003.
- Build a new executable during the consumer test — rejected by FR-003 and SC-002.
- Check only archive upload/download success — rejected because package identity and runtime behavior still require manifest equations, consumer use, denials, EOF, and rollback.
- Omit source denial families from artifact proof — rejected by FR-005 and SC-002.

**Evidence:** `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewMcpProcessDriver.cs`; `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewProcessContractTests.cs`; `tests/fixtures/PreviewSearchApp/`; `specs/005-stateless-preview/quickstart.md`; `specs/001-mcp-stateless-strangler/quickstart.md`; `specs/010-a1-preview-artifact/spec.md` FR-003 through FR-005 and SC-002/SC-004.

## Decision 4 — Make S2/S3 evidence and the S4 decision distinct immutable records

**Decision:** Keep candidate approval, stage evidence, attempt authorization, and post-publication closeout as separate immutable records. An `APPROVE` Decision binds one Candidate Identity Record, one passing pre-decision Stage Gate Evidence set, a sealed seven-lens S2/S3 aggregate, a distinct independent PR review, decision author, and authorized GitHub dispatcher. A fresh Promotion Attempt then revalidates pre-decision evidence and adds pre-publication evidence before the first remote mutation. Only post-publication closeout and the Program B Handoff require the remote consumer/byte proof. `APPROVE` is not inferred from an environment, source-run acceptance, test result, or later candidate.

A versioned prerelease promotion remains a release action. The stage catalog maps every applicable non-Sonar consumer-proof, review, build/install, tag, and remote-proof obligation from `AGENTS.md` and `docs/RELEASE-PROTOCOL.md` to a stage descriptor or a named bounded inapplicability disposition. The S2/S3 aggregate does not waive any named release gate. User decision of 2026-10-09 supersedes the earlier Sonar-gate requirement: run exact-SHA analysis only after actual publication/asynchronously, preserve its real red or blocked evidence, and route confirmed substantial defects to a subsequent corrective patch and other findings to the backlog. Sonar receipts are not catalog, stage-seal, or release PASS prerequisites.

**Rationale:** The A1 local source-run acceptance explicitly excludes artifact, release, publication, and S4 evidence. FR-006 and SC-003 require nonzero evidence denominators and zero unresolved high-severity findings. The D3 map fixes the S2/S3 lens set and makes external prerelease publication the S4 boundary. Separating records makes changed bytes, stale evidence, and omitted matrix rows mechanically rejectable.

**Alternatives considered:**

- Treat local source-run acceptance as approval evidence — rejected because it is not artifact or S4 proof.
- Treat a local rebuild, partial report, or stale receipt as sufficient — rejected because the candidate identity and nonzero-denominator requirements are exact.
- Treat a GitHub environment reviewer as the approval record — rejected because it can be an additional execution guard but does not bind the exact candidate.
- Infer that a prerelease is exempt from the project release gates — rejected; a tag-and-release promotion must use the owning release protocol unless the authority is explicitly changed.

**Evidence:** `AGENTS.md`; `CONTRIBUTING.md`; `docs/RELEASE-PROTOCOL.md`; `.agent/runs/python-removal-strangler-program-v1/stateless-convergence-program-v4.md` §S3/S4; `docs/adr/ADR-004-stateless-preview.md`; `specs/005-stateless-preview/tasks.md` T007–T010; `specs/010-a1-preview-artifact/spec.md` FR-006/FR-007 and SC-003.

## Decision 5 — Classify every remote state before mutation, including tag-only interruption

**Decision:** The current feature contracts define a promotion classifier with these observed states: `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, and `collision`.

- `unstarted` requires both exact preview tag and release to be absent.
- `tag_only` requires an exact annotated tag whose peeled target is the approved source commit and no release; retry may create only the matching draft prerelease.
- `draft_empty`, `draft_partial`, `draft_complete`, and `published_complete` retain the inherited matching-state behavior.
- `collision` covers any differing tag target/type, release metadata, asset name/size/hash, incomplete non-draft state, unavailable source artifact, or unreadable remote state.

Every attempt starts with live remote classification. Upload only missing matching assets; remotely download and SHA-256/size verify both assets before publish and again after publish. Never use `--clobber`, delete/replace an asset, move/delete/reuse a tag, or convert an unreadable/mismatched remote state into recovery.

**Rationale:** Annotated tag creation and draft-release creation are separately observable remote operations. A failure between them can leave a correct tag with no release; treating that state as `unstarted` would violate the parent admission rule, while treating it as collision would make a recoverable exact state unrecoverable. The explicit `tag_only` state preserves the immutable identity boundary without guessing across a partial remote mutation.

**Alternatives considered:**

- Fold tag-only into `unstarted` — rejected because the tag is no longer absent.
- Treat every tag-only state as collision — rejected because it discards an exact, safe recovery path required by FR-009.
- Use `gh release create` without a pre-created tag or `--verify-tag` — rejected because GitHub CLI may create a tag from the default branch.
- Use `gh release upload --clobber` or delete/re-upload assets — rejected because it can lose original bytes and violates the immutable recovery contract.

**Evidence:** `specs/005-stateless-preview/contracts/promotion-state-machine.md`; `specs/005-stateless-preview/plan.md`; `specs/010-a1-preview-artifact/spec.md` FR-008 through FR-010; GitHub CLI [`gh release create`](https://cli.github.com/manual/gh_release_create) and [`gh release upload`](https://cli.github.com/manual/gh_release_upload) documentation; GitHub [release-assets API](https://docs.github.com/en/rest/releases/assets?apiVersion=2022-11-28#upload-a-release-asset).

## Decision 6 — Record retention and approval configuration rather than inventing them

**Decision:** Treat Actions retention and any GitHub environment guard as observed candidate/run facts, not invented repository constants. The build records selected retention metadata; proof and promotion refuse expired/unavailable/mismatched artifacts and require a new candidate rather than rebuilding under old approval. The Decision names the dispatcher; Attempt 3.0 independently binds current actors/run/attempt/job, canonical source and exact tracked-workflow execution policy. This is configuration evidence, not observed token grants. GitHub enforces each actual operation, whose response and exact readback are recorded in Observation 2.0. An environment guard never replaces candidate or Attempt validation.

**Rationale:** GitHub permits repository/organization policies to constrain artifact retention, and the current repository does not define a preview-specific duration or environment. Choosing a number or assuming an environment would be fictitious policy. Recording the actual value preserves exact evidence while keeping unavailability fail-closed.

**Alternatives considered:**

- Hard-code an arbitrary retention period — rejected because no active authority sets it.
- Accept a missing/expired artifact and rebuild under the old approval — rejected by the exact-candidate contract.
- Infer approval from a workflow-dispatch actor or environment presence — rejected because neither binds the exact run and hashes.

**Evidence:** GitHub Actions [`upload-artifact` retention documentation](https://github.com/actions/upload-artifact#retention-period); `specs/010-a1-preview-artifact/spec.md` edge cases and FR-002 through FR-009; `docs/adr/ADR-004-stateless-preview.md`.

## Decision 7 — Bind exact execution policy and preserve operation outcomes

**Decision:** Replace mandatory running-job grant-log attestation with Attempt 3.0 `execution_policy`, model `GITHUB_JOB_TOKEN_SERVER_ENFORCED`, exact current job/run/source/workflow and tracked workflow-blob SHA-256. Keep top-level `permissions: {}`, promote exactly `actions: read` / `contents: write`, and job-scoped `GITHUB_TOKEN: ${{ github.token }}`. App/installation identity remains `UNOBSERVED`/null. This deliberately removes the former pre-write log observation; declarations/source are not renamed as observed grants, token introspection or guaranteed authorization. Retire the collector/probe/report; do not add another credential, service or publisher.

**Operation boundary:** GitHub authorizes the actual intended request. Valid expected acknowledgement plus complete exact readback is `CONFIRMED`; ambiguous/unobserved original response plus exact read-only reconciliation is `RECONCILED`, proving resulting state rather than exclusive sender attribution. A definite refusal stays `DENIED` and ends its sending attempt even after matching foreign state. Conflict/collision stops; rate-limit 403 is not proof of a missing grant, 404 is not absence, and lost/malformed/truncated/potentially applied responses remain `UNKNOWN` until exact reconciliation. Never resend uncertainty under a new step/run/request ID, delete a starter asset, overwrite, or broaden credentials. Preserve partial publication.

**Predecessors:** Complete independent canonical run/artifact discovery derives nullable `predecessor_attempt`. Missing conclusive evidence after retained A is potentially sent, not unstarted. Resolve original immutable intent/outcome and known tag OID read-only in new O0; never rewrite historical UNKNOWN or attribute later state to a denied sender. A fresh independently admitted attempt may retry only definitely-not-applied refusal after its cause is resolved under fresh context and safe exact state, or perform genuinely remaining operations after predecessor reconciliation. Observation 2.0 closes these outcomes inside the existing envelope, at most two per fixed block; no new ledger/retry coordinator is introduced.

**Historical evidence:** Run `38937481596/1`, source `541b57007b0f56c0bfabe9bd1dd94a20daeac6df`, job `117096512282` observed `UNAVAILABLE/CURRENT_JOB_LOGS_UNAVAILABLE` at offsets 0/60 seconds while the job remained `in_progress`; both reports had no grants/log hash. Preserve artifact `12061642440`, original reports/timing summary and earlier negative run `38907673130`. These observations establish neither publication denial, universal log impossibility nor completion-only semantics. The old requirement is superseded, not passed; no replacement probe is scheduled.

**Migration/authority:** Root accepted this internal mechanism change under its delegated A1 mandate; it needs no operator click. Exact candidate S4 still follows evidence. Preserve original `5f96014b930e269752b47e878f45f8d220b6bfc7` / build `38960876498/1` and APPROVE unchanged as original-only. Changed workflow/publisher and policy snapshots require successor canonical SHA S, a new canonical payload/manifest/Candidate/Catalog, complete retained downloaded consumer/denial/EOF/unchanged-Python proof, S-bound seven-lens and distinct independent review, passing pre-decision Stage and Root's new explicit S4 before a new noncolliding tag. Source tests/simulations or identical executable bytes cannot transfer approval or prove public release. Actual public consumer/rollback, Remote Verification, post-publication Stage and non-authorizing Handoff remain required.

**Primary basis:** GitHub documents [job-token provenance/lifetime](https://docs.github.com/en/actions/concepts/security/github_token), [job-scoped permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions), [REST refusal ambiguity](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) and [release-asset collisions/starter assets](https://docs.github.com/en/rest/releases/assets#upload-a-release-asset). The accepted source-owned amendment is retained at `.agent/runs/a1-release-authorization-20261017/design.json`; this research performs no live request. `contents: write` is repository-wide; reviewed exact-object guards impose the narrower boundary. Current-main equality also avoids introducing another credential for the [release workflow-target caveat](https://docs.github.com/en/rest/releases/releases#create-a-release). No suitable existing-token full effective-permission introspection route was established; that scoped conclusion is not a universal impossibility claim. Revisit only with an explicit applicable primary contract, not a probe or new credential.


## Resolved Technical Context Inputs

| Initial research input | Resolved design decision |
|---|---|
| Artifact size, throughput, and proof-duration targets | No new numerical target is invented. Record measured archive/executable bytes and elapsed proof duration for traceability. Existing 256 KiB MCP response-frame cap and test-local 2/5-second bounds keep their current, narrower contracts. |
| Candidate version and tag namespace | Use the inherited preview schema namespace, distinct from Python `v*`: `<semver>-preview.<n>` and `stateless-preview-v<version>`. |
| Build retention and cross-run provenance | Bind `run_id`, artifact ID/digest, explicit retention metadata, source SHA, and inner file hashes. Download by retained run/artifact identity and fail closed when unavailable. |
| Remote release and retry semantics | Use the state classifier above, including the required `tag_only` state; retry only an exact matching state. |
| Approval record shape | Add a feature-local machine-readable Promotion Decision contract rather than relying on the absent historical `approval-record.md` placeholder. |
| Security/review scope | Use the D3 seven-lens S2/S3 aggregate, the distinct independent PR review, and staged release-gate evidence. No source-run or stale evidence waives a named gate. |

**Result:** all Phase 0 research inputs are resolved. No `NEEDS CLARIFICATION` marker remains for Phase 1.

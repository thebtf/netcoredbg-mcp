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

**Decision:** Add one dedicated manual `.github/workflows/stateless-preview.yml` with `mode=build|promote`. `build` derives its source from canonical `refs/heads/main`, verifies the live origin-main target and trusted build-run provenance, then publishes the one self-contained Windows x64 executable, creates the archive and manifest, computes all identities, and uploads one uniquely named retained Actions artifact with no overwrite. `promote` accepts only an exact `APPROVE` Decision and a fresh current-attempt authorization by the Decision's named dispatcher; it downloads the retained artifact by its run/artifact identity, verifies stage-appropriate gates, and never rebuilds.

The namespace is fixed by the inherited manifest schema:

- version: `<major>.<minor>.<patch>-preview.<n>`;
- tag: `stateless-preview-v<version>`;
- archive: `netcoredbg-mcp-stateless-preview-win-x64-<version>.zip`;
- executable: `netcoredbg-mcp-stateless-preview.exe`;
- sibling release asset: the matching manifest JSON.

**Rationale:** The current `.github/workflows/publish.yml` triggers on Python `v*` tags, creates a non-draft release independently of its build/test jobs, builds only `dist/`, and routes to PyPI environments. Extending it would couple a preview artifact to the default Python channel and would make a tag-triggered release precede exact artifact proof and S4 authorization. GitHub Actions artifacts provide an immutable, addressable build-run transport; GitHub prerelease assets provide the consumer-durable distribution state.

**Alternatives considered:**

- Extend `publish.yml` — rejected because its trigger, artifacts, permissions, and PyPI behavior belong exclusively to the Python channel.
- Split build and promotion into separate workflows — deferred; technically viable but creates another workflow identity and more cross-workflow drift than the parent plan's one manual two-mode workflow.
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

A versioned prerelease promotion remains a release action. The stage catalog maps every applicable consumer-proof, review, build/install, Sonar, tag, and remote-proof obligation from `AGENTS.md` and `docs/RELEASE-PROTOCOL.md` to a stage descriptor or a named bounded inapplicability disposition. The S2/S3 aggregate does not waive any named release gate.

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

**Decision:** Treat Actions retention and any GitHub environment guard as observed candidate/run facts, not fixed repository constants. The build records the selected retention metadata; proof and promotion refuse an expired, unavailable, or mismatched artifact and require a new candidate/version rather than rebuilding. The Promotion Decision names the authorized dispatcher, while the fresh Promotion Attempt proves the consuming run's actor, run/attempt, permissions, and canonical-main provenance. An environment guard may add protection but never replaces candidate or attempt validation.

**Rationale:** GitHub permits repository/organization policies to constrain artifact retention, and the current repository does not define a preview-specific duration or environment. Choosing a number or assuming an environment would be fictitious policy. Recording the actual value preserves exact evidence while keeping unavailability fail-closed.

**Alternatives considered:**

- Hard-code an arbitrary retention period — rejected because no active authority sets it.
- Accept a missing/expired artifact and rebuild under the old approval — rejected by the exact-candidate contract.
- Infer approval from a workflow-dispatch actor or environment presence — rejected because neither binds the exact run and hashes.

**Evidence:** GitHub Actions [`upload-artifact` retention documentation](https://github.com/actions/upload-artifact#retention-period); `specs/010-a1-preview-artifact/spec.md` edge cases and FR-002 through FR-009; `docs/adr/ADR-004-stateless-preview.md`.

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

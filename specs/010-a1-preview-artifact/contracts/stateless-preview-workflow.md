# Stateless preview workflow contract

`.github/workflows/stateless-preview.yml` is one executable manual `build` surface for the A1 preview artifact. It runs only through `workflow_dispatch`, never from `push`, tag push, `workflow_run`, a schedule, or a Python release event. It creates no tag, release, asset outside retained Actions artifacts, Python change, or Program B/C change. Promotion remains a future consuming surface; this workflow intentionally contains no `promote` input, job, placeholder, or remote-mutation path.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

The workflow accepts exactly three caller inputs, all as strings. It validates every input before using it in a path, API call, artifact name, or record. It derives source, repository, workflow identity, run identity, and every receipt location from trusted Actions and Git facts.

| Executable mode | Inputs |
| --- | --- |
| `build` | `preview_version`, `preview_tag`, and `retention_days`. The workflow derives the source from the current canonical `refs/heads/main` ref. It has no source-commit, receipt, artifact-ID, or promotion input. |

Set top-level `permissions: {}`. The build job gets only `contents: read` for checkout/canonical-main resolution and `actions: read` for current-run artifact discovery. It gets no Python publication, package registry, cross-repository, or unnecessary token permission.

The workflow has one concurrency group per preview tag and `cancel-in-progress: false`. GitHub Actions supplies no `queue: max` workflow key, so the executable workflow does not claim one; each admitted run still recomputes every source and artifact fact before sealing its candidate.

## Build from canonical main

The build job must do the following in order:

1. Fetch live `origin/main`. Require `github.ref`, the workflow ref, `github.sha`, checkout `HEAD`, and the resolved `origin/main` target to bind one canonical `refs/heads/main` commit.
2. In a clean detached scanner worktree at that commit, invoke only `scripts/run_sonarqube_exact_head.py --role post-merge`. The repository-owned producer discovers its fixed receipt location, requires repository/main/stage=`post-merge`/`PASS`/scanned-commit/tag-target equality, and seals a path-free post-merge receipt.
3. Publish only `NetCoreDbg.Mcp.Stateless.Preview` as a self-contained, single-file Windows x64 executable.
4. Create the archive and inherited manifest. Validate its equations and hash archive, manifest, and extracted executable.
5. Upload the sealed post-merge receipt and the payload as separate new Actions artifacts with `if-no-files-found: error`, bounded retention, and `overwrite: false`.
6. Discover those current-run artifact IDs and expiry facts through the GitHub Actions API, then seal the Candidate Identity Record and static Release Gate Catalog through `scripts/stateless_preview_artifact.py`.
7. Upload Candidate Identity and Release Gate Catalog as separate new immutable Actions artifacts with the same bounded retention and `overwrite: false`.

The build job must not build a Python wheel, call `publish.yml`, use PyPI tooling, change the default selector, accept an arbitrary source or receipt, or create a promotion input.

## Future promotion boundary (not implemented)

A later separately authorized consuming surface may admit promotion only after a Promotion Decision consumes a passing `pre-decision` Stage Gate Evidence record. The Decision must name one authorized GitHub dispatcher, but it must not name a historical consuming run or require future-stage evidence.

Before any later remote mutation, that future surface must create a fresh Promotion Attempt and verify current actor, run ID, run attempt, canonical main ref/SHA, current permission readback, Decision, and pre-decision Stage Gate Evidence. It must also require passing `pre-publication` Stage Gate Evidence. A different dispatcher, old attempt, static-equivalent permission, arbitrary commit, failed stage record, or changed source must refuse before mutation.

The future attempt must not require `post-publication` evidence. That evidence can exist only after a prerelease is public.

## Recover matching remote state

The fresh attempt creates a Remote Observation and a Remote Classification. The classifier admits only `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, or `collision`.

The workflow creates an annotated tag only for `unstarted`. It creates only the draft prerelease for `tag_only`. It uploads only missing matching assets for `draft_empty` or `draft_partial`. It verifies draft assets before it publishes the prerelease. A retry uses a new attempt and fresh matching classification.

The workflow never rebuilds a candidate, uses `--clobber`, overwrites or deletes an asset, moves/deletes/reuses a tag, deletes/replaces a release, invokes Python packaging, or publishes to PyPI.

## Close after publication

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main or post-merge receipt mismatch, catalog policy drift, an omitted release-protocol disposition, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal.
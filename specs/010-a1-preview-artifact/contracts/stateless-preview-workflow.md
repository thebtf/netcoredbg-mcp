# Stateless preview workflow contract

`.github/workflows/stateless-preview.yml` is the future manual workflow for the A1 preview artifact. It has `build` and `promote` modes. It does not run from `push`, tag push, `workflow_run`, a schedule, or a Python release event. This contract plans the workflow. It does not create a tag, release, asset, Python change, or Program B/C change in this session.

## Own one preview channel

The workflow uses only the feature-local Candidate Identity, Stage Gate Evidence, Promotion Decision, Promotion Attempt, Release Gate Catalog, recovery, remote, and handoff contracts. The Python package, `netcoredbg-mcp` command, default selector, rollback route, and `.github/workflows/publish.yml` remain outside this workflow.

All durable records use closed fields only: structural GitHub references, bounded opaque IDs, hashes, timestamps, and declared codes. The seal step rejects raw diagnostics, commands, local paths, URLs outside structural artifact references, credential-shaped strings, control characters, and unapproved values.

## Dispatch and permissions

The workflow accepts string inputs only. It validates every input before it uses the value in a path, API call, or record.

| Mode | Inputs |
| --- | --- |
| `build` | `preview_version`, `preview_tag`, and `retention_days`. The workflow derives the source from the current canonical `refs/heads/main` ref. It has no arbitrary source-commit input. |
| `promote` | Structural references and expected hashes for the Candidate Identity Record, Promotion Decision, pre-decision Stage Gate Evidence, pre-publication Stage Gate Evidence, and Release Gate Catalog. Runtime facts are never inputs. |

Set top-level `permissions: {}`. The build job gets only the read permissions needed to resolve canonical main and upload immutable Actions artifacts. The promote job gets `actions: read` and `contents: write` only after it validates a fresh current-attempt record. Neither job gets Python publication, package registry, cross-repository, or unnecessary token permissions.

Use one workflow-level concurrency group per preview tag with `queue: max` and `cancel-in-progress: false`. The group excludes mode, actor, run ID, and run attempt. Every queued promotion starts from fresh admission and fresh remote observation.

## Build from canonical main

The build job must do the following in order:

1. Resolve the live `origin/main` target. Require `github.ref`, the workflow ref, and the resolved target to be `refs/heads/main` and the same commit.
2. Require a passing post-merge exact-head receipt whose scanned commit and tag target equal that commit.
3. Publish only `NetCoreDbg.Mcp.Stateless.Preview` as a self-contained, single-file Windows x64 executable.
4. Create the archive and manifest. Validate inherited manifest equations and hash archive, manifest, and extracted executable.
5. Upload the payload in a new immutable Actions artifact with `if-no-files-found: error` and `overwrite: false`.
6. Create and upload a separate immutable Candidate Identity Record artifact that binds canonical main, trusted build-run provenance, post-merge receipt, payload artifact, and identities.
7. Resolve and seal the static Release Gate Catalog from tracked policy files at that exact commit.

The build job must not build a Python wheel, call `publish.yml`, use PyPI tooling, change the default selector, or let a caller select an arbitrary commit.

## Admit promotion by stage

A Promotion Decision consumes only the passing `pre-decision` Stage Gate Evidence record. It names a decision author and one authorized GitHub dispatcher. It does not name a consuming run and does not require future-stage evidence.

Before remote mutation, the named dispatcher creates a fresh Promotion Attempt. The attempt verifies the current `github.actor`, run ID, run attempt, canonical main ref/SHA, current permission readback, Decision, and pre-decision Stage Gate Evidence. It also requires the passing `pre-publication` Stage Gate Evidence record. A different dispatcher, old attempt, static-equivalent permission, arbitrary commit, failed stage record, or changed source refuses before mutation.

The attempt must not require `post-publication` evidence. That evidence can exist only after a prerelease is public.

## Recover matching remote state

The fresh attempt creates a Remote Observation and a Remote Classification. The classifier admits only `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, or `collision`.

The workflow creates an annotated tag only for `unstarted`. It creates only the draft prerelease for `tag_only`. It uploads only missing matching assets for `draft_empty` or `draft_partial`. It verifies draft assets before it publishes the prerelease. A retry uses a new attempt and fresh matching classification.

The workflow never rebuilds a candidate, uses `--clobber`, overwrites or deletes an asset, moves/deletes/reuses a tag, deletes/replaces a release, invokes Python packaging, or publishes to PyPI.

## Close after publication

After the prerelease is public, download the public archive and manifest into a new directory. Create the Remote Verification and remote-release Artifact Consumer Proof records. Then seal a passing `post-publication` Stage Gate Evidence record.

A Program B Handoff requires passing evidence for all three stages, `published_complete`, matching remote verification, and matching remote proof. The handoff proves A1 only. It does not start Program B or Program C.

## Test the workflow contract

`tests/test_stateless_preview_artifact.py` must reject arbitrary/unmerged source admission, origin-main or post-merge receipt mismatch, catalog policy drift, an omitted release-protocol disposition, missing or duplicate stage evidence, an aggregate with fewer than seven lenses, a duplicate lens, a stale review, a different dispatcher, a reused attempt, missing pre-publication evidence, premature post-publication evidence, nonmatching remote state, a rebuild, a clobber/delete operation, Python-channel use, secret/path leakage, and a false handoff seal.
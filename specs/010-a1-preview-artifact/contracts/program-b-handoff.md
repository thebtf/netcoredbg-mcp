# Program B handoff contract

The [Program B Handoff schema](program-b-handoff.schema.json) defines the closed durable record that proves `A1_OPT_IN_PREVIEW_COMPLETE` for one published preview candidate. The record does not authorize, start, scope, or implement Program B or Program C.

## Bind the handoff

Every reference in a handoff is a `github_artifact_file_reference`: repository, run ID, artifact ID, safe artifact-relative path, and raw-byte SHA-256. The handoff validator downloads and hashes each raw record before it parses it. A URL, branch, tag, local path, artifact name, source checkout, or a document with the right JSON shape is not a record reference.

The handoff binds all of these records to the same canonical `refs/heads/main` candidate commit:

- Candidate Identity Record
- approved Promotion Decision
- fresh Promotion Attempt
- Remote Observation
- Remote Classification
- Remote Verification
- remote-release Artifact Consumer Proof
- Release Gate Catalog
- three Stage Gate Evidence records

The validator requires exact equality of candidate binding, source ref, source commit, origin-main target, trusted build run, Decision hash, authorized dispatcher, current attempt ID, current workflow run/attempt, and permission readback.

## Require every stage

The handoff contains three sealed Stage Gate Evidence references. Each stage record must resolve to the same catalog and candidate.

| Stage | Required outcome |
| --- | --- |
| `pre-decision` | The retained proof, seven-lens aggregate, distinct PR review, and candidate exact-head Sonar all pass. |
| `pre-publication` | Post-merge exact-head Sonar passes, and its scanned commit and tag target equal the candidate commit. |
| `post-publication` | Fresh remote consumer and byte proof passes after the prerelease is public. |

The validator derives the descriptor subset for each stage from the static catalog. It requires exact descriptor/evidence equality. It rejects a missing, extra, duplicate, wrong-stage, wrong-type, stale, failed, incomplete, candidate-mismatched, source-mismatched, or policy-drifted entry.

## Require published remote proof

The Remote Observation must describe an annotated tag at the candidate commit, a published prerelease, and matching archive and manifest assets. The Remote Classification must be `published_complete`. The Remote Verification must bind the same Decision and Promotion Attempt and show matching archive, manifest, and executable bytes before and after publication.

The remote Artifact Consumer Proof must use `proof_stage: "remote_release"` and pass the full consumer journey, denial matrix, clean EOF, and unchanged Python rollback. A retained-only proof, source-tree binary, rebuild, partial matrix, failed EOF, or changed Python route refuses the handoff.

## Preserve program boundaries

The schema requires both `program_b_authorization_required: true` and `program_c_authorization_required: true`. These flags are facts about the handoff boundary, not grants.

A separately authorized Program B scope may cite a sealed handoff. It still owns stateful identity and ownership, bridge work, Native Scene or UI, DAP or inspection/runtime work, and remaining routes. Program C still owns default selection, package cutover, deprecation, and Python retirement.

## Future implementation ownership

`scripts/stateless_preview_artifact.py` owns record resolution, raw-byte hashing, semantic admission, and handoff sealing. It seals a handoff only after every required stage and remote record passes.

`tests/test_stateless_preview_artifact.py` owns regression coverage. It must reject a changed candidate, source ref/commit, origin-main target, build run, Decision, dispatcher, attempt ID, run/attempt, permission readback, stage record, catalog descriptor, post-merge scan/tag target, remote record, remote proof, or authorization-required flag. It must also reject raw diagnostics, local paths, secrets, and an attempt to use a handoff as Program B/C authorization.
# Program B handoff contract

The [Program B Handoff schema](program-b-handoff.schema.json) defines the closed durable record that proves `A1_OPT_IN_PREVIEW_COMPLETE` for one published preview candidate. The record does not authorize, start, scope, or implement Program B or Program C.

## Bind the handoff

Every reference in a handoff is a `github_artifact_file_reference`: repository, run ID, artifact ID, safe artifact-relative path, and raw-byte SHA-256. The handoff validator downloads and hashes each raw record before it parses it. A URL, branch, tag, local path, artifact name, source checkout, or a document with the right JSON shape is not a record reference.

The handoff binds all of these records to the same canonical `refs/heads/main` candidate commit:

- Candidate Identity Record
- approved Promotion Decision
- fresh Promotion Attempt 2.0
- Remote Observation
- Remote Classification
- Remote Verification
- remote-release Artifact Consumer Proof
- Release Gate Catalog
- three Stage Gate Evidence records

The validator requires exact equality of candidate binding, source ref/commit, origin-main target, trusted build run, Decision hash/authorization, current attempt ID, workflow run/attempt/job and workflow SHA. Both current initiator ID/login maps must equal the Decision's authorized dispatcher. Resolve the Attempt 2.0 qualified same-promote-job report, not a probe report or declaration; its exact rights projection and fixed unobserved/null installation-token provenance must validate. Do not equate a human dispatcher with the execution-token principal or accept legacy Attempt 1.0. These are future closeout obligations, not an implemented handoff sealer or live permission proof.

## Require every stage

The handoff contains three sealed Stage Gate Evidence references. Each stage record must resolve to the same catalog and candidate.

| Stage | Required outcome |
| --- | --- |
| `pre-decision` | The retained proof, seven-lens aggregate, and distinct PR review all pass. |
| `pre-publication` | The candidate-bound stage record passes for the current empty catalog subset; fresh source, provenance, permission, and remote-state guards still apply. |
| `post-publication` | Fresh remote consumer and byte proof passes after the prerelease is public. |

The validator derives the descriptor subset for each stage from the static catalog. It requires exact descriptor/evidence equality. It rejects a missing, extra, duplicate, wrong-stage, wrong-type, stale, failed, incomplete, candidate-mismatched, source-mismatched, or policy-drifted entry.

## Require published remote proof

The Remote Observation must describe an annotated tag at the candidate commit, a published prerelease, and matching archive and manifest assets. The Remote Classification must be `published_complete`. The Remote Verification must bind the same Decision and Promotion Attempt and show matching archive, manifest, and executable bytes before and after publication.

The remote Artifact Consumer Proof must use `proof_stage: "remote_release"` and pass the full consumer journey, denial matrix, clean EOF, and unchanged Python rollback. A retained-only proof, source-tree binary, rebuild, partial matrix, failed EOF, or changed Python route refuses the handoff.

Sonar is a separate asynchronous post-publication audit, not a handoff or
`PASS` receipt dependency. Preserve its real red or blocked record; it does not
replace or weaken any functional consumer, byte-identity, or review proof.

## Preserve program boundaries

The schema requires both `program_b_authorization_required: true` and `program_c_authorization_required: true`. These flags are facts about the handoff boundary, not grants.

A separately authorized Program B scope may cite a sealed handoff. It still owns stateful identity and ownership, bridge work, Native Scene or UI, DAP or inspection/runtime work, and remaining routes. Program C still owns default selection, package cutover, deprecation, and Python retirement.

## Future implementation ownership

`scripts/stateless_preview_artifact.py` owns record resolution, raw-byte hashing, semantic admission, and handoff sealing. It seals a handoff only after every required stage and remote record passes.

`tests/test_stateless_preview_artifact.py` owns regression coverage. It must reject a changed candidate, source ref/commit, origin-main target, build run, Decision, dispatcher, attempt ID, run/attempt, permission readback, stage record, catalog descriptor, remote record, remote proof, or authorization-required flag. It must also reject raw diagnostics, local paths, secrets, and an attempt to use a handoff as Program B/C authorization.
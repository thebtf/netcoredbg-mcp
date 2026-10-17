# Program B handoff contract

The [Program B Handoff schema](program-b-handoff.schema.json) defines the closed durable record that proves `A1_OPT_IN_PREVIEW_COMPLETE` for one published preview candidate. The record does not authorize, start, scope, or implement Program B or Program C.

## Bind the handoff

Every reference stored in a handoff is a compact
`github_artifact_file_reference`: `repository`, `run_id`, `artifact_id`, safe
artifact-relative `path`, and original raw-byte `sha256`. The pure sealer asks
its supplied `record_resolver` for each original record's bytes and checks their
hash before parsing. It does not download them itself. The effect-owning adapter
must resolve actual Actions metadata and obtain the bytes. A URL, branch, tag,
local path, artifact name, source checkout, or document with the right JSON shape
is not a record reference.

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

The validator requires exact equality of candidate binding, source ref/commit,
origin-main target, trusted build run, Decision hash/authorization, current
attempt ID, workflow run/attempt/job and workflow SHA. Both current initiator
ID/login maps must equal the Decision's authorized dispatcher. Resolve the
Attempt 2.0 qualified same-promote-job report, not a probe report or declaration;
its exact rights projection and fixed `github.token` installation-token
provenance with unobserved/null app and installation IDs must validate. Do not
equate a human dispatcher with the execution-token principal or accept legacy
Attempt 1.0. Source-only sealers implement these checks; their availability does
not establish live permission or successful published proof.

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

Remote consumer execution is the actual `remote` command in the
[quickstart](../quickstart.md#run-public-remote-proof), not a sealer effect. It
downloads fresh anonymous archive/manifest bytes from the exact public nondraft
GitHub prerelease, binds the annotated tag object to the candidate commit, and
checks the distinct asset IDs, raw hashes and extracted executable before running
the sole `find_code_symbol` capability with explicit `--project`, the complete
denial matrix and EOF. It then replays the retained installed-Python oracle
without reinstalling, replacing or changing its console entry point/default.

Both `pre_publication_verification` and `post_publication_verification` must
already contain the genuine phase fragments for the exact release and asset
IDs, archive/manifest/executable descriptors and four verifier equations. The
sealer compares them with the validated lineage; it does not create a missing
fragment from expected hashes, boolean flags, publisher cache or
`published_complete`. In particular, an initial published-state recovery with
`pre_publication_verification: null` cannot seal Remote Verification or Handoff.
An actual predecessor fragment is required; absence refuses closeout.

Sonar is a separate asynchronous post-publication audit, not a handoff or
`PASS` receipt dependency. Preserve its real red or blocked record; it does not
replace or weaken any functional consumer, byte-identity, or review proof.

## Preserve program boundaries

The schema requires both `program_b_authorization_required: true` and `program_c_authorization_required: true`. These flags are facts about the handoff boundary, not grants.

A separately authorized Program B scope may cite a sealed handoff. It still owns stateful identity and ownership, bridge work, Native Scene or UI, DAP or inspection/runtime work, and remaining routes. Program C still owns default selection, package cutover, deprecation, and Python retirement.

## Source-only sealing APIs

`scripts/stateless_preview_artifact.py` exports `seal_remote_verification` and
`seal_program_b_handoff`. Each takes one supplied record plus exactly these
keyword-only inputs:

```python
from scripts.stateless_preview_artifact import (
    seal_program_b_handoff,
    seal_remote_verification,
)

sealed_verification = seal_remote_verification(
    verification,
    candidate_identity=candidate_identity,
    candidate_identity_bytes=candidate_identity_bytes,
    candidate_identity_reference=candidate_identity_reference,
    release_gate_catalog=release_gate_catalog,
    release_gate_catalog_bytes=release_gate_catalog_bytes,
    release_gate_catalog_reference=release_gate_catalog_reference,
    record_resolver=record_resolver,
    repository_root=repository_root,
    current_promotion_context=current_promotion_context,
)

# Call only after the adapter retains and resolves this verification's raw record.
sealed_handoff = seal_program_b_handoff(
    handoff,
    candidate_identity=candidate_identity,
    candidate_identity_bytes=candidate_identity_bytes,
    candidate_identity_reference=candidate_identity_reference,
    release_gate_catalog=release_gate_catalog,
    release_gate_catalog_bytes=release_gate_catalog_bytes,
    release_gate_catalog_reference=release_gate_catalog_reference,
    record_resolver=record_resolver,
    repository_root=repository_root,
    current_promotion_context=current_promotion_context,
)
```

This is an embedding call sequence, not a standalone CLI or evidence collector.
Prepare every input before calling it:

- `verification` and `handoff` are complete supplied records conforming to their
  respective frozen schemas, backed by real predecessor evidence, not empty
  templates. The handoff references the retained raw verification record.
- `candidate_identity` and `release_gate_catalog` are the parsed original
  records; their corresponding `*_bytes` are the unchanged original bytes, not
  re-serialized JSON.
- `candidate_identity_reference` is the **expanded** downloaded metadata
  reference with nested `artifact: {id, name, sha256, retention}`. The helper
  projects it to the compact candidate reference used inside durable records.
  `release_gate_catalog_reference` and references passed to `record_resolver`
  are **compact** five-field structural locators. The CLI's expanded catalog
  reference must be projected from verified metadata, not passed unchanged.
- `record_resolver(reference) -> bytes` resolves every compact locator to its
  exact original bytes, including all nested Decision/Attempt/permission/Stage/
  observation/classification/consumer-proof records and the verification for
  handoff. Missing or wrong bytes refuse. The sealer adds no transport.
- `repository_root` identifies the Git repository with the candidate commit's
  exact tracked policy snapshots; current checkout files are not substitutes.
- `current_promotion_context` is independently obtained trusted runtime context
  with exactly `current_attempt_id`, `workflow_run`, `job_id`, `job_key`,
  `workflow_sha`, `checkout_head`, `origin_main_target`, `current_github_actor`
  and `current_github_triggering_actor`. Use the
  [Attempt 2.0 context contract](../quickstart.md#revalidate-one-source-only-attempt-20),
  not values copied from the record being admitted. An old completed/foreign job,
  identical permission probe, PAT/auth cache or YAML declaration cannot supply
  the missing qualified same-current-promote-job grant.

Raw-record SHA-256, Actions ZIP-container SHA-256 and Candidate's ordered
`SHA256(archive_bytes || manifest_bytes)` payload hash are distinct. Neither
container metadata nor a reconstructed JSON object's hash proves original raw
record or public asset bytes.

Both APIs return a detached deeply immutable mapping or raise `ValueError`.
They reuse semantic validators, derive `published_complete`, require `APPROVE`,
revalidate Attempt 2.0, and bind the same candidate, Decision, Attempt, Catalog,
remote asset IDs and consumer proof. `seal_program_b_handoff` additionally
requires all three exact passing catalog Stage subsets. They collect no live
evidence, write no receipt, publish nothing and grant no authority. Their caller
owns real collection, write-once retention and readback.

`tests/test_stateless_preview_artifact.py` owns the source regression vectors.
Controlled/SIMULATED vectors and loopback HTTP/MCP/EOF/tamper checks are supporting
evidence, not live public-release receipts. Record actual source, runtime and
publication proof separately before claiming A1 completion. Python/PyPI and
Program B/C authorization remain unchanged.
# Release gate catalog contract

The Release Gate Catalog defines the fixed release obligations for one A1 Candidate Identity Record. It is an applicability record. It does not carry gate outcomes, create a tag, publish a prerelease, or authorize a release.

The catalog validates against [release-gate-catalog.schema.json](release-gate-catalog.schema.json). Its resolver reads the candidate and the tracked policy files at the candidate's canonical merged-main commit. A caller cannot supply a gate list, a policy snapshot, or an inapplicability result.

## Bind the canonical source

The resolver accepts a candidate only when all of these values match:

- `candidate.source.ref`, `candidate.build.ref`, and `catalog.source_ref` are `refs/heads/main`.
- `candidate.source.commit`, `candidate.source.origin_main_target`, `candidate.build.commit`, and `catalog.source_commit` are the same commit.
- The trusted build workflow ran from that ref and commit.
- The candidate's post-merge exact-head receipt is `PASS`, and both its scanned commit and tag target equal that commit.

The catalog snapshots these tracked files at that exact commit:

1. `AGENTS.md`
2. `CONTRIBUTING.md`
3. `docs/RELEASE-PROTOCOL.md`
4. `docs/adr/ADR-004-stateless-preview.md`
5. `specs/010-a1-preview-artifact/spec.md`

An untracked `.agent` file, a current-branch readback, a later main commit, a URL, or an arbitrary JSON document is not catalog authority.

## Define the six gates

The catalog contains exactly these descriptors. Each descriptor has a fixed stage, record type, authority-rule set, and evidence requirement set.

| Stage | Gate ID | Typed record | Meaning |
| --- | --- | --- | --- |
| `pre-decision` | `retained-downloaded-consumer-proof` | `artifact-consumer-proof` | The retained archive proves the local opt-in consumer, denial, EOF, and Python rollback journey. |
| `pre-decision` | `s2-s3-seven-lens-evidence` | `s2-s3-seven-lens-aggregate` | The sealed aggregate proves all seven required S2/S3 lenses. |
| `pre-decision` | `independent-review` | `independent-pr-review` | The distinct project release obligation for an independent PR review. It does not duplicate an S2/S3 lens. |
| `pre-decision` | `candidate-exact-head-sonar` | `sonarqube-exact-head` | Candidate exact-head Sonar evidence binds the canonical candidate commit. |
| `pre-publication` | `post-merge-exact-head-sonar` | `sonarqube-exact-head` | Post-merge Sonar scans and tags the same canonical main commit before the first remote mutation. |
| `post-publication` | `remote-downloaded-consumer-proof` | `artifact-consumer-proof` | Fresh public prerelease bytes prove the consumer, denial, EOF, and Python rollback journey. |

The resolver also records an explicit release-protocol disposition for every relevant rule. A rule is either mapped to one descriptor above or marked inapplicable with a bounded reason. The preview channel never treats a Python-wheel, PyPI, or default-selector rule as silently absent. Those rules are inapplicable because this feature does not invoke `.github/workflows/publish.yml`, build a Python wheel, publish to PyPI, or change the public Python selector.

## Seal evidence by stage

[Stage Gate Evidence](stage-gate-evidence.schema.json) carries the typed proof for one descriptor stage. It binds the Candidate Identity Record and this catalog, then carries exactly the descriptor subset for its stage.

1. A Decision consumes only a passing `pre-decision` record.
2. A fresh [Promotion Attempt](promotion-attempt.schema.json) revalidates that record and requires a passing `pre-publication` record before a tag, release, or asset mutation.
3. The `post-publication` record is created only after a prerelease exists and fresh remote bytes are downloaded.
4. [Program B Handoff](program-b-handoff.md) requires passing evidence for all three stages.

The catalog never requires post-publication proof before publication. A stage record never predicts evidence from a later stage.

## Resolve and test the catalog

`scripts/stateless_preview_artifact.py` owns `resolve_release_gate_catalog`. Before it seals a catalog, it resolves and hashes the Candidate Identity Record, verifies canonical-main and post-merge provenance, hashes every tracked policy file, derives the fixed descriptors, and records every release-protocol disposition.

`tests/test_stateless_preview_artifact.py` must refuse policy drift, a wrong ref or commit, an untrusted build run, a failed or unrelated post-merge receipt, a missing or extra descriptor, an omitted release-protocol disposition, a wrong typed record, and a catalog supplied by a caller rather than the resolver. No test run publishes a release.
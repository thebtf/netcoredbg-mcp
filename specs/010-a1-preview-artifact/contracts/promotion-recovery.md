# A1 preview promotion and recovery contract

This contract governs the remote state of one approved A1 preview candidate. It extends the parent [promotion state machine](../../005-stateless-preview/contracts/promotion-state-machine.md) with `tag_only`. It does not change the Python package, `netcoredbg-mcp` command, default route, or rollback journey.

The [Candidate Identity Record](candidate-identity.schema.json), [Promotion Decision](promotion-decision.schema.json), [Promotion Attempt](promotion-attempt.schema.json), [Release Gate Catalog](release-gate-catalog.schema.json), [Stage Gate Evidence](stage-gate-evidence.schema.json), and typed remote records are the authority for this contract. A URL, local checkout, source-tree executable, arbitrary record, or matching commit alone is not authority.

## Admit an approved candidate by stage

A Promotion Decision is candidate approval. It requires only one passing `pre-decision` Stage Gate Evidence record. That record contains the retained downloaded consumer proof, seven-lens aggregate, distinct independent PR review, and candidate exact-head Sonar.

A Decision does not bind a historical workflow run. It names a decision author and one authorized GitHub dispatcher. The dispatcher creates a fresh Promotion Attempt in the consuming `workflow_dispatch` run. The attempt must bind the current actor, run ID, run attempt, permission readback, canonical main source, Decision, and fresh remote observation.

Before the first remote mutation, the attempt revalidates the passing pre-decision record and requires a passing `pre-publication` Stage Gate Evidence record. The pre-publication record contains the post-merge exact-head Sonar receipt. The receipt's scanned commit and tag target must equal the canonical candidate commit.

The attempt must not require the `post-publication` record. That record cannot exist until the prerelease is public.

## Classify remote state before mutation

Every attempt creates a [Remote Observation](remote-observation.schema.json) and a [Remote Classification](remote-classification.schema.json). Both records bind the attempt ID and the exact candidate.

| State | Required facts | Allowed action |
| --- | --- | --- |
| `unstarted` | The matching annotated tag and release are absent. | Create the annotated tag at the candidate commit, then create the matching draft prerelease. |
| `tag_only` | The matching annotated tag peels to the candidate commit. No release exists. | Create only the matching draft prerelease. |
| `draft_empty` | The matching tag and draft prerelease exist. Both expected assets are absent. | Upload the approved archive and manifest. |
| `draft_partial` | The matching tag and draft prerelease exist. Every present expected asset has the approved name, size, and SHA-256. | Upload only missing approved assets. |
| `draft_complete` | The matching tag and draft prerelease exist. Both remote assets match the candidate. | Re-download and verify the assets, then publish the prerelease. |
| `published_complete` | The matching tag and published prerelease exist. Both remote assets match the candidate. | Do not mutate. Run post-publication proof. |
| `collision` | Any other state, unreadable record, unavailable retained artifact, or mismatch. | Refuse. A changed candidate needs a new version and tag. |

A retry starts with retained-byte verification, a new Promotion Attempt, and a new observation. Only the same dispatcher named by the Decision may retry. The contract never permits a rebuild, `--clobber`, asset overwrite, asset deletion, release deletion, tag move, tag deletion, tag reuse, Python release action, or PyPI publication.

## Close the post-publication stage

After the prerelease is public, create a [Remote Verification](remote-verification.schema.json) record and a fresh `post-publication` Stage Gate Evidence record. The remote verification and consumer proof must bind the same candidate, Decision, Promotion Attempt, remote observation, and remote classification.

The post-publication consumer proof uses fresh release downloads. It proves archive, manifest, and executable identities; the complete denial matrix; JSON-RPC-only stdout; clean EOF; and unchanged Python rollback. The proof must not use a source-tree executable, a local rebuild, or the retained download from before publication.

## Hand off A1 only

[Program B Handoff](program-b-handoff.md) requires passing `pre-decision`, `pre-publication`, and `post-publication` Stage Gate Evidence records. It also requires `published_complete`, a passing remote verification, and a passing remote consumer proof.

The handoff proves only the A1 opt-in preview boundary. It does not start Program B or Program C. Program B and Program C still need their own separate authorization.
# A1 preview promotion and recovery contract

This contract governs the remote state of one approved A1 preview candidate. It extends the parent [promotion state machine](../../005-stateless-preview/contracts/promotion-state-machine.md) with `tag_only`. It does not change the Python package, `netcoredbg-mcp` command, default route, or rollback journey.

The [Candidate Identity Record](candidate-identity.schema.json), [Promotion Decision](promotion-decision.schema.json), [Promotion Attempt](promotion-attempt.schema.json), [Release Gate Catalog](release-gate-catalog.schema.json), [Stage Gate Evidence](stage-gate-evidence.schema.json), and typed remote records are the authority for this contract. A URL, local checkout, source-tree executable, arbitrary record, or matching commit alone is not authority.

## Admit an approved candidate by stage

A Promotion Decision is candidate approval. It requires only one passing `pre-decision` Stage Gate Evidence record. That record contains the retained downloaded consumer proof, seven-lens aggregate, and distinct independent PR review.

A Decision does not bind a historical workflow run. It names a decision author and one authorized GitHub dispatcher; they need not be the same identity. Attempt 2.0 requires both the current actor and rerun initiator ID/login maps to equal that dispatcher. Its separate closed current context supplies fresh attempt ID, the unchanged eight-field workflow run, positive decimal-string job ID, fixed `job_key: promote`, workflow SHA, checkout HEAD, and origin-main target. These facts come from trusted runtime admission, not the Attempt or dispatch request. Source/build/main/checkout/workflow SHA must all equal the approved candidate.

Before the first remote mutation, the attempt revalidates the passing pre-decision record and requires a passing `pre-publication` Stage Gate Evidence record. Its current catalog descriptor subset is empty; the record still binds the same canonical candidate and catalog. Source, provenance, current permission, and remote-state checks remain required. No Sonar receipt or audit outcome is an admission or seal dependency.

The attempt must not require the `post-publication` record. That record cannot exist until the prerelease is public.

`seal_promotion_attempt` resolves raw Candidate/Catalog/Decision/Stage/report/remote bytes and hashes, reuses the existing semantic sealers, requires `APPROVE` and its exact authorization/pre-decision receipt, and re-derives the complete current-attempt classification. The native permission report must be closed, `AVAILABLE`/`QUALIFIED`, bind the same numeric run/attempt/job and source/workflow SHA, use the fixed promote job name, and retain the native log hash/source, timestamps, before/after HTTP evidence, and full explicit grants. The Attempt's rights are exactly the checked `actions: read`/`contents: write` projection. A probe report never substitutes for the consuming promote job.

Keep execution-token provenance separate: trusted workflow wiring must use `GITHUB_TOKEN: ${{ github.token }}`. Attempt records `github.token`, `GITHUB_APP_INSTALLATION_TOKEN`, `UNOBSERVED`, and null App/installation IDs; no human principal, PAT, auth-cache fallback, or token introspection is introduced. Legacy Attempt 1.0/`authenticated_actor`, stale/colliding records, extras, missing report evidence and premature post-publication proof refuse.

The deterministic order is pre-decision ≤ Decision ≤ pre-publication ≤ report start ≤ report finish ≤ observation ≤ classification ≤ Attempt, before candidate retention expiry. A new retry needs a new independently captured attempt ID and fresh same-job facts, not a renamed old record. The pure source seam does not collect live facts, add a promote YAML/CLI route, or mutate remote state. Actual permission capability remains `UNKNOWN`; probe run `38907673130` was `UNAVAILABLE` and supplies no live grant or approval.

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

Run the separate exact-head Sonar audit only after actual publication. Preserve
its real red or blocked result without adding a required gate descriptor or
changing the functional consumer proof, immutable tag, or published artifacts.

## Hand off A1 only

[Program B Handoff](program-b-handoff.md) requires passing `pre-decision`, `pre-publication`, and `post-publication` Stage Gate Evidence records. It also requires `published_complete`, a passing remote verification, and a passing remote consumer proof.

The handoff proves only the A1 opt-in preview boundary. It does not start Program B or Program C. Program B and Program C still need their own separate authorization.
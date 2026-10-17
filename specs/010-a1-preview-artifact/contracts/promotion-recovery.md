# A1 preview promotion and recovery contract

This contract governs the remote state of one approved A1 preview candidate. It extends the parent [promotion state machine](../../005-stateless-preview/contracts/promotion-state-machine.md) with `tag_only`. It does not change the Python package, `netcoredbg-mcp` command, default route, or rollback journey.

The [Candidate Identity Record](candidate-identity.schema.json), [Promotion Decision](promotion-decision.schema.json), [Promotion Attempt](promotion-attempt.schema.json), [Release Gate Catalog](release-gate-catalog.schema.json), [Stage Gate Evidence](stage-gate-evidence.schema.json), and typed remote records are the authority for this contract. A URL, local checkout, source-tree executable, arbitrary record, or matching commit alone is not authority.

## Admit an approved candidate by stage

A Promotion Decision is candidate approval. It requires only one passing `pre-decision` Stage Gate Evidence record. That record contains the retained downloaded consumer proof, seven-lens aggregate, and distinct independent PR review.

A Decision does not bind a historical workflow run. It names a decision author and one authorized GitHub dispatcher; they need not be the same identity. Attempt 3.0 requires both current actor and rerun initiator ID/login maps to equal that dispatcher. Independent trusted runtime context supplies fresh attempt ID, the unchanged eight-field workflow run, positive decimal-string job ID, fixed `job_key: promote`, workflow SHA, checkout HEAD, origin-main target, `predecessor_attempt` and `predecessor_operation_results`. Complete bounded canonical workflow run/attempt/artifact/job discovery derives predecessor context; only the predecessor Attempt reference persists in A, with operation results in Observation 2.0. No CLI/env inputs are added. Source/build/main/checkout/workflow SHA must all equal the approved candidate.

Before the first remote mutation, revalidate the passing pre-decision record and seal a passing `pre-publication` Stage Gate Evidence record. Its current catalog descriptor subset is empty; the record still binds the same canonical candidate and catalog. Source, provenance, execution policy and remote-state checks remain required. No Sonar receipt or audit outcome is an admission or seal dependency.

The attempt must not require the `post-publication` record. That record cannot exist until the prerelease is public.

`seal_promotion_attempt` resolves raw Candidate/Catalog/Decision/Stage/remote bytes and hashes, reuses existing semantic sealers, requires `APPROVE` and exact authorization/pre-decision receipt, and re-derives the complete current-attempt classification. `authorization.execution_policy` uses model `GITHUB_JOB_TOKEN_SERVER_ENFORCED`, current run attempt/job ID/key, exact workflow SHA and SHA-256 derived from that tracked workflow blob. Reviewed configuration is top-level `permissions: {}`, exactly `actions: read` / `contents: write` for `promote`, with job-scoped `GITHUB_TOKEN: ${{ github.token }}`. This is source/configuration evidence, not observed grants or a future success guarantee. The retired log collector/report and probe have no active replacement or alias.

Keep execution-token provenance separate: record `github.token`, `GITHUB_APP_INSTALLATION_TOKEN`, `UNOBSERVED`, and null App/installation IDs. No human principal, PAT, auth-cache fallback, token introspection or alternate credential is introduced. Attempt 1.0/2.0, `authenticated_actor`, removed permission fields, stale/colliding records, extras and premature post-publication proof refuse.

The deterministic order is pre-decision ≤ Decision ≤ pre-publication ≤ O0 ≤ C0 ≤ Attempt, before candidate retention expiry. Retain/read back P0 (Stage/O0/optional genuine fragment), C0 and A before any write; A remains immutable. A source seal neither collects live facts nor proves GitHub authorization or actual public release.

## Preserve operation outcomes across recovery

Observation 2.0 requires at most two closed `operation_results` per existing advancement block. Each binds original attempt, operation, credential-free canonical intent hash, exact target, transmission time (null only for an unobserved predecessor crash), response kind/status/resource identity and complete exact postcondition. Only expected valid acknowledgement plus exact readback is `CONFIRMED`; only an ambiguous/unobserved original response with exact complete read-only reconciliation is `RECONCILED`. Reconciliation proves resulting state, not exclusive actor attribution or a full permission map.

A definite 401/403 or explicit Git rejection remains `DENIED` and terminates the sending attempt, even if another writer later produces matching state. A 403 rate limit does not prove missing permission; a 404 alone proves neither absence nor an authorization cause. 409/422 conflict or validation refusal stops without retry or clobber. Lost/truncated/malformed or potentially applied 5xx responses remain `UNKNOWN`; reconcile read-only and never resend the uncertain operation. A 502 starter asset or same-name collision is preserved, never deleted. ACK without matching exact readback cannot pass. `DENIED`, `COLLISION` or unresolved `UNKNOWN` refuses dependent continuation regardless of apparent resource completeness.

Independently discover complete earlier canonical run/artifact history for this candidate/destination. `predecessor_attempt` is null only when no earlier admitted attempt is established; otherwise resolve its immutable Attempt, original intents and last classification. A retained A with no conclusive later evidence is potentially sent. Missing/expired/ambiguous diagnostics do not prove no-send. Reconstruct the original known tag OID, not a new timestamp's OID; read-only reconciliation belongs in the new O0 with original operation identity, without rewriting historical UNKNOWN. Conflicting histories refuse.

A later independently admitted attempt may perform only genuinely remaining operations after exact predecessor reconciliation, or retry a positively established definitely-not-applied refusal once its cause is resolved under fresh context and safe exact state. It must preserve the denial and must not claim the denied sender performed a later observed operation. New run/attempt/step/request IDs cannot erase uncertainty. Positive retained step evidence that advancement never began may establish no-send; absent diagnostics cannot.

Three fixed advancement blocks remain. Every successful block retains and reads back O_N/optional genuine fragment, then C_N before the next block. Failed/denied/unresolved blocks may retain safe original intents and truthful diagnostics through explicitly enumerated files, but no later classification or mutation proceeds. No whole scratch-directory upload, hidden retry loop or extra publisher exists.

## Classify remote state before mutation

Every attempt creates a [Remote Observation 2.0](remote-observation.schema.json) and a [Remote Classification](remote-classification.schema.json). Both bind the attempt ID and exact candidate. The table applies only after original operation outcomes and predecessor history admit continuation; resource state alone is insufficient.

| State | Required facts | Allowed action |
| --- | --- | --- |
| `unstarted` | The matching annotated tag and release are absent. | Create the annotated tag at the candidate commit, then create the matching draft prerelease. |
| `tag_only` | The matching annotated tag peels to the candidate commit. No release exists. | Create only the matching draft prerelease. |
| `draft_empty` | The matching tag and draft prerelease exist. Both expected assets are absent. | Upload the approved archive and manifest. |
| `draft_partial` | The matching tag and draft prerelease exist. Every present expected asset has the approved name, size, and SHA-256. | Upload only missing approved assets. |
| `draft_complete` | The matching tag and draft prerelease exist. Both remote assets match the candidate. | Re-download and verify the assets, then publish the prerelease. |
| `published_complete` | The matching tag and published prerelease exist. Both remote assets match the candidate. | Do not mutate. Run post-publication proof. |
| `collision` | Any other state, unreadable record, unavailable retained artifact, or mismatch. | Refuse. A changed candidate needs a new version and tag. |

A retry starts with retained-byte verification, independently resolved predecessor history, a new Promotion Attempt 3.0 and new Observation 2.0. Only the Decision's named dispatcher may retry under the outcome rules above. The contract never permits a rebuild, `--clobber`, asset overwrite/deletion, release deletion/replacement, tag move/deletion/reuse, Python release action or PyPI publication.

## Close the post-publication stage

After the prerelease is public, create a [Remote Verification](remote-verification.schema.json) record and a fresh `post-publication` Stage Gate Evidence record. The remote verification and consumer proof must bind the same candidate, Decision, Promotion Attempt, remote observation, and remote classification.

The post-publication consumer proof uses fresh release downloads. It proves archive, manifest, and executable identities; the complete denial matrix; JSON-RPC-only stdout; clean EOF; and unchanged Python rollback. The proof must not use a source-tree executable, a local rebuild, or the retained download from before publication.

Run the separate exact-head Sonar audit only after actual publication. Preserve
its real red or blocked result without adding a required gate descriptor or
changing the functional consumer proof, immutable tag, or published artifacts.

## Hand off A1 only

[Program B Handoff](program-b-handoff.md) requires passing `pre-decision`, `pre-publication`, and `post-publication` Stage Gate Evidence records. It also requires `published_complete`, a passing remote verification, and a passing remote consumer proof.

The handoff proves only the A1 opt-in preview boundary. It does not start Program B or Program C. Program B and Program C still need their own separate authorization.
# Quickstart — Validate retained and public-remote A1 preview artifacts

This is the retained-artifact and public-remote consumer-proof procedure. It is
neither an execution receipt nor authority to approve, publish, tag, upload,
distribute, or select the preview as a public default. Both modes bind to the
same sealed candidate from GitHub Actions evidence; remote mode additionally
requires fresh anonymous bytes from that candidate's published GitHub prerelease.

The receipt shape is defined by the [Artifact Consumer Proof schema](contracts/artifact-consumer-proof.schema.json). The candidate and its retained files remain bound by the [Candidate Identity Record schema](contracts/candidate-identity.schema.json) and the [workflow contract](contracts/stateless-preview-workflow.md).

## Inputs

Use a Windows x64 validation machine with PowerShell, Python, GitHub CLI, and a
local, ordinary (non-reparse) fixture directory containing the contained C#
marker `PreviewMarker`. Create an empty ordinary download root, for example
`C:\preview-validation\downloads`.

Obtain the four expanded metadata references from the sealed candidate evidence;
do not construct them from local files, source output, a rebuilt archive, or a
later Actions run. Save them as `C:\preview-validation\candidate-references.json`:

```json
{
  "reference_schema_version": "1.0",
  "candidate_identity": {
    "repository": "<owner/repository>",
    "run_id": "<identity-run-id>",
    "artifact": {
      "id": "<identity-artifact-id>",
      "name": "<identity-artifact-name>",
      "sha256": "<artifact-sha256>",
      "retention": {
        "configured_days": 7,
        "expires_at": "<artifact-expiry-rfc3339>"
      }
    },
    "path": "<candidate-identity-file-path-within-artifact>",
    "sha256": "<candidate-identity-file-sha256>"
  },
  "archive": "<complete-retained-payload-file-reference>",
  "manifest": "<complete-retained-payload-file-reference>",
  "release_gate_catalog": "<complete-retained-catalog-file-reference>"
}
```

The `archive`, `manifest`, and `release_gate_catalog`
placeholders each stand for a complete object with the same reference shape as
`candidate_identity`: `repository`, `run_id`, `artifact` (`id`, `name`,
`sha256`, and `retention`), artifact-relative `path`, and file `sha256`.
Replace every placeholder with its sealed value. `archive` and `manifest` must
name the same retained payload artifact. The candidate identity, archive,
manifest, and catalog references must retain their exact repository, run,
artifact, retention, path, and SHA-256 values.

Both modes resolve candidate identity and catalog through GitHub Actions metadata
and artifact downloads. Candidate mode also downloads the retained archive and
manifest; remote mode downloads that pair only from the public GitHub prerelease.
A manually downloaded pair, cached publisher payload, extracted executable, or
source-tree executable is not a substitute input.

The shared retained transport is `ArtifactDownloader` with the expanded
`ArtifactFileReference` in `scripts/stateless_preview_artifact.py`. These CLI
references include artifact metadata (`id`, `name`, container `sha256`, retention).
Structural references inside review/Stage/Decision/Attempt/Verification/Handoff
records are compact: `repository`, `run_id`, `artifact_id`, `path`, `sha256`.
They are not interchangeable. Resolve compact locators to real metadata in the
effect-owning adapter; do not invent names, retention, digests or artifact IDs.

Keep three hash domains separate: a file reference's `sha256` hashes its original
raw bytes, expanded `artifact.sha256` hashes the downloaded Actions ZIP container,
and Candidate `build.artifact.sha256` hashes `archive_bytes || manifest_bytes`, in
that order. The archive, manifest and extracted executable also have independent
raw-file descriptors. Re-serializing JSON or substituting a container digest for
the payload-pair digest breaks the binding.

The workflow has canonical build, nonpublishing evidence-retention and promote
routes. Their source availability is not proof of live retention, server
authorization, successful promotion or public consumer completion. The obsolete
permission diagnostic/probe route is retired; its selector is rejected, not aliased.

Sonar receipts are not inputs to build admission, candidate sealing, or consumer
proof. Run the separate exact-head audit only after actual publication, as
described in [SonarQube onboarding](../../docs/SONARQUBE-ONBOARDING.md). Preserve
its real red or blocked outcome; it does not change the functional proof result.

## Prepare the unchanged Python rollback oracle

Prepare the established retained-Python consumer environment exactly as
described in [the A1 local-preview rollback procedure](../006-a1-local-preview/quickstart.md#reproducible-retained-python-rollback--product_works). It supplies `$consumerPython`, the disposable environment's Python executable, and `.agent/tmp/t001-retained-python-consumer.py`, whose invocation exercises the installed public `netcoredbg-mcp --project-from-cwd` journey.

This is the unchanged rollback oracle, not candidate proof. Do not reinstall,
replace, or reconfigure the Python package or its console entry point while
running it; remove only any explicit preview selection.

## Run candidate-mode proof

Run from the repository root. The output receipt path must not already exist;
the validator writes it once only. Replace the opaque receipt id and timestamp
with real values; `--recorded-at` must be an RFC 3339 UTC timestamp ending in
`Z`.

```powershell
python tests/preview/validate_preview_artifact.py candidate `
  --references C:\preview-validation\candidate-references.json `
  --download-root C:\preview-validation\downloads `
  --fixture-root tests\fixtures\PreviewSearchApp `
  --receipt-output C:\preview-validation\retained-artifact-proof.json `
  --receipt-id retained-proof-<opaque-id> `
  --recorded-at <utc-rfc3339-timestamp-ending-z> `
  --python-rollback-command $consumerPython `
  --python-rollback-argument .agent/tmp/t001-retained-python-consumer.py
```

Candidate mode downloads the identity record, archive, manifest, and release-gate
catalog into a fresh child of `--download-root`.
Before extracting anything, it verifies retained-artifact metadata and every
referenced raw file hash; it then verifies the Candidate Identity Record,
archive, manifest, and extracted executable equations. The extracted executable
is disposable verification material, never candidate authority.

On success the command prints exactly:

```text
PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED
```

and creates the schema-valid receipt at `--receipt-output`. On any rejected
input, download, hash, matrix, or rollback result it prints
`PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED` to stderr and creates no receipt.

## Run public-remote proof

Use the same four-reference bundle and unchanged rollback oracle. Supply the
actual positive numeric release ID from the matching published prerelease, not
an Actions run ID or a tag string. Create a separate empty ordinary download root
`C:\preview-validation\remote-downloads`; the receipt path must be new. From the
repository root, replace the receipt ID and timestamp as in candidate mode:

```powershell
python tests/preview/validate_preview_artifact.py remote `
  --references C:\preview-validation\candidate-references.json `
  --release-id <positive-github-release-id> `
  --download-root C:\preview-validation\remote-downloads `
  --fixture-root tests\fixtures\PreviewSearchApp `
  --receipt-output C:\preview-validation\remote-release-proof.json `
  --receipt-id remote-proof-<opaque-id> `
  --recorded-at <utc-rfc3339-timestamp-ending-z> `
  --python-rollback-command $consumerPython `
  --python-rollback-argument .agent/tmp/t001-retained-python-consumer.py
```

Both modes use the same validator and complete matrix.
`--python-rollback-argument` is repeatable. Neither mode accepts a user URL,
token, mirror, local payload override or a reduced matrix.

Remote mode uses
`download_public_release(candidate, release_id, destination)` from
`scripts/stateless_preview_public_release.py`, returning archive path, manifest
path and immutable remote origin. Its private stdlib HTTPS opener sends no
Authorization, inherited GitHub credentials, cookies or environment proxies.
This anonymous rule applies to public release downloads; the identity/catalog
Actions transport still uses GitHub CLI access to retained evidence.

The public downloader constructs endpoints from the validated repository and IDs.
It checks a public repository, nondraft prerelease, exact tag and target commit,
then dereferences the annotated Git tag object to that same candidate commit.
A lightweight tag or symbolic `target_commitish` alone is insufficient. Exactly
two uploaded assets must have distinct IDs and the candidate's names and sizes.
Each asset accepts HTTP 200 or validated HTTPS 302 delivery only through
`api.github.com`, `github.com` or `release-assets.githubusercontent.com`; downgrade,
unexpected host, redirect loop and unavailable anonymous access refuse.

Before extraction, bounded fresh archive/manifest reads must match their raw
SHA-256 descriptors and the ordered payload-pair hash; the extracted executable
must match the manifest. A nullable provider `digest` never replaces these byte
checks. Release/tag/asset identity is read again after download; identity changes
refuse, while unrelated provider counters such as `download_count` may change.
Only this verified extracted executable runs the full matrix below, with the
fixture's absolute path passed as `--project <absolute-fixture-root>`.

Success uses the same `PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED` terminal marker and
write-once receipt, with `proof_stage: "remote_release"` and exact remote asset
IDs. Refusal emits `PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED`, exits nonzero and
writes no receipt. An argument-parser usage error exits before proof execution.
Temporary downloads are disposable; the sealed receipt is the retained output.

## What both modes must prove

The validator launches only the verified extracted executable with the explicit
absolute fixture root, not CWD or an environment-selected project. This A1
read-only preview is not the compatibility proxy or a desktop debugger. It
performs the positive JSON-RPC journey itself:

1. `server/discover` exposes only the `tools` capability.
2. `tools/list` exposes exactly `find_code_symbol`.
3. `tools/call` finds `PreviewMarker` as a `class` and completes without an
   error result.
4. Stdout remains JSON-RPC-only; closing stdin exits cleanly, emits no
   cancellation result, and retains no state.

It also records the complete closed matrix: invalid `--project` launch roots;
hostile CWD and environment roots; containment/reparse escapes; invalid tool
arguments; an unreadable file; each resource ceiling; excluded protocol and
catalog methods; EOF and cancellation; and the unchanged Python rollback. Each
scenario must have one passing result with no partial output and no unintended
side effect. A missing, changed, or failed scenario rejects the proof.

The matrix has ten required scenario groups, in catalog order. Each must run;
required symlink/reparse or Windows file-lock setup failures refuse the proof.

| Scenario group | Required observation |
| --- | --- |
| `launch-cli-invalid-root` | Missing, relative, nonexistent, non-directory, UNC/device, reparse or duplicate project selection exits 64 with empty stdout and `PREVIEW_ROOT_INVALID` on stderr. |
| `launch-configuration-hostile-roots` | Hostile CWD, `NETCOREDBG_MCP_PROJECT` and `PREVIEW_PROJECT` do not override explicit project authority. |
| `contained-fixture-escape` | Reparse escape refuses without returning partial in-root matches. |
| `tool-input-invalid` | Missing/null/non-string/blank/oversized names, extra fields and invalid kinds return the closed `INVALID_TOOL_ARGUMENTS` result. |
| `file-system-unreadable` | Locked C# source returns `PREVIEW_SEARCH_UNREADABLE`, not partial matches. |
| `resources-ceilings` | File size, result count, aggregate bytes and entry-count ceilings each return `PREVIEW_SEARCH_BUDGET_EXCEEDED` without partial results. |
| `protocol-catalog-exclusions` | `initialize`, `resources/list` and `roots/list` return method-not-found; `start_debug` is an unknown tool, never a hidden runtime route. |
| `transport-eof-cancellation` | Empty EOF and cancellation followed by EOF exit cleanly without a cancellation result or retained state. |
| `valid-discovery-list-call` | Tools-only discovery, sole `find_code_symbol`, successful `PreviewMarker` class result, JSON-RPC-only stdout and clean EOF. |
| `rollback-python-default` | The unchanged installed-Python oracle reports its exact `PRODUCT_WORKS` result. |

## Python rollback outcome

Both modes invoke the supplied rollback command as part of the full matrix. The
retained Python consumer driver must exit successfully and report the exact
established `PRODUCT_WORKS` journey. The resulting receipt records that only
preview selection was removed and that the Python package, console entry point,
and default selector were unchanged. The driver must emit exactly
`{"product_works":true,"denominator":"5/5","tool_count":135,"stopped_at_entry":true}`
as a JSON object; a zero exit alone is not sufficient. The controller does not
uninstall the preview or alter client configuration on the operator's behalf.

The controller resolves the supplied command with the supported executable lookup
and prepends its directory to PATH only in the rollback child process. This makes
the unchanged driver's parent-side console lookup select the retained environment
before the MCP SDK applies its server environment. All inherited PATH entries and
the controller's own environment remain unchanged; no wrapper or driver change is
required.

The full rollback journey has a separate 120-second process budget, including
startup, the installed consumer journey, and shutdown. A correctly selected
retained-environment replay completed in 74.9 seconds; the former 30-second total
budget could not admit that valid journey. DAP/MCP per-call deadlines are unchanged.
This budget change does not improve or diagnose startup performance, and does not
itself prove T014: rerun the full retained-candidate matrix and unchanged oracle.

Do not substitute a local candidate build, a source-bin executable, or a later
artifact for this process. Do not treat a sealed retained-artifact receipt as
publication authority: T014 remains the separate live retained-candidate
execution and receipt-sealing task.

## Internal review and stage sealing

The pure library functions in [the artifact helper](../../scripts/stateless_preview_artifact.py)
validate supplied evidence. They do not generate reviews, perform Actions
admission, retain artifacts, or publish; the separate workflow adapters below
own event/API/retention effects:

- `seal_s2_s3_lens_result` admits one ordered, independent, exact-candidate lens.
- `seal_s2_s3_review_aggregate` revalidates all seven ordered lenses and derives
  `7/7/0` coverage rather than trusting supplied counts.
- `seal_independent_pr_review` admits the separate PR-review obligation, including
  the exact repository/commit and reviewer/author/producer separation. Both PR
  reviews and lenses require `checks`: 1..128 closed records with unique
  `check_id`, `result: {expected, observed}`, and `status`. The required count is
  the number of checks; the completed count includes only `PASS` checks whose
  expected and observed codes match. Both supplied counts must equal the derived
  counts, and every required check must complete. `EXPECT_REFUSAL/REFUSAL/PASS`
  is completed evidence of an expected refusal; `status: REFUSED` is incomplete.
  Findings are separate: a clean PR review or lens may have no findings, while
  any unresolved HIGH/CRITICAL finding refuses admission. Eleven completed
  checks and zero findings therefore prove `11/11/0` without INFO padding.
- `seal_stage_gate_evidence` re-hashes the five exact-source policy snapshots,
  resolves and hashes each required raw record, validates its type and candidate
  binding, and recomputes each gate and stage outcome.
- `seal_promotion_decision` revalidates the exact raw candidate and passing
  pre-decision stage, including its catalog/source and nested proof/review records.
  It accepts `APPROVE` or `DECLINE` as an immutable record with a closed decision
  author/dispatcher template; the decision time cannot precede the stage seal.
  It takes the same candidate, catalog, resolver, and repository inputs as stage
  sealing. It does not create an attempt, read current permissions or remote state,
  write a receipt, or perform publication.

Review calls require the Candidate Identity Record object, its raw bytes, and
its expanded retained-artifact reference. Compact review references must equal
the verified projection of that reference. Stage sealing additionally requires
the catalog object/raw bytes/reference, a raw-record resolver, and the Git
repository containing the candidate's policy snapshots. The functions reject
missing, duplicate, out-of-order, mismatched, unredacted, non-independent,
incomplete, or falsely passing evidence. They return immutable validated records
or raise `ValueError`; they do not write receipts or repair claims into `PASS`.

The closed stage subsets are three pre-decision records (retained consumer
proof, the seven-lens aggregate, and independent PR review), no pre-publication
records, and one post-publication remote consumer proof. A passing empty
pre-publication subset proves only that subset, not review completion or
publication authorization. Post-publication admission checks the remote receipt's
candidate-bound asset identities and complete consumer/denial/EOF/Python-rollback
matrix; the sealer does not itself download or execute those assets.

Decision sealing and its nonpublishing retention adapter are source implementation,
not a completed live S4 decision. The complete promote CLI/workflow now calls
pure immutable Attempt 3.0 revalidation through real upload/readback boundaries.
A sealed Decision or SIMULATED Attempt is not live publication authority.

### Revalidate one source-only Attempt 3.0

Call `seal_promotion_attempt(attempt, **stage_binding,
current_promotion_context=current_context)`, where `stage_binding` contains the
same candidate object/raw bytes/expanded reference, catalog object/raw bytes/
reference, raw-record resolver and repository root required by
`seal_stage_gate_evidence`. The required independent `current_context` has exactly:

```text
current_attempt_id, workflow_run, job_id, job_key, workflow_sha,
checkout_head, origin_main_target, current_github_actor,
current_github_triggering_actor, predecessor_attempt, predecessor_operation_results
```

`workflow_run` retains the classifier's eight fields. `job_id` is a positive
decimal string; `job_key` is `promote`. Current source/build/main/checkout/workflow
SHA agree. Both current actor maps contain numeric-string `id` and `login` and
must match the resolved Decision's dispatcher, not necessarily its author.
Supply current facts from trusted admission, never the checked Attempt/request.
The two predecessor fields are independently derived from complete bounded
canonical workflow run/attempt/artifact/job discovery, not dispatch or copied
Attempt claims. They are runtime context: A persists only `predecessor_attempt`;
reconciled operation results belong in Observation 2.0. No new CLI argument or
environment variable is added.

The API resolves/hashes the `APPROVE` Decision, exact pre-decision receipt,
PASS pre-publication Stage even for an empty subset, and Observation 2.0/remote
records, reusing existing sealers/classifier. `authorization.execution_policy`
contains model `GITHUB_JOB_TOKEN_SERVER_ENFORCED`, independently current
`run_attempt`, `job_key: promote`, positive `job_id`, `workflow_sha`, and
`workflow_blob_sha256` derived from exact tracked workflow bytes at that SHA.
Reviewed configuration keeps top-level `permissions: {}`, exactly `actions: read`
and `contents: write` for promote and literal job-token wiring. Source/blob equality
is not observed effective grants, token introspection or a success guarantee.
Chronology is pre-decision ≤ Decision ≤ pre-publication ≤ O0 ≤ C0 ≤ Attempt,
before required retention expiry. No report/log/grant projection remains.

`predecessor_attempt` is independently discovered, null only after complete
prior canonical-run/artifact discovery establishes no earlier admitted attempt.
Otherwise resolve its original immutable Attempt, intent and last outcome;
missing or ambiguous history is potentially sent, never proof of no-send.
Reconcile an uncertain original operation read-only in new O0 without rewriting
its UNKNOWN or resending it. A definite denial stops its sending attempt; only
a fresh independently admitted attempt may retry a definitely-not-applied refusal
after resolving its cause under fresh context and exact safe state.

The promote job supplies `GITHUB_TOKEN: ${{ github.token }}` to every command.
Execution provenance is fixed to `github.token`, `GITHUB_APP_INSTALLATION_TOKEN`,
`UNOBSERVED`, and null `app_id`/`installation_id`; initiators are not token owners.
Attempt 1.0/2.0, `authenticated_actor`, obsolete permission fields, extras,
stale/colliding remote records and premature post-publication proof refuse.
There is no dual reader or shim.
The API returns a detached deeply immutable record or raises `ValueError`;
it does not collect live facts, create a receipt or perform remote effects.

Schema-valid simulated test vectors are not actual reviewer or acceptance
receipts. Library tests do not complete the live retained-candidate journey,
independent reviews, live stage sealing, promotion decision, or external
publication. Those remain separate tasks; the Python default remains unchanged.

## Pure remote observation and classification API

`seal_remote_observation(observation, *, candidate_identity,
candidate_identity_bytes, candidate_identity_reference, current_attempt_id)`
validates and freezes the closed `remote-observation.schema.json` record.
It checks exact raw candidate/reference binding and the independent current
attempt ID. Structurally valid negative facts, including unavailable or expired
payloads and unreadable remote objects, remain observations rather than errors
or inferred absence. It does not fetch or verify remote bytes itself.

`classify_remote_observation(remote_observed_state_reference, *,
classification_id, classified_at, candidate_identity, candidate_identity_bytes,
candidate_identity_reference, current_attempt_id, current_workflow_run,
record_resolver)` resolves the exact raw observation, checks its hash and closed
form again, and derives one immutable `remote-classification.schema.json` record.
The resolver takes a compact artifact-file reference and returns previously
obtained bytes; this pure function adds no downloader, credential or network
producer. The separate publisher CLI owns those effects. `current_workflow_run` has exactly the existing
`promotion-attempt.schema.json#/$defs/current_promote_workflow_run` shape and
must come from the invoking runtime, not the observation under validation.

The classifier derives state/action/reason from constituent tag/release/asset,
retained-availability facts and original operation outcomes, never caller state
or approval. Observation 2.0 requires closed `operation_results`, at most two
for one block (tag+draft, archive+manifest or publication), empty O0 without a
predecessor. Each result binds original attempt/operation, credential-free intent
hash, exact target, sent time, response class/status/resource ID, postcondition
and outcome. Only valid acknowledgement plus exact readback is `CONFIRMED`;
only ambiguous/unobserved original response plus exact complete read-only
reconciliation is `RECONCILED`. DENIED/conflict cannot become success through
matching foreign state. Invalid documents/hash/context/time substitutions raise
`ValueError`; negative facts or DENIED/COLLISION/unresolved UNKNOWN refuse.
A matching published prerelease with admitted history permits only
`RUN_REMOTE_CONSUMER_PROOF`, never mutation.

Neither function consults the wall clock or adds an observation TTL. Payload
expiry is evaluated at `observed_at` and `classified_at`; classification cannot
precede observation. These times and the truth of the recorded readback remain
runtime/observer inputs. The stored observation has no workflow `run_attempt`,
so matching run ID and attempt ID cannot prove freshness across a rerun of that
run. The publisher derives a new attempt ID and creates observations for each
run attempt; full Promotion Attempt admission and retained readback precede
protected effects. An `ADMITTED` classification alone is not effect authority.

## Retain exact evidence through the existing workflow

The workflow source accepts `mode=build` (the default), `mode=evidence`, or
`mode=promote`. Build still requires `preview_version`, `preview_tag`, and
`retention_days`; these are optional empty strings in YAML so evidence requests
need not supply fake build inputs. A legacy request without `mode` uses build
admission; an explicitly empty/unknown mode, unknown operation, mixed fields, or
unsupported input refuses instead of succeeding with every job skipped. Promote
is the complete publisher described below, not an admission-only route.

For `evidence_operation=retain`, the original candidate build must be complete and successful at
its exact run attempt, workflow/ref/SHA and named build job. Obtain the real
candidate-bound consumer proof, seven-lens aggregate, and distinct independent
PR review before dispatch. Keep those three original byte sequences outside
tracked source. A passing source fixture or transported `FAIL`/`INCOMPLETE`
document is not that evidence, and transport must not rewrite it into `PASS`.

Prepare an `evidence_request` JSON object with exactly these fields:

| Field | Required source/value |
| --- | --- |
| `candidate_identity_reference` | Exact retained Candidate Identity file reference, not local candidate bytes. |
| `release_gate_catalog_reference` | Exact retained Catalog file reference from the successful source build. |
| `bundle_base64` | Base64 ZIP containing only `consumer-proof.json`, `s2-s3-review.json`, and `independent-pr-review.json`, preserving their original bytes. |
| `record_sha256` | Object with those three exact filenames mapped to their original raw-file SHA-256 values. |
| `decision` | Root's explicit candidate-bound `APPROVE` or `DECLINE` intent, not an outcome derived from passing checks. |
| `authorized_dispatcher` | Explicit closed `{id, login}` identity, separate from the actual decision author. |
| `retention_days` | String integer from `1` through `90`. |

The destination is already bound by the candidate. Do not add `decision_author`,
a future Stage/reference/time, a workflow/job name override, paths, URLs, scripts,
or publication parameters. Admission derives the real actor and rerun principals
from Actions/API facts and requires both `7106373` and `thebtf`; this identity
binding is not approval or the operator's personal click. A distinct dispatcher
is admitted only as the existing Decision contract permits. Even `DECLINE`
requires the existing passing pre-decision Stage contract.

The base64 string is at most 60,000 characters and its decoded ZIP at most
45,000 bytes; fixed-member, CRC/hash, strict JSON and decompression bounds still
apply. The whole actual `inputs` object, conservatively serialized as escaped
ASCII JSON including the nested request string, must fit 65,535 characters.
Oversize or semantically invalid evidence refuses; do not truncate or substitute
local paths. Raw file hashes, GitHub artifact-container digests, and
`SHA256(archive_bytes + manifest_bytes)` are three separate hash domains.

Once this source is integrated on canonical main, Root may send the request as
data on standard input using the [GitHub CLI JSON route](https://cli.github.com/manual/gh_workflow_run):

```powershell
$request = Get-Content -Raw C:\preview-validation\evidence-request.json
@{
  mode = 'evidence'
  evidence_operation = 'retain'
  evidence_request = $request
} | ConvertTo-Json -Compress | gh workflow run stateless-preview.yml --repo thebtf/netcoredbg-mcp --ref main --json
```

This command is a live dispatch, not a validation command. Root owns its timing
and the explicit decision; the procedure grants no new publication authority.
The runner reads the actual event file rather than interpolating request JSON
into a shell command. Build-only input fields must remain absent or empty.

The `retain_evidence` job has exact display name
`Retain canonical-main preview evidence` and only `contents: read`/`actions: read`.
It checks out the dispatched SHA, fetches live main, and admits the exact source,
workflow SHA and actor before any upload. It uses the house Python 3.12 action
and the existing stdlib helper, with no SDK, payload build or publisher:

```text
python scripts/stateless_preview_artifact.py admit-evidence
E0 upload: consumer-proof.json + s2-s3-review.json + independent-pr-review.json
python scripts/stateless_preview_artifact.py seal-evidence-stage
E1 upload: pre-decision-stage.json
python scripts/stateless_preview_artifact.py seal-evidence-decision
E2 upload: promotion-decision.json
python scripts/stateless_preview_artifact.py verify-evidence-retention
```

The four commands have no extra arguments. Admission alone supplies
`records_directory`, `stage_path`, `decision_path`, `records_artifact_name`,
`stage_artifact_name`, `decision_artifact_name`, and `retention_days`.
Paths stay below `RUNNER_TEMP` in the current run/attempt namespace. The job
enumerates exactly the three E0 filenames and the separate E1/E2 files; it never
uploads the whole scratch directory. All three uploads use `overwrite: false`,
`if-no-files-found: error`, and admitted retention. Upload IDs/digests reach
helpers only as `A1_EVIDENCE_{RECORDS,STAGE,DECISION}_ARTIFACT_{ID,DIGEST}`.
Tokens reach helpers only through `GITHUB_TOKEN`, never CLI arguments.

Each sealing command independently downloads the previous real upload and
revalidates metadata, ownership, expiry, container/raw hashes, provenance and
record semantics. Final verification independently resolves Decision, Stage and
the original records while the evidence run remains `in_progress`; the source
build alone must already be `completed/success`. There is no fourth upload or
future-ID cycle. The final summary's compact Decision reference is a retained
file identity, not current-attempt permission or publication authority.

## Dispatch the complete publisher after explicit retained approval

This procedure is for authorized later execution, not an executed command or a
new publication grant. Root first integrates and verifies the full source at
canonical SHA S, then uses its canonical build for a new payload, manifest,
Candidate and Catalog. Replay complete retained downloaded consumer/denial/EOF/
unchanged-Python proof, obtain S-bound seven-lens and distinct independent review,
seal the pre-decision Stage, then retain Root's new explicit S4 `APPROVE` or
`DECLINE`. Internal mechanism selection needs no operator click.

Preserve original `5f96014b930e269752b47e878f45f8d220b6bfc7`, build
`38960876498/1` and its APPROVE unchanged as original-only. No successor may
borrow that approval, even if executable bytes happen to match. Source/main,
checkout, build/manifest and workflow SHA must agree; changed policy snapshots
require the new Catalog and evidence. Derive the next unused preview ordinal
from current inventory before a new tag. Correct job-token configuration can
still be refused by GitHub; no PAT, auth cache, broader credential or token
introspection is a fallback. No permission probe or test publication is added.

Save the actual compact reference obtained from the retained Decision/readback
as `C:\preview-validation\promotion-decision-reference.json`. It has exactly:

```text
repository: the retained Decision's repository
run_id: its positive decimal-string producer run ID
artifact_id: its real positive decimal-string uploaded artifact ID
path: promotion-decision.json
sha256: the lowercase SHA-256 of that original raw file
```

Do not supply the Decision body, an arbitrary request path/URL, actor, grants,
source SHA, release/job/attempt ID, or extra Candidate/Stage references. The
helper resolves the existing Decision chain itself. `preview_tag` is checked
against that chain's candidate destination; a local candidate copy supplies only
this concurrency tag and creates no authority. The request stays data on stdin:

```powershell
$candidate = Get-Content -Raw C:\preview-validation\candidate-identity.json | ConvertFrom-Json
$decisionReference = Get-Content -Raw C:\preview-validation\promotion-decision-reference.json
@{
  mode = 'promote'
  preview_tag = $candidate.candidate.destination.tag
  promote_request = $decisionReference
} | ConvertTo-Json -Compress | gh workflow run stateless-preview.yml --repo thebtf/netcoredbg-mcp --ref main --json
```

Leave `preview_version`, `retention_days`, `evidence_operation`, and
`evidence_request` absent or empty. The actual escaped input object must still
fit 65,535 characters. Do not interpolate the request into shell code. No dispatch
shown here was performed by this source change.

The exact job is `promote` / `Promote canonical-main preview candidate`, with
`actions: read` and `contents: write`, exact checkout and no persisted credentials,
live main fetch, existing Python setup and job-scoped
`GITHUB_TOKEN: ${{ github.token }}`. It installs no SDK, builds nothing, and uses
only approved retained payload bytes. The house helper scrubs overriding
`GH_TOKEN`/enterprise/debug/Git environment and disallows auth-cache fallback.
It independently derives the current source/run/attempt/job and both initiators;
the Decision's named dispatcher must match them, not a request-supplied identity.

Native tag construction and push run in a private temporary bare Git repository,
not the checkout's transport configuration. The helper independently validates
the canonical object store, uses it only as a read-only alternate, disables
templates/hooks/proxies/redirects and supplies only the bound job token to the
explicit GitHub URL. The absent-tag lease, exact tag OID and durable intent remain
mandatory; checkout config is unchanged and uncertain writes are never resent.

The actual CLI/retention sequence is:

```text
admit-promote                         -> real pre-publication Stage and owned paths
observe-promote                      -> O0 and genuine draft-byte fragment if present
P0 upload                            -> Stage + O0 + optional genuine fragment
classify-promote --step 0             -> independent P0 readback, C0
C0 upload
seal-promote-attempt                  -> independent C0/P0 readback, immutable A
A upload
advance-promote --step 1              -> A/C0 readback, one legal action, O1/optional fragment
O1 upload; classify-promote --step 1; C1 upload
advance-promote --step 2              -> A/C1 readback, one legal action, O2/optional fragment
O2 upload; classify-promote --step 2; C2 upload
advance-promote --step 3              -> A/C2 readback, one legal action, O3/optional fragment
O3 upload; classify-promote --step 3; C3 upload
finish-promote --step N               -> A/final C_N readback, exact published references
```

Publisher commands above use `python scripts/stateless_preview_artifact.py`.
`admit-promote`, `observe-promote` and `seal-promote-attempt` accept no extra arguments.
Classification/finish require `--step` 0–3; advance requires 1–3. Numbers identify
fixed blocks, not retry counts or caller-selected operations. Once classification
is `published_complete`, remaining action blocks skip and finish uses that actual
step, including step 0 for initial published recovery. A remains unchanged.

The classifier alone chooses the legal action: `unstarted` creates/readbacks
the exact annotated tag and draft; `tag_only` creates only a draft;
`draft_empty`/`draft_partial` upload only missing approved assets;
`draft_complete` requires retained genuine pre-publication byte verification,
then fresh exact draft/asset readback before publishing. Collision, unreadable
or mismatched resources refuse. Each protected write repeats current-context,
retained-chain and remote checks. No rebuild, clobber, delete, tag move, release
replacement, automatic `APPROVE`, or Python/PyPI publication is permitted.

Every upload enumerates only actual helper-owned file paths, uses
`overwrite: false`, `if-no-files-found: error`, and Candidate's validated 1–90-day
retention. P0/C/A/O references come only from real upload output IDs/digests;
there is no future-ID cycle. YAML removes only the exact leading `sha256:` from
v7 container digests before helpers validate 64 lowercase hex digits. Container,
raw-file and payload hashes remain different domains. Every advance emits only
actual original-intent paths: `tag_intent_path`, `draft_intent_path`,
`archive_intent_path`, `manifest_intent_path`, `publish_intent_path`, before
transport. The existing final diagnostic upload explicitly lists all five from
each advance1/2/3 plus per-command `diagnostics_path`, including failed commands;
no broad scratch/payload/log glob is retained. Actions upload success is neither
a protected release write nor proof of contents authorization.

A definite refusal stops the sending attempt and remains DENIED, even if matching
foreign state is read later. Conflicts/validation refusal and starter-asset
collisions stop without delete/overwrite. A 403 rate limit does not establish
missing permission, and a 404 does not establish absence. A lost/malformed/
truncated or potentially applied response is UNKNOWN: bounded read-only
reconciliation may prove the exact known tag OID, draft ID/listing, downloaded
asset IDs/bytes or published release/assets. Only CONFIRMED or exactly RECONCILED
results permit advancement; acknowledgement without exact readback cannot pass.
Failed/denied/unresolved blocks may retain truthful O_N/intents/diagnostics but
cannot admit C_N or any later mutation. Never resend an uncertain operation in
the same or another run. A new attempt must independently resolve prior history
and perform only genuinely remaining operations or the definitely-not-applied
refusal retry described above. Preserve partial publication, never compensate.
Non-cancelling tag concurrency is not a transaction against external writers.

On success download the immutable `<attempt_artifact_name>-handoff` Actions
artifact from this actual run and preserve its `publisher-handoff.json`. It names
exact Candidate/Catalog/Decision/Stage/A/final O/C references and any real
predecessor pre-publication fragment. It is not Remote Verification, Consumer Proof,
post-publication Stage or Program B Handoff. Execution policy is resolved through
`promotion_attempt`, not a permission-report locator. Initial `published_complete`
recovery makes no mutations; without an actual earlier fragment it returns
`pre_publication_verification: null`, never a fabricated pre-publication PASS.
Root must resolve real predecessor evidence or leave that closeout incomplete.

Source tasks T027–T030 do not complete the published-candidate closeout in
T034–T035. After actual publication, require fresh public bytes and the full
consumer matrix, Remote Verification, the post-publication Stage and the
non-authorizing Program B Handoff. Use `seal_remote_verification` and
`seal_program_b_handoff` with the exact keyword inputs in the
[handoff contract](contracts/program-b-handoff.md#source-only-sealing-apis).
These APIs validate supplied original records, not live facts. They neither
create authority nor collect or reconstruct missing genuine pre-publication
fragments; absent fragments refuse even when classification is
`published_complete`. Publisher cache bytes cannot replace public consumer proof.

Controlled-loopback HTTP/MCP/EOF/tamper checks and SIMULATED record tests are
supporting source evidence, not public GitHub release receipts. Record source,
runtime and publication results separately; none substitutes for the others.

## Retain the published-candidate closeout

This is a later authorized procedure, not an execution receipt, new S4 approval
or Program B/C grant. Integrate the closeout adapter and publisher repair into
new canonical SHA S before its first successful publication; obtain new
S-bound Candidate/Catalog, consumer/review evidence and Root S4. Source
`526df406fb3225a4d23686ac4de920b8067698d2` and its earlier approval remain
historical-only. Do not redispatch an older candidate to borrow its approval.

1. After actual publisher P completes successfully, obtain the real compact
   `publisher-handoff.json` reference from its immutable Actions upload and
   verify the original bytes. It has exactly `repository`, `run_id`,
   `artifact_id`, `path`, `sha256`; `path` is `publisher-handoff.json`. A local
   handoff file, artifact name or invented ID is not that reference. Missing or
   null genuine pre-publication evidence refuses closeout, even when the remote
   state is already `published_complete`; do not republish to manufacture it.
2. Run [public-remote proof](#run-public-remote-proof) on Windows x64 against the
   actual public release, using the unchanged installed-Python oracle. Preserve
   the complete original PASS receipt bytes from that external execution. Its
   `remote_release` proof must cover the full consumer/denial matrix, clean EOF
   and unchanged Python rollback after the final published Observation.
3. Prepare `C:\preview-validation\closeout-request.json` as a JSON object with
   exactly the fields below. Put the original receipt into the ZIP under the
   fixed member name `remote-consumer-proof.json`; changing that member name
   from the local receipt filename must not reserialize or change its bytes.

| Field | Required source/value |
| --- | --- |
| `publisher_handoff_reference` | The real five-field retained reference from P, not a local path or the handoff body. |
| `bundle_base64` | Canonical base64 ZIP containing only the original `remote-consumer-proof.json` bytes. |
| `record_sha256` | Object with only `remote-consumer-proof.json` mapped to its original raw-file lowercase SHA-256. |

The existing bounds remain: full escaped dispatch input at most 65,535
characters, base64 at most 60,000 characters, ZIP at most 45,000 bytes and original
record at most 128 KiB. Strict JSON/base64, fixed-member, path, duplicate,
metadata, CRC, EOF and raw-hash checks still apply. Do not supply retention,
candidate/context objects, actors, timestamps, URLs, execution paths, future
artifact IDs or ready-made Verification/Stage/Handoff records.

4. While canonical main still equals S, dispatch a separate closeout run Q
   through the existing data-on-stdin route:

```powershell
$request = Get-Content -Raw C:\preview-validation\closeout-request.json
@{
  mode = 'evidence'
  evidence_operation = 'closeout'
  evidence_request = $request
} | ConvertTo-Json -Compress | gh workflow run stateless-preview.yml --repo thebtf/netcoredbg-mcp --ref main --json
```

Leave `preview_version`, `preview_tag`, `retention_days` and `promote_request`
absent or empty. The local request file only transports JSON; it never replaces
a durable Actions reference. This is a live dispatch owned by Root, not a check
executed by this source change. Both Q initiators must match the approved
dispatcher. Q is independently admitted from its current Actions/API/Git facts;
P is independently reconstructed from the completed exact publisher attempt,
tracked workflow S, original artifacts and predecessor history before P.
Attempt/Observation/Classification retain P; new retained closeout references
name Q. The pure sealers receive P through `current_promotion_context`, never Q
masquerading as P or context copied from a record. P/Q workflow, source/build,
checkout and fetched/live main all remain S.

The read-only `retain_closeout` / `Retain canonical-main preview closeout` job
uses exact checkout without persisted credentials, live main fetch, Python 3.12
and job-scoped `github.token`. It neither builds nor installs the product, runs
Python rollback, creates a Decision, mutates a release or starts Program B/C.
It preserves the external receipt and separately downloads fresh anonymous
public archive/manifest bytes to create the genuine post-publication byte
fragment. Publisher caches and retained payloads cannot replace that download.

```text
admit-closeout                 -> R0 upload: original remote-consumer-proof.json
                                           + post-publication-verification.json
seal-closeout-verification     -> R0 readback; R1 upload: remote-verification.json
seal-closeout-stage            -> R1/dependency readback; R2 upload: post-publication-stage.json
seal-closeout-handoff          -> R2/R1/lineage readback; R3 upload: program-b-handoff.json
verify-closeout-retention      -> new-directory R3 and recursive dependency readback
```

Each command uses `python scripts/stateless_preview_artifact.py` with no extra
arguments. All four uploads use `actions/upload-artifact@v7`, `archive: true`,
`overwrite: false`, `if-no-files-found: error` and Candidate-derived 1–90-day
retention. Only the listed helper-owned files are uploaded. Real upload outputs
flow through `A1_CLOSEOUT_{RECORDS,VERIFICATION,STAGE,HANDOFF}_ARTIFACT_{ID,DIGEST}`;
only leading `sha256:` is removed before strict lowercase digest validation.
The existing `seal_remote_verification`, `seal_stage_gate_evidence` and
`seal_program_b_handoff` remain pure and keep their schemas/signatures.
R2 references the R0 Catalog-required remote proof, not a new Verification
field; R1 is its adapter-order dependency. See the
[exact output interface](contracts/stateless-preview-workflow.md#fixed-cli-four-upload-barriers-and-final-readback).

Only after successful independent R3/graph readback and fresh Q/live-main
revalidation does the final step emit `program_b_handoff_reference` in action
output and `GITHUB_STEP_SUMMARY`. This is the real compact reference to R3, not
a local receipt or promised future upload. It keeps
`completion_scope: A1_OPT_IN_PREVIEW_COMPLETE` and both Program B/C
authorization-required flags true. Source fixtures or a green run alone cannot
close A1; Root must retain the actual publication, original public consumer/
Python-rollback proof and all four real artifact references with readback.

On failure, preserve public state and any uploaded artifacts. Do not move tags,
replace assets, republish or automatically retry. A later separately admitted Q
still needs the same canonical S. New retention does not renew older artifacts;
the dependency chain is usable only until its earliest expiry. The Python
default, package and console entry point remain unchanged.

## Historical permission experiment — no active probe

The former one-time timing experiment completed with a negative log-availability result.
Actual [run `38937481596`, attempt 1](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/attempts/1)
used source/workflow SHA `541b57007b0f56c0bfabe9bd1dd94a20daeac6df`
(merged PR #333) and [job `117096512282`](https://github.com/thebtf/netcoredbg-mcp/actions/runs/38937481596/job/117096512282).
Both samples started at the observed offsets 0.0 and 60.0 seconds and returned
`UNAVAILABLE` / `CURRENT_JOB_LOGS_UNAVAILABLE`, exit 1, empty grants and no log
hash. Bounded metadata reads confirmed the same job was `in_progress` with null
conclusion after each sample. Observation completed at 62.062 seconds; this is
a completed negative experiment, not a qualified current-job grant or a PASS
for promotion, retention, publication or the final consumer journey.

Root verified the original report hashes. Preserve artifact ID `12061642440`,
named `stateless-preview-current-job-permissions-38937481596-1`, and its separate
`current-job-permissions-t0.json`, `current-job-permissions-t60.json` and
`timing-summary.json`. Local original bytes remain under
`.agent/runs/a1-artifact-20261016/permission-timing-live-38937481596/`; Root's
verdict is `permission-timing-live-root-verdict.json` in the parent directory.
The earlier negative run `38907673130` and all positive/negative source and
watchdog receipts, including private failed scratch, remain unchanged. The
consumed driver, watchdog and their two tracked test files are retired, not
promoted into a retry feature or reusable wrapper. Their source remains in
PR #333 / that merge commit; historical receipts remain evidence for their
recorded source, not proof of later changes.

The accepted amendment deliberately removes that pre-write log attestation;
it does not turn either historical `UNAVAILABLE` result into PASS. The former
`RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION` claim remains historical only.
The collector and permission-probe job are retired after surviving current-job
selection moves to the canonical helper. Removed `permission-probe` requests
fail closed, not as retain/promote aliases. No replacement probe, third sample,
extended window, PAT/auth-cache fallback or test mutation is scheduled.

These samples prove neither universal impossibility, completion-only semantics
nor a log-availability SLA. The new exact-source policy does not claim observed
effective grants. GitHub authorizes real intended operations and their responses
plus exact readback establish outcomes. Source changes alone establish neither
actual publication nor fresh public consumer/installed-Python rollback success.

## Root validation and remaining evidence

After all source owners finish, Root runs the focused integrated owners in the
existing project test environment, not a child agent's midflight check:

```powershell
python -m pytest tests/test_stateless_preview_artifact.py tests/preview/test_remote_preview_artifact.py -q
```

Root must also check actionlint/YAML parsing and token routing, unchanged build
gates, exact upload file sets, digest normalization and permissions; then run the
actual CLI owner, retained-gate/API-edge smoke and current publisher diff review.
The pre-implementation unknown-mode/unknown-input assertions were real RED in
`.agent/runs/a1-artifact-20261016/evidence-cli-dispatch-red.xml`; this procedure
does not invent a later PASS. T023/T026 remain unclosed until integrated proof.
Source/fixture evidence is separate from actual server outcomes, live
retention/readback, exact-candidate approval, mutation and fresh public consumer
proof. Negative branches not encountered live stay `NOT_OBSERVED_LIVE`; never
induce them with a probe publication or credential change. Final successor
Candidate/Catalog/review/Root S4 and remote closure need real receipts.
Python default/rollback and Program B/C remain unchanged.


# Quickstart — Validate an A1 retained preview artifact

This is the retained-artifact consumer-proof procedure. It is neither an
execution receipt nor authority to approve, publish, tag, upload, distribute,
or select the preview as a public default. Its only candidate authority is the
downloaded GitHub Actions evidence named by a structural reference bundle.

The receipt shape is defined by the [Artifact Consumer Proof schema](contracts/artifact-consumer-proof.schema.json). The candidate and its retained files remain bound by the [Candidate Identity Record schema](contracts/candidate-identity.schema.json) and the [workflow contract](contracts/stateless-preview-workflow.md).

## Inputs

Use a Windows x64 validation machine with PowerShell, Python, GitHub CLI, and a
local, ordinary (non-reparse) fixture directory containing the contained C#
marker `PreviewMarker`. Create an empty ordinary download root, for example
`C:\preview-validation\downloads`.

Obtain the four structural references from the sealed candidate evidence; do
not construct them from local files, source output, a rebuilt archive, or a
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

The validator resolves every reference itself with GitHub Actions metadata and
artifact downloads. A manually downloaded archive, manifest, extracted
executable, or source-tree executable is not an input and cannot be proof.

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

## What candidate mode proves

The validator launches only the verified extracted executable with the explicit
fixture root. It performs the positive JSON-RPC journey itself:

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

## Python rollback outcome

Candidate mode invokes the supplied rollback command only after the matrix. The
retained Python consumer driver must exit successfully and therefore report the
established `PRODUCT_WORKS` journey. The resulting receipt records that only
preview selection was removed and that the Python package, console entry point,
and default selector were unchanged.

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

The library functions in [the artifact helper](../../scripts/stateless_preview_artifact.py)
validate supplied evidence; they do not add review or promotion CLI commands:

- `seal_s2_s3_lens_result` admits one ordered, independent, exact-candidate lens.
- `seal_s2_s3_review_aggregate` revalidates all seven ordered lenses and derives
  `7/7/0` coverage rather than trusting supplied counts.
- `seal_independent_pr_review` admits the separate PR-review obligation, including
  the exact repository/commit, reviewer/author/producer separation, and completed
  denominator derived from typed scoped finding records. A clean lens may have
  no findings; a positive PR-review denominator cannot be proved by an empty
  scoped-record set under the current contract.
- `seal_stage_gate_evidence` re-hashes the five exact-source policy snapshots,
  resolves and hashes each required raw record, validates its type and candidate
  binding, and recomputes each gate and stage outcome.

Review calls require the Candidate Identity Record object, its raw bytes, and
its expanded retained-artifact reference. Compact review references must equal
the verified projection of that reference. Stage sealing additionally requires
the catalog object/raw bytes/reference, a raw-record resolver, and the Git
repository containing the candidate's policy snapshots. The functions reject
missing, duplicate, out-of-order, mismatched, unredacted, non-independent,
incomplete, or falsely passing evidence. They return immutable passing records
or raise `ValueError`; they do not write receipts or repair claims into `PASS`.

The closed stage subsets are three pre-decision records (retained consumer
proof, the seven-lens aggregate, and independent PR review), no pre-publication
records, and one post-publication remote consumer proof. A passing empty
pre-publication subset proves only that subset, not review completion or
publication authorization. Post-publication admission checks the remote receipt's
candidate-bound asset identities and complete consumer/denial/EOF/Python-rollback
matrix; the sealer does not itself download or execute those assets.

Schema-valid simulated test vectors are not actual reviewer or acceptance
receipts. Library tests do not complete the live retained-candidate journey,
independent reviews, live stage sealing, promotion decision, or external
publication. Those remain separate tasks; the Python default remains unchanged.

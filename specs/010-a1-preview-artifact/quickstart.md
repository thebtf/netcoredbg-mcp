# Quickstart — Validate an A1 opt-in preview artifact

This guide defines the planned consumer validation procedure after implementation.
It is not an execution receipt, release evidence, or approval to distribute a
preview. Use one Candidate Identity Record as the authority for every step.
The record structure is defined by the [Candidate Identity Record schema](contracts/candidate-identity.schema.json); the proof receipt must validate against the [Artifact Consumer Proof schema](contracts/artifact-consumer-proof.schema.json). The complete identity model remains authoritative in the [data model](data-model.md), the dispatch/admission boundary is fixed by the [workflow contract](contracts/stateless-preview-workflow.md), and the reasoning for an independently downloaded proof is in [Research decision 3](research.md#decision-3--prove-only-independently-downloaded-artifact-bytes).

## Prepare the validation machine

Before you start, provide all of the following:

- A Windows x64 machine with PowerShell, GitHub CLI, SHA-256 support, and an MCP stdio client that can send raw JSON-RPC requests.
- The unchanged installed Python `netcoredbg-mcp` command and its established consumer journey.
- One local, ordinary, non-reparse project directory that contains a contained C# symbol. The examples call the symbol `PreviewMarker`.
- The candidate's immutable Candidate Identity Record, retained archive, and retained manifest. Obtain the archive and manifest by downloading the retained build artifact. Do not use an archive, executable, manifest, or build output from the source tree.
- A separate empty validation directory, such as `C:\preview-validation`. Do not extract the archive over an earlier candidate.

Treat an unavailable or expired retained artifact as a failed candidate. Do not rebuild it. Create a new candidate that has a new identity record and repeat this guide.

## Download and verify the retained candidate

Start from the sealed Candidate Identity Record reference emitted by the build, a later Stage Gate Evidence record, or a Promotion Decision: its repository, run ID, identity-artifact ID/name, normalized record path, and expected raw-record SHA-256. These identifiers are not credentials, but an arbitrary local JSON file is not a candidate authority.

```powershell
# Download and hash the separately retained Candidate Identity Record first.
gh run download <identity-run-id> --repo <owner/repository> --name <identity-artifact-name> --dir C:\preview-validation\identity
Get-FileHash -Algorithm SHA256 C:\preview-validation\identity\<candidate-identity-file>

# Parse only the verified record, then download the exact payload artifact it names.
gh run download <payload-run-id> --repo <owner/repository> --name <payload-artifact-name> --dir C:\preview-validation\retained
Get-FileHash -Algorithm SHA256 C:\preview-validation\retained\<archive-name>
Get-FileHash -Algorithm SHA256 C:\preview-validation\retained\<manifest-name>
Expand-Archive -LiteralPath C:\preview-validation\retained\<archive-name> -DestinationPath C:\preview-validation\extracted
Get-FileHash -Algorithm SHA256 C:\preview-validation\extracted\<executable-name>
```

After implementation, run the artifact-consumer harness against the downloaded archive, downloaded manifest, verified Candidate Identity Record file, and fixture root. Its output must validate as an Artifact Consumer Proof Receipt; the [workflow contract](contracts/stateless-preview-workflow.md) defines the semantic checks that occur before a receipt can support S2/S3 review.

```powershell
python tests/preview/validate_preview_artifact.py candidate --archive C:\preview-validation\retained\<archive-name> --manifest C:\preview-validation\retained\<manifest-name> --candidate-identity C:\preview-validation\identity\<candidate-identity-file> --fixture-root C:\preview-validation\fixture
```

The post-implementation command must reject the candidate before launch unless all of the following hold:

1. The identity record names the downloaded build run and source revision.
2. The manifest has the required version, tag, commit, `win-x64` RID, archive, and executable fields defined by the [inherited manifest schema](../005-stateless-preview/contracts/preview-manifest.schema.json).
3. The archive and manifest names, byte sizes, and SHA-256 values match the Candidate Identity Record.
4. The extracted executable's byte size and SHA-256 value match both the archive member and the manifest.
5. The tag, archive name, and manifest name satisfy the verifier equations in the [parent research](../005-stateless-preview/research.md#artifactverifier-equations).

Do not activate the preview when any check fails. Preserve the installed Python route unchanged.

## Run the exact local opt-in journey

Start only the extracted executable. The command must name the selected project explicitly.

```powershell
& "C:\preview-validation\extracted\<executable-name>" --project "C:\preview-validation\fixture"
```

Use the MCP stdio client to send `server/discover`, `tools/list`, and a `tools/call` for `find_code_symbol` with `{"name":"PreviewMarker","kind":"class"}`. Every request includes this request-local `_meta` object:

```json
{
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientInfo": {"name": "preview-validation", "version": "1.0"},
  "io.modelcontextprotocol/clientCapabilities": {}
}
```

Record the following outcomes:

1. `server/discover` and `tools/list` work on a fresh process. The listed catalog contains exactly `find_code_symbol`.
2. The valid call returns the complete `find_code_symbol` result. Its file is root-relative, results are deterministically ordered, and stdout contains only JSON-RPC traffic.
3. The process has no DAP session, Native Scene, bridge, artifact, Python, mux, HTTP, remote transport, or shared-process route.
4. Close stdin after the valid exchange. The process exits within its implemented bounded shutdown contract, produces no cancellation result, and retains no state.

The protocol and result details belong to the [parent A1 specification](../005-stateless-preview/spec.md#modern-mcp-contract) and its [exact tool result contract](../005-stateless-preview/spec.md#exact-tool-result-contract). Do not copy a source-tree test result into the candidate receipt.

## Execute the complete denial matrix

Use the same downloaded archive, extracted executable, identity record, and fixture root for every trial. Execute every row and every case in the [required negative and containment matrix](../005-stateless-preview/spec.md#required-negativecontainment-matrix). The artifact-consumer harness must record one outcome for each trial. A missing row is not a pass.

The matrix includes launch CLI validation, authority attempts through CWD, environment, client roots, and URI input, containment escapes, tool-input failures, filesystem failures, every resource ceiling, excluded protocol/catalog routes, EOF, cancellation, and rollback. Expected outcomes remain authoritative in that linked matrix. In particular:

- Invalid launch roots exit with code `64`, write exactly `PREVIEW_ROOT_INVALID\n` to stderr, write zero stdout bytes, and read no root content.
- Reparse, outside-root, and sibling-worktree entries return the closed `PREVIEW_PATH_REFUSED` result with no target content or partial output.
- Invalid tool arguments, unreadable paths, and every resource ceiling return only their documented closed errors, with no partial result.
- Legacy or excluded methods do not dispatch a route or cause a file or process side effect.
- EOF and cancellation leave no retained state.

Stop the candidate proof when any trial differs from its documented outcome. Do not replace the candidate with a local rebuild or a later retained artifact.

## Prove Python rollback in the same session

After the preview process exits, remove only the explicit preview selection or local preview configuration. Do not uninstall, replace, reconfigure, or rebuild the Python package.

Run the existing installed Python consumer journey through its public `netcoredbg-mcp` command. It must reach its established `PRODUCT_WORKS` outcome. Record that the Python command, default selection, package, and rollback journey were unchanged. A failed rollback leaves the candidate ineligible for promotion.

## Hand off S2 and S3 review

After the retained-byte consumer journey, full denial matrix, clean EOF, and Python rollback pass, seal an Artifact Consumer Proof Receipt. Then seal the exact seven-lens aggregate in [s2-s3-review.schema.json](contracts/s2-s3-review.schema.json) and the distinct [independent PR review](contracts/independent-pr-review.schema.json). Each record names the same Candidate Identity Record and contains bounded IDs/codes only.

Use the [Release Gate Catalog](contracts/release-gate-catalog.schema.json) to derive the `pre-decision` descriptor subset. Seal a passing [Stage Gate Evidence](contracts/stage-gate-evidence.schema.json) record for that subset. It includes retained proof, the seven-lens aggregate, independent PR review, and candidate exact-head Sonar. It does not include post-merge Sonar or remote proof.

Apply the named project gates in [ADR-004](../../docs/adr/ADR-004-stateless-preview.md), the [Program map S3/S4 contract](../../.agent/runs/python-removal-strangler-program-v1/stateless-convergence-program-v4.md#s3s4-security-contract), [the release protocol](../../docs/RELEASE-PROTOCOL.md), and the platform **Security Review** wiki (`nvmd-ai/wiki/security-review.md`). The catalog resolver maps each obligation to a stage descriptor or an explicit bounded inapplicability disposition. Source-run acceptance or a local artifact proof cannot replace any required gate.

## Keep S4 as a separate decision

After the pre-decision Stage Gate Evidence passes, an authorized release decision records exactly one `APPROVE` or `DECLINE` in the [Promotion Decision schema](contracts/promotion-decision.schema.json). It binds the canonical merged-main Candidate Identity Record, pre-decision evidence, decision author, and explicitly authorized GitHub dispatcher. It does not require post-merge or remote proof, and it does not name a consuming promotion run.

A `DECLINE` ends at local proof. Do not dispatch promotion, create a tag, create a release, upload an asset, or change the Python route. An `APPROVE` permits a future manual `stateless-preview.yml` promotion only when the named dispatcher creates a fresh [Promotion Attempt](contracts/promotion-attempt.schema.json). The attempt revalidates pre-decision evidence and requires one passing `pre-publication` Stage Gate Evidence record, including post-merge exact-head Sonar for the same canonical main commit, before it creates a remote object.

The Promotion Attempt then creates a fresh [remote observation](contracts/remote-observation.schema.json) and [remote classification](contracts/remote-classification.schema.json). The [promotion recovery contract](contracts/promotion-recovery.md) selects only `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, or `collision`. Never overwrite an asset, move/delete a tag or release, rebuild, or treat mismatched remote bytes as recoverable.

## Re-verify published prerelease bytes

After approved promotion reports a published prerelease, download its archive and manifest into a new directory. Do not reuse the retained download or extracted executable.

```powershell
gh release download <preview-tag> --pattern <archive-name> --dir C:\preview-validation\remote
gh release download <preview-tag> --pattern <manifest-name> --dir C:\preview-validation\remote
Get-FileHash -Algorithm SHA256 C:\preview-validation\remote\<archive-name>
Get-FileHash -Algorithm SHA256 C:\preview-validation\remote\<manifest-name>
Expand-Archive -LiteralPath C:\preview-validation\remote\<archive-name> -DestinationPath C:\preview-validation\remote-extracted
Get-FileHash -Algorithm SHA256 C:\preview-validation\remote-extracted\<executable-name>
```

After implementation, run the artifact-consumer harness with the newly downloaded remote archive, remote manifest, the approved Candidate Identity Record, and the fixture root:
```powershell
python tests/preview/validate_preview_artifact.py remote --archive C:\preview-validation\remote\<archive-name> --manifest C:\preview-validation\remote\<manifest-name> --candidate-identity C:\preview-validation\candidate-identity.json --fixture-root C:\preview-validation\fixture
```

Replay the valid journey, the complete denial matrix, clean EOF, and the unchanged installed Python rollback journey. The remote archive, manifest, and executable must match the approved identity exactly. A mismatch is a promotion failure, not a reason to overwrite, move, delete, or rebuild an asset.

## Record the Program B handoff condition

After remote proof and all catalog gates pass, produce a closed [Program B Handoff](contracts/program-b-handoff.schema.json) through the semantic admission contract in [program-b-handoff.md](contracts/program-b-handoff.md). Its producer resolves and hashes every candidate, decision, remote observation/classification, remote verification, proof, catalog, and gate-evidence reference before sealing the record.

The handoff is valid only when it contains all of the following:

- One explicit `APPROVE` decision and a fresh matching Promotion Attempt with current dispatcher/run/permission evidence.
- Passing `pre-decision` and `pre-publication` Stage Gate Evidence records for the canonical merged-main candidate, including post-merge Sonar scanned/tag target equality before publication.
- A published prerelease with remote archive, manifest, and executable bytes that match the Candidate Identity Record.
- A passing `post-publication` Stage Gate Evidence record, including fresh remote consumer/byte proof, full denial matrix, clean EOF, and unchanged Python rollback.
- Literal boundaries that `program_b_authorization_required` and `program_c_authorization_required` are both `true`; Program B still needs separate authorization for stateful/UI/DAP/remaining-route work, and Program C still owns default selection, package cutover, deprecation, and Python retirement.

A declined, unreviewed, incomplete, expired, locally proved only, unpublished, policy-drifted, mismatched, or remotely unverified candidate does not satisfy this handoff condition. The handoff proves only A1 completion; it does not start Program B or Program C.

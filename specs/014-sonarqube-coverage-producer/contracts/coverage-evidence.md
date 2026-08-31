# Coverage evidence contract

This contract defines the evidence the future exact-head runner must accept before scanner end and record after analysis. It does not define a release action. Diagnostic `release_intent` is always `none`.

## Admit execution before scanner begin

Before the runner starts a scanner transaction, it must:

1. Resolve `Wave2ClosureEntryV1` only from the tracked `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json` source and validate it against [wave2-closure-entry-v1.schema.json](wave2-closure-entry-v1.schema.json).
2. Verify `release_intent: none`, the accepted candidate, closure receipt hash, and PR #289 head identity. At runtime, derive `observed_main_sha` and `artifact_commit_sha`, prove the PR merged through GitHub/workflow evidence or first-parent history, and require candidate/artifact ancestry to observed main.
3. Resolve and version-check `uv`, `bash`, and `dotnet`.
4. Evaluate each fixed project for `net8.0`, direct private `coverlet.msbuild` `10.0.1`, `Microsoft.NET.Test.Sdk` `17.12.0`, and VSTest selection.
5. Refuse MTP, including `TestingPlatformDotnetTestSupport` activation and Microsoft Testing Platform references.

A failed entry or preflight emits a planned-stage failure. It has zero scanner-begin calls and zero run-root claims.

## Own one transaction

After entry and preflight succeed, `scripts/run_sonarqube_exact_head.py`:

1. Captures one clean detached head and pre-analysis identity.
2. Derives the pure `CoveragePlan` and two runtime scanner properties.
3. Starts SonarScanner with those properties.
4. Claims the UUID root and canonical marker exclusively, then writes a hash-bound resolved Wave-2 entry copy under that root.
5. Builds the retained broad scanner inventory.
6. Invokes the private `build/coverage.sh` producer with an enumerated plan and scrubbed environment.
7. Validates the Python final report and five private .NET Cobertura inputs.
8. Normalizes the fixed .NET inputs into one final .NET Cobertura report and validates it.
9. Checks the post-producer head and ends SonarScanner only after step 8 succeeds.
10. Binds report-task, Compute Engine, canonical analysis identity, complete component pages, complete diagnostic inventory, receipt evidence, and cleanup.

The shell receives no scanner credentials and performs no scanner, API, discovery, normalization, validation, acceptance, or receipt behavior.

## Marker and report identities

The marker validates against [coverage-run-marker.schema.json](coverage-run-marker.schema.json). It binds the tracked Wave-2 source and resolved-copy hash plus two final reports and five ordered private producer inputs. Scanner arguments use only these final paths:

```text
/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/<run-id>/python/coverage.xml
/d:sonar.cs.cobertura.reportsPaths=.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml
```

The final paths are slash-relative. Producers use absolute paths below the claimed root. The runner rejects an alternate path, static XML property, report glob, symlink, reparse point, URI, traversal, duplicate normalized path, or report outside the root.

## Admit Python evidence

The runner accepts `python/coverage.xml` only when it has a `coverage` root, positive line and branch denominators, ordered counts, and a nonempty sorted unique source set. Every mapping must resolve exactly once to a tracked regular `.py` file below `src/netcoredbg_mcp`. URI, absolute, escape, missing, reparse, duplicate-normalized, and test-only paths fail closed.

## Admit .NET inputs and normalize one final report

The producer runs exactly the five projects named in [architecture.md](../architecture.md#fixed-net-producer-inventory). Each project restores and tests without `--no-build`, filters, exclusions, thresholds, or merge switches. It emits a private Cobertura input at its planned input prefix.

The runner validates each private input before normalization. It requires a `coverage` root, a positive line denominator, ordered counts, at least one tracked production `.cs` mapping, and no unsafe path. The aggregate private input branch denominator must be positive. The Stateless input must map production Stateless source and preserve the selected DLL/PDB bytes.

The runner normalizes the five inputs in marker order. It canonicalizes source paths, unions coverage facts by canonical source path, line, and condition ordinal, and emits `.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml` in deterministic lexical order. A fact is covered when any input covers it. The output must have positive line and branch denominators and a source set equal to the validated input union. The five inputs are not scanner report identities.

## Gate scanner end

```text
wave2-entry -> preflight -> begin -> claim -> build -> produce -> normalize -> validate -> post-producer-head -> end
```

Any failure through `post-producer-head` is a blocked transaction with zero scanner-end calls. Cleanup may run after foreground producers terminate, but cleanup cannot make scanner end legal.

## Bind analysis and diagnostic inventory

After scanner end, the runner requires one canonical exact-head analysis identity. Submitted analysis and every current-analysis observation must equal it. It then requires positive aggregate coverage values, the unchanged `new_coverage` condition `OK` at threshold `80`, and complete component paging with positive mapped contributions for both final language source sets.

Before `DIAGNOSTIC_COMPLETE`, the runner writes a create-new artifact that validates against [diagnostic-inventory-v1.schema.json](diagnostic-inventory-v1.schema.json). It retains all paginated issue and hotspot records, routing fields, counts, key digests, and identity. The receipt binds the artifact's relative path, bytes, and SHA-256. The runner refuses incomplete or count-only inventory evidence.

## Receipt rule

All roles validate against [exact-head-receipt-v3.schema.json](exact-head-receipt-v3.schema.json). Diagnostic records can be `DIAGNOSTIC_COMPLETE` or `BLOCKED` and always have `release_intent: none`. Candidate and post-merge records can be `PASS` or `BLOCKED` and require v3 coverage, canonical identity, complete inventory, successful cleanup, and a zero-blocking release gate for PASS. `scripts/stateless_preview_artifact.py` must consume the same v3 post-merge shape. Schema v2 has no compatibility path.

## Protect secrets and cleanup

No `SONAR_*` variable reaches `uv`, Bash, pytest, restore, test, or a test-host descendant. Receipts contain no credentials, environment dump, raw report body, or secret-bearing command line.

The `finally` path removes only the claimed UUID root after foreground producers are terminal. It removes the coverage parent only when empty. It never deletes a generic `.tmp` path. Cleanup failure stays secondary to the first causal failure.

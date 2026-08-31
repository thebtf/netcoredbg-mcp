# Coverage evidence contract

This contract defines the evidence that the future exact-head runner must accept before scanner end and record after server analysis. It does not define a release action. `release_intent` is always `none` for this Wave-3 contract.

## Own one transaction

`scripts/run_sonarqube_exact_head.py` owns all of these steps:

1. Capture one clean detached head and pre-analysis identity.
2. Derive the pure `CoveragePlan` and the two runtime scanner properties.
3. Start SonarScanner with those properties.
4. Claim the UUID root and canonical marker exclusively.
5. Build the retained broad scanner inventory.
6. Invoke the private `build/coverage.sh` producer with an enumerated plan and scrubbed environment.
7. Validate marker, report paths, report bytes, XML structures, denominators, mappings, source sets, host restoration, and post-producer head.
8. End SonarScanner only after step 7 succeeds.
9. Bind report-task, Compute Engine, current-analysis bookends, measures, complete component pages, diagnostic evidence, and cleanup.

`build/coverage.sh` receives no scanner credentials and performs no scanner, API, report discovery, acceptance, or receipt behavior. It writes only planned report paths.

## Admit the marker and paths

The marker validates against [coverage-run-marker.schema.json](coverage-run-marker.schema.json). The runner also requires all of these rules:

- The root did not exist before claim.
- The root is `.tmp/sonarqube-coverage/<run-id>` under the scanner worktree.
- The marker is canonical sorted compact JSON plus one LF and has the plan's recorded SHA-256 and byte count.
- The marker head, project key, report IDs/order/paths, tool versions, producer hash, and `.coveragerc` hash equal the in-memory plan.
- Every report is the exact planned regular file below the claimed root. No report path, parent, or ancestor is a symbolic link or reparse point.
- Scanner arguments use the marker's slash-normalized relative paths. Producers use only their corresponding absolute paths.

## Admit Python evidence

The Python producer uses the external isolated locked `uv` route and `.coveragerc`. The runner accepts its Cobertura XML only when:

- The root local name is `coverage`.
- `lines-valid > 0`, `0 <= lines-covered <= lines-valid`.
- `branches-valid > 0`, `0 <= branches-covered <= branches-valid`.
- Every report mapping resolves uniquely to an existing tracked regular `.py` file under `src/netcoredbg_mcp`.
- The normalized source set is nonempty, sorted, unique, and hashed.

URI, absolute, `..`, outside-root, missing, reparse, duplicate-normalized, and test-only paths fail closed.

## Admit .NET evidence

The producer accepts exactly the five test projects declared in [architecture.md](../architecture.md#fixed-net-producer-inventory). It restores each project, runs without `--no-build`, and writes OpenCover XML. The runner accepts each report only when:

- The root local name is `CoverageSession`.
- Exactly one direct `Summary` supplies `numSequencePoints > 0`.
- `0 <= visitedSequencePoints <= numSequencePoints`.
- Branch counts are nonnegative and ordered, and the five-report aggregate `numBranchPoints > 0`.
- Sequence points join to at least one tracked non-test, non-fixture production `.cs` source below the scanner worktree.
- The source set excludes URI, outside-root, duplicate, `bin`, `obj`, `tests/fixtures`, and `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures` paths.
- Only `stateless` receives the absolute IncludeDirectory. Its report maps production Stateless source and the selected DLL/PDB hashes match before and after coverage collection.

A zero process exit without the expected canonical report is `COVERAGE_REPORT_MISSING`.

## Gate scanner end

The legal event order is:

```text
begin -> claim -> build -> produce -> validate -> post-producer head -> end
```

Any failure through `post-producer head` is a blocked coverage transaction. It has zero scanner-end calls. Cleanup can run after foreground producers terminate, but cleanup cannot make scanner end legal.

## Bind analysis and both languages

After scanner end, the runner accepts analysis coverage only when:

- Submitted analysis, pre-measure current analysis, post-measure current analysis, and final current analysis have the same ID and captured revision.
- Aggregate `coverage`, `lines_to_cover`, and `new_lines_to_cover` are positive finite values.
- The analysis-bound `new_coverage` Quality Gate condition has status `OK`, threshold `80`, and an actual value that equals normalized `new_coverage`.
- Complete file-component pages intersect the validated Python source set and the validated .NET source set separately.
- Each language intersection has a mapped path, positive summed lines to cover, positive covered lines, and a mapped branch measure.

A project-level aggregate number never proves that both language reports were imported.

## Protect secrets and cleanup

The runner derives producer environments from its existing scrubbed environment. No `SONAR_*` variable reaches `uv`, Bash, pytest, restore, test, or a test-host descendant. Receipts record IDs, relative paths, sizes, hashes, counts, safe errors, and page summaries. They do not retain tokens, URLs with credentials, environment dumps, raw report bytes, or raw secret-bearing commands.

The `finally` path removes exactly the claimed UUID root after all foreground producers are terminal. It removes the coverage parent only when it is empty. It never deletes a generic `.tmp` path. If cleanup fails after another failure, the original failure remains primary and cleanup failure is secondary.

## Receipt rule

The future diagnostic record validates against [diagnostic-receipt-v3.schema.json](diagnostic-receipt-v3.schema.json). Its only outcomes are `DIAGNOSTIC_COMPLETE` and `BLOCKED`, and it always has `release_intent: none`. A diagnostic record cannot satisfy release pass validation. Candidate and post-merge validation require schema-v3 coverage evidence and reject schema-v2 or incomplete records.

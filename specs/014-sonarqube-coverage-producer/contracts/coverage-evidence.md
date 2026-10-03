# Coverage evidence contract

This contract defines the evidence the future exact-head runner must accept before scanner end and record after analysis. It does not define a release action. Diagnostic `release_intent` is always `none`.

## Admit execution before scanner begin

Before the runner starts a scanner transaction, it must:

1. Resolve `Wave2ClosureEntryV1` only from the tracked `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json` source and validate it against [wave2-closure-entry-v1.schema.json](wave2-closure-entry-v1.schema.json).
2. Verify source `integration.kind: pull_request_head`, `release_intent: none`, the accepted candidate, and `integration.head_sha == accepted_candidate_sha`. The source head is the reviewed implementation head, not the actual PR head.
3. Hash the tracked source and closure receipt from canonical Git blob bytes. Do not hash checkout files.
4. Obtain fail-closed first-party PR evidence that binds PR #289's actual PR head to `merge_commit_sha`.
5. Require the accepted candidate to be ancestor-or-equal to the actual PR head, require equal PR-head and merge trees, record their shared `integrated_tree_sha`, and require the tracked artifact blob to equal the PR-head artifact blob.
6. Derive `artifact_commit_sha` from current path history. Require it to equal `merge_commit_sha` or be ancestor-or-equal to `observed_main_sha`, and require `merge_commit_sha` to be ancestor-or-equal to `observed_main_sha`.
7. Resolve and version-check `uv`, `bash`, and `dotnet`.
8. Evaluate each fixed project for `net8.0`, `Microsoft.NET.Test.Sdk` `17.12.0`, and VSTest selection. Require direct private `coverlet.msbuild` `10.0.1` in the four Coverlet projects and direct private `Microsoft.CodeCoverage` `17.14.1` in Stateless alone.
9. Refuse MTP, including `TestingPlatformDotnetTestSupport` activation and Microsoft Testing Platform references.

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

The marker validates against [coverage-run-marker.schema.json](coverage-run-marker.schema.json). It binds the tracked Wave-2 source and resolved-copy hash, plus the accepted candidate, actual PR head, artifact commit, merge commit, one integrated tree SHA, observed main, two final reports, and five ordered private producer inputs. `source_sha256` is the SHA-256 of the tracked source's canonical Git blob bytes. Scanner arguments use only these final paths:

```text
/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/<run-id>/python/coverage.xml
/d:sonar.cs.cobertura.reportsPaths=.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml
```

The final paths are slash-relative. Producers use absolute paths below the claimed root. The runner rejects an alternate path, static XML property, report glob, symlink, reparse point, URI, traversal, duplicate normalized path, or report outside the root.

## Admit Python evidence

The runner accepts `python/coverage.xml` only when it has a `coverage` root, positive line and branch denominators, ordered counts, and a nonempty sorted unique source set. Every mapping must resolve exactly once to a tracked regular `.py` file under `src/netcoredbg_mcp` or exactly one of `scripts/run_sonarqube_exact_head.py` and `scripts/stateless_preview_artifact.py`. URI, absolute, escape, missing, reparse, duplicate-normalized, untracked, other-script, and test-only paths fail closed.

## Admit .NET inputs and normalize one final report

The producer runs exactly the five projects named in [architecture.md](../architecture.md#fixed-net-producer-inventory). Each project restores and tests without `--no-build`, filters, exclusions, thresholds, or merge switches. The four unchanged inputs use Coverlet; Stateless alone builds its test project then executes its complete VSTest DLL using the pinned `Microsoft.CodeCoverage` 17.14.1 adapter with managed dynamic instrumentation and child-process collection. It emits one private Cobertura input at its planned input prefix.

The Stateless producer admits one attached Cobertura report only after checking the installed pinned adapter, VSTest diagnostic loaded-assembly path, collector URI, the exact regular attachment referenced by the TRX deployment root and href, and a nonempty all-passing test run. A second same-basename collector source file is not a second attachment. Producer-terminal evidence requires the Windows Job owner's `DRAINED` receipt: zero active processes, a signaled root and retained member handles or proven member retirement, and Job lifetime reconciliation. Process exit, zero accounting alone, or advisory Job notifications cannot establish producer-terminal status or allow run-root cleanup; on timeout or interruption the producer force-drains the same owned tree first and retains failed ownership for recovery. It hashes both the test and production DLL/PDB pairs immediately before and after VSTest and refuses changed bytes. Its projection maps tracked regular production `.cs` from Stateless, `bridge/`, and `host/NetCoreDbg.Mcp.DesignProbe.Wpf/` under the checkout; these are real child-process hits from the full attached run, not extra private inputs. Known Stateless test classes and tracked sources in the three existing test-fixture project directories contribute no facts or denominator: `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/`, `host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/NativeSceneProbe.WpfFixture/`, and `tests/fixtures/WpfSmokeApp/`. The third directory requires exact package `WpfSmokeApp`, a dot-bounded `WpfSmokeApp.` class namespace with a nonempty suffix, and tracked regular authored `.cs` validated through the unchanged safe-source containment/no-reparse checks. Nested/compiler class names attached to those authored files are valid fixture identities; sibling paths, foreign modules/classes, and unexpected `bin`/`obj` sources fail closed. The filtered grid capture observed no `obj` records, which is not absence proof and grants no generated-source admission. This remains one Stateless input in the unchanged five-provider inventory, not a sixth provider. The exact untracked virtual `bridge/obj/Debug/net8.0-windows/win-x64/Microsoft.Interop.LibraryImportGenerator/Microsoft.Interop.LibraryImportGenerator/LibraryImports.g.cs` path is omitted only for its four observed classes: `FlaUIBridge.Commands.ClickCommands`, `FlaUIBridge.Commands.ElementCommands`, `FlaUIBridge.Commands.HoverCommands`, and `FlaUIBridge.Commands.NativeScreenshotCaptureTransport`. The screenshot class requires actual package `FlaUIBridge` and a same-package, same-class authored entry at tracked `bridge/Commands/ScreenshotCaptureTransport.cs`. The exact untracked virtual `host/NetCoreDbg.Mcp.Stateless/obj/Debug/net8.0/Microsoft.Interop.LibraryImportGenerator/Microsoft.Interop.LibraryImportGenerator/LibraryImports.g.cs` path is omitted only for `NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession.WindowsProcessTreeOwnership`, with package `NetCoreDbg.Mcp.Stateless` and a same-package, same-class authored entry at tracked `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs`. Neither owner nor the third fixture admits the guessed `ModuleNamespace` alias. Generated hits and branches are not credited to authored sources; their existing hit/branch facts and denominator remain intact. Other generated, foreign, tracked-generated, untracked-authored, nonregular, escaping, or duplicate-normalized source paths fail closed. Unprojected collector XML is not an input, and filtered mapping observations cannot satisfy this complete unfiltered producer contract.

The collector's actual `asyncio.run` consumer stays alive until the same owner's
read-only `closed` fact confirms physical closure, including repeated caller
cancellation. A retained object followed by runner exit is not operational
cleanup. Private native-operation failures are outcome data; the exact first
fatal object is raised at the collector boundary only after physical exit,
creator-worker join and acknowledged resource release. A physically closed fatal
collection remains `FAILED`: producer-terminal stays false, scanner end has zero
calls, and the claimed root is retained. Ambiguous native effects keep the loop,
cleanup owner and creator worker alive instead of authorizing retry or shutdown.

First-party Cobertura repeats method line facts under each class and emits a class-level `<lines>` summary. The projection verifies each method line's hit state and covered/valid branch totals against the matching class-summary line before discarding redundant method XML. Class-summary branch totals preserve distinct method outcomes sharing a source line (for example, two lambdas on `bridge/Commands/ElementCommands.cs:1602` contribute 6 and 2 branches to a summary of 8). A missing or disagreeing summary fails closed. Neither the duplicated method rows nor cross-provider ordinals may be unioned into the denominator.

The runner validates each private input before normalization. It requires a `coverage` root, a positive line denominator, ordered counts, at least one tracked production `.cs` mapping, and no unsafe path. The aggregate private input branch denominator must be positive. The Stateless input must map production Stateless source and preserve the selected production DLL/PDB bytes.

The runner normalizes the five inputs in marker order. It canonicalizes source paths and unions identifiable coverage facts only when condition ordinals have the same provider and class meaning. Two distinct first-party classes can each own branches at the same source line (`bridge/Commands/ClickCommands.cs:521` has 2 + 2). Their class-qualified groups remain disjoint; the final line carries the sum of observed covered/valid counts as a grouped Cobertura aggregate, without inventing per-outcome IDs. A single class keeps its exact identifiable conditions. Coverlet inputs retain their existing ordinal union. Any overlapping source line across Coverlet and Microsoft.CodeCoverage fails closed. The final output must have positive line and branch denominators and a source set equal to the validated input union. The five inputs are not scanner report identities.

## Gate scanner end

```text
wave2-entry -> preflight -> begin -> claim -> build -> produce -> normalize -> validate -> post-producer-head -> end
```

Any failure through `post-producer-head` is a blocked transaction with zero scanner-end calls. Foreground process exit does not make the collector producer terminal: its owned tree must first meet the drain condition above. Cleanup cannot make scanner end legal.

## Bind analysis and diagnostic inventory

After scanner end, the runner requires one canonical exact-head analysis identity. Submitted analysis and every current-analysis observation must equal it. It then requires positive aggregate coverage values, the unchanged `new_coverage` condition `OK` at threshold `80`, and complete component paging with positive mapped contributions for both final language source sets.

Before `DIAGNOSTIC_COMPLETE`, the runner writes a create-new artifact that validates against [diagnostic-inventory-v1.schema.json](diagnostic-inventory-v1.schema.json). It retains all paginated issue and hotspot records, routing fields, counts, key digests, and identity. The receipt binds the artifact's relative path, bytes, and SHA-256. The runner refuses incomplete or count-only inventory evidence.

## Receipt rule

All roles validate against [exact-head-receipt-v3.schema.json](exact-head-receipt-v3.schema.json). Diagnostic records can be `DIAGNOSTIC_COMPLETE` or `BLOCKED` and always have `release_intent: none`. Candidate and post-merge records can be `PASS` or `BLOCKED` and require v3 coverage, canonical identity, complete inventory, successful cleanup, and a zero-blocking release gate for PASS. `scripts/stateless_preview_artifact.py` must consume the same v3 post-merge shape. Schema v2 has no compatibility path.

## Protect secrets and cleanup

No `SONAR_*` variable reaches `uv`, Bash, pytest, restore, test, or a test-host descendant. Receipts contain no credentials, environment dump, raw report body, or secret-bearing command line.

The `finally` path removes only the claimed UUID root after foreground producers are terminal, including a proven drain of the collector's owned tree. If that drain fails, retain the run root and report cleanup failure; an unrelated owner cannot declare the producer terminal. Remove the coverage parent only when empty; never delete a generic `.tmp` path. Cleanup failure stays secondary to the first causal failure.

Windows claim cleanup holds verified ancestor/claim handles before validation
and verified child handles during every descent, including final directory
deletion. Handle-bound `FileDispositionInfoEx | IGNORE_READONLY_ATTRIBUTE`
deletion never clears shared file attributes. Unsupported native capability
blocks cleanup without pathname or attribute-changing fallback. A same-user
writer may still create an alias after a link-count observation; protection is
unchanged external bytes/attributes, not atomic alias prevention. Private scanner
POSIX claim cleanup fails closed before deletion until final-object identity
and namespace ownership can be retained through the actual removal operation;
product runtime POSIX support is unaffected.

Cleanup interruption persists failed cleanup and already reached coverage,
inventory, and analysis facts, then re-raises the exact original interruption.
No interrupted deletion is retried by transaction finalization. A cleanup block
uses the v3 `INCOMPLETE` analysis shape with null unobserved after/final bookends;
it cannot satisfy the unchanged all-true completion/PASS contract.

Cleanup failures retain `COVERAGE_CLEANUP_FAILED` and the original exception
class. When a native or filesystem operation supplies an `OSError`, the optional
`cleanup.failure.native` discriminator records only its allowlisted operation,
cleanup stage, claim-relative entry, numeric `winerror`/`errno` (null when absent).
The entry is `.` for the claim root, `@parent` for its empty-parent cleanup, or
`@ancestor/N` for a pinned ancestor N lexical components above the claim. No
absolute ancestor names, exception text/filenames, report bodies, credentials,
or provider content are copied into the discriminator. The existing secret-free
receipt writer still refuses credential-bearing values.

Attribution preserves the first error even when disposition cancellation or
handle closing also fails; it never retries deletion or relaxes `BLOCKED`.
Receipts predating this discriminator identify only the exception class: their
historical native operation/error/entry remain unknown. This is diagnostic
preservation, not a fix or retrospective cause claim for a retained cleanup run.

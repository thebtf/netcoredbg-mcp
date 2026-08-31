# Research: exact-head SonarQube coverage producer

**Status**: Planning research. It records observed inputs and selected design conclusions. It reports no producer run, diagnostic result, green Quality Gate, or release.
**Packet source base**: `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`

## Evidence labels

- **OBSERVED** means a cited repository or packet artifact reports a fact.
- **SELECTED** means this packet fixes an implementation shape.
- **INFERRED** means a future implementation must prove the stated property on its exact head.
- **BLOCKED** means current execution may not cross the named entry boundary.

## Parent and source facts

| Classification | Observation | Evidence | Consequence |
| --- | --- | --- | --- |
| OBSERVED | The parent `CoverageTransaction` requires exactly two deterministic project-root-relative language reports. | Parent `data-model.md` and `architecture.md` | Wave 3 produces one Python Cobertura and one .NET Cobertura scanner identity. |
| OBSERVED | The parent requires a complete diagnostic inventory before Wave 4 creates its one-owner manifest. | Parent `architecture.md` and tasks | Diagnostic completion must retain full inventory authority, not counts alone. |
| OBSERVED | The retained runner is the only scanner caller and currently has no coverage transaction. | `agent://Wave3CoverageSource` | Extend the retained runner. Do not add a scanner. |
| OBSERVED | The five selected test projects target `net8.0` and directly reference `Microsoft.NET.Test.Sdk` `17.12.0`. | Current five project files | Preflight fixes the VSTest tuple rather than assuming it. |
| OBSERVED | The broader build inventory is not the closed coverage producer inventory. | `agent://Wave3CoverageSource`; `agent://Wave3CoverageTests` | The five projects remain a fixed ordered input set. |
| OBSERVED | Stateless coverage needs the exact `IncludeDirectory` and binary restoration check. | `agent://Wave3CoverageSource` | Preserve the Stateless-only branch. |
| OBSERVED | Wave-2 PR #289 is open. | Operator instruction | Wave-3 execution is BLOCKED pending a tracked closure artifact; the observed main identity can exist only at later Wave-3 runtime. |
| OBSERVED | `scripts/stateless_preview_artifact.py` requires receipt schema version 2 for post-merge artifact sealing. | Verifier recheck of `08a4231` | Wave 3 must migrate this receipt consumer and its focused proof to unified v3. |
| OBSERVED | The current checkout contains no `specs/013-owner-scoped-prebuild-cleanup/` directory or tracked Wave-2 closure artifact. | Verifier recheck of `587d735` | This absence is current blocker evidence. Wave-2 T014/PR #289 must create and merge the tracked artifact before Wave 3 can execute. |

## Primary-source constraints

| Source family | Observed constraint | Selected use |
| --- | --- | --- |
| SonarQube .NET coverage documentation | C# accepts a Cobertura report path during analysis. | Pass the one final .NET Cobertura path through `sonar.cs.cobertura.reportsPaths`. |
| SonarQube Python coverage documentation | Python accepts a Cobertura path during analysis. | Pass the one final Python Cobertura path at scanner begin. |
| Coverage.py XML documentation | Cobertura output can carry line and branch denominators with relative files. | Keep `.coveragerc` as the Python policy owner. |
| Coverlet MSBuild integration | Direct package references support VSTest MSBuild collection and Cobertura output. | Use direct private `coverlet.msbuild` `10.0.1` for the five inputs. |
| Coverlet compatibility documentation | `coverlet.msbuild` is incompatible with MTP v2 because MTP bypasses VSTest MSBuild targets. | Preflight refuses MTP before scanner begin. It does not inject a fallback property. |

## Selected decisions

### Preserve the parent two-report contract

**SELECTED**: The runner sends exactly two final Cobertura reports to Sonar. The five fixed .NET projects produce private Cobertura inputs, then the runner writes one deterministic `.NET` output.

**Reason**: The parent binds one .NET report and one Python report in the same transaction. Five scanner report identities would contradict that contract.

### Normalize the private .NET inputs inside the runner

**SELECTED**: `normalize_dotnet_cobertura` consumes the fixed marker order. It validates every input, canonicalizes safe production paths, unions line and branch facts, emits lexical output order, reparses its output, and rejects a missing, added, dropped, unsafe, or zero-denominator source set.

**Reason**: A bounded normalizer preserves producer coverage while leaving Sonar one .NET identity. It is not a generic report-discovery facility.

### Gate before scanner begin

**SELECTED**: Wave-2 entry validation and producer preflight precede scanner begin and run-root claim. The preflight requires `uv`, `bash`, `dotnet`, Coverlet `10.0.1`, Test SDK `17.12.0`, and VSTest. It rejects MTP.

**Reason**: A known unavailable or incompatible producer discovered after begin would violate the transaction boundary and make cleanup carry unnecessary scanner state.

### Make the diagnostic inventory an immutable Wave-4 input

**SELECTED**: `DIAGNOSTIC_COMPLETE` requires a create-new `DiagnosticInventoryV1` artifact with complete paginated issue and hotspot records, identity, counts, key digests, and routing fields. The receipt hash-binds that artifact.

**Reason**: Wave 4 cannot assign each current blocking key once from aggregate counts.

### Use one discriminated v3 receipt contract

**SELECTED**: `ExactHeadReceiptV3` supports diagnostic, candidate, and post-merge roles. It permits diagnostic completion only for `diagnostic`, PASS only for candidate/post-merge, and no schema-v2 compatibility.

**Reason**: The prior diagnostic-only schema could not define a release PASS shape. One role-discriminated schema gives every caller the same coverage linkage and identity contract.

### Preserve callers through a tracked Wave-2 closure artifact and migrate the actual v3 consumer

**SELECTED**: `GitContext` resolves only the tracked `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json` artifact. Wave-2 T014 produces it on PR #289 with `integration.kind: pull_request_head`, `release_intent: none`, accepted implementation candidate, PR head-ref/SHA, and closure receipt hash, but no current merge or future main SHA. At Wave-3 runtime, the verifier proves PR merge, derives observed main and artifact commit identities, and checks candidate/artifact ancestry. The hosted post-merge workflow performs the same clean-checkout proof before scanning. The same cutover migrates `scripts/stateless_preview_artifact.py` from schema v2 to the unified v3 post-merge receipt.

**Reason**: A PR-head artifact must not assert that its PR is already merged. Runtime observation supplies post-merge authority while the source artifact remains clone-reproducible and role CLI shapes stay stable.

## Rejected designs

| Design | Disposition | Reason |
| --- | --- | --- |
| A branch head, a source record claiming merge, or a precomputed main SHA as Wave-2 authority | Rejected | The source records premerge candidate/PR-head facts only; Wave 3 proves merge and derives observed main after merge. |
| Static XML report paths or report globs | Rejected | They can admit stale or extra artifacts. |
| Generic merger or report discovery | Rejected | The normalizer must consume the fixed input list and emit one fixed output. |
| Automatic MTP fallback | Rejected | It masks the Coverlet compatibility boundary rather than proving VSTest compatibility. |
| Count-only diagnostic evidence | Rejected | It cannot seed a one-owner manifest. |
| Schema-v2 release compatibility | Rejected | It leaves candidate/post-merge PASS under-specified. |

## Unknowns and future proof

| Unknown | Required proof |
| --- | --- |
| Live Sonar component and inventory paging | The exact diagnostic run proves complete pages and both language intersections. |
| Project evaluation on the implementation head | Preflight proves the exact five tuples and refuses MTP. |
| .NET normalization behavior | Focused fixtures prove union, deterministic output, source safety, and positive denominators. |
| Wave-2 merge identity and tracked artifact | Wave-2 T014/PR #289 must create and merge `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json`; T000 derives observed main and validates it only after merge. |

## Research conclusion

The selected shape retains the existing scanner authority and role CLI shapes, requires a tracked Wave-2 merge artifact before execution, meets the parent two-report Cobertura contract, keeps the five projects as deterministic producer inputs, fails known toolchain defects before begin, gives Wave 4 a full inventory authority, migrates the existing post-merge receipt consumer, and defines every v3 role without a v2 compatibility path.

# Specification quality checklist: exact-head SonarQube coverage producer

**Purpose**: Review this Wave-3 packet before implementation. Marking a checkbox records document review only. It creates no diagnostic receipt, inventory artifact, acceptance receipt, release, tag, or publication.

## Scope and entry authority

- [ ] The packet names the parent Wave-3 two-report Cobertura contract and does not amend the parent.
- [ ] The packet uses `specs/014-sonarqube-coverage-producer/` and documents the older parent pointer without editing it.
- [ ] The packet names `scripts/run_sonarqube_exact_head.py` as the only scanner, normalization, analysis, and receipt authority.
- [ ] The packet says Wave-2 PR #289 is open and that Wave-3 implementation and diagnostic execution are BLOCKED until the tracked PR-head artifact and runtime observed-main proof validate.
- [ ] `wave2-closure-entry-v1.schema.json` requires the tracked `specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json`, source `pull_request_head`, `release_intent: none`, accepted candidate, PR head-ref/SHA, closure receipt hash, and PR #289. It contains no current merge or future main claim. The runner proves merge and derives observed main/artifact commit at runtime; no branch-derived runtime or ambient `.agent` entry exists.
- [ ] The hosted post-merge workflow validates the tracked entry source from a clean checkout before scanning and artifact sealing. It provisions no `.agent` state.

## Plan, preflight, and report layout

- [ ] `CoveragePlan` is pure before scanner begin and root claim.
- [ ] The preflight checks `uv`, `bash`, `dotnet`, Coverlet `10.0.1`, Test SDK `17.12.0`, VSTest, and MTP refusal before begin and claim.
- [ ] The matrix proves every entry/preflight failure has zero begin and claim calls.
- [ ] The marker names exactly two final Cobertura reports and five ordered private .NET producer inputs.
- [ ] The layout names exactly one Python final report and one .NET final report under `.tmp/sonarqube-coverage/<run-id>/`.
- [ ] The five .NET paths under `dotnet/inputs/` are explicitly private inputs, not scanner report identities.
- [ ] Scanner begin gets exactly one Python and one .NET Cobertura property with slash-relative paths.
- [ ] The packet prohibits static XML coverage properties, report globs, alternate report formats, generic discovery, filters, exclusions, threshold switches, and `--no-build`.

## Producer, normalizer, and local evidence

- [ ] The Python route uses isolated locked `uv` and does not make Coverage.py a permanent dependency.
- [ ] Each .NET producer is one of the fixed five VSTest projects and has direct private Coverlet `10.0.1`.
- [ ] The shell restores/tests each project and writes planned private Cobertura inputs without merge switches.
- [ ] The normalizer consumes the fixed input order, canonicalizes source paths, unions coverage facts, and emits one lexical final .NET report.
- [ ] Input and final validation require safe tracked production mappings and positive required denominators.
- [ ] The Stateless input requires its production mapping and unchanged DLL/PDB bytes.
- [ ] Invalid marker, root, input, normalizer output, source, denominator, hash, or head evidence blocks scanner end.

## Analysis, inventory, and v3 receipt authority

- [ ] Submitted and all current-analysis observations bind to one canonical identity.
- [ ] Fully paginated components prove positive mapped coverage for both final language source sets.
- [ ] `diagnostic-inventory-v1.schema.json` stores complete issue and hotspot records with pagination, key digests, and routing fields.
- [ ] `DIAGNOSTIC_COMPLETE` rejects `complete:false`, count-only inventory, absent records, artifact hash mismatch, and identity mismatch.
- [ ] `exact-head-receipt-v3.schema.json` has discriminated diagnostic, candidate, and post-merge role/outcome rules.
- [ ] Candidate and post-merge PASS reject schema v2, diagnostic-as-PASS, missing coverage linkage, incomplete inventory, stale identity, failed cleanup, and nonzero release gate.
- [ ] Diagnostic records remain `release_intent: none` and cannot authorize release.

## Behavior-first and delayed-receipt completeness

- [ ] [tasks.md](../tasks.md#binding-redgreen-matrix) contains exactly 15 rows, R01 through R15.
- [ ] Every row names a current RED oracle, a nonzero GREEN oracle, a V01 to V15 label, an owner task, and a dependency.
- [ ] COV-001 through COV-026 appear in both `spec.md` traceability and `tasks.md` requirement coverage.
- [ ] S1 explicitly depends on T000 Wave-2 tracked-entry validation.
- [ ] T021 owns v3 candidate/post-merge PASS enforcement and `scripts/stateless_preview_artifact.py` consumer migration. T023 owns the hosted clean-clone entry verification.
- [ ] T028 is the first task allowed to invoke diagnostic or create a diagnostic record, inventory artifact, or acceptance receipt.

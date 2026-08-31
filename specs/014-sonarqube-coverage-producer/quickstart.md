# Quickstart: verify a future exact-head coverage diagnostic

**Status**: Future implementation guide. No command in this file was run while authoring this packet. This guide is not a receipt, release authority, or permission to change Sonar policy.
**Release intent**: `none`

## Before any Wave-3 execution

Wave-2 PR #289 is open while this packet is authored. Do not implement or run a Wave-3 diagnostic until a verified `Wave2ClosureEntryV1` exists. The entry artifact must validate against `contracts/wave2-closure-entry-v1.schema.json`, name an accepted `origin/main` SHA, hash-bind the Wave-2 closure receipt, and identify merged PR #289. A PR head or feature branch is not entry evidence.

Set non-secret values only after the entry record exists:

```powershell
$Repo = '<fresh-detached-scanner-worktree>'
$CoordinationRoot = '<primary-coordination-root>'
$ExpectedHead = '<frozen-implementation-sha>'
$Wave2Entry = '<verified-wave2-entry-evidence.json>'
$ProjectKey = 'thebtf_netcoredbg_mcp'
```

## 1. Confirm the exact source and entry identities

Run from `$Repo`:

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse --show-toplevel
```

Continue only when the scanner worktree is detached and clean, `HEAD` equals `$ExpectedHead`, and the runner accepts `$Wave2Entry`. A failed entry is `WAVE2_CLOSURE_UNVERIFIED`; it must make preflight, scanner begin, and root claim unreachable.

## 2. Run focused contract proof

After T021 and T022 make all rows green, run:

```powershell
uv run --locked --extra dev pytest tests/test_sonarqube_exact_head_runner.py -q
```

The output must cover exactly R01 through R15 from [tasks.md](tasks.md#binding-redgreen-matrix). It must prove the Wave-2 entry and preflight failures occur before scanner begin and claim.

## 3. Inspect the planned layout and scanner arguments

During a successful transaction, the producer and runner use this layout:

```text
.tmp/sonarqube-coverage/<run-id>/
├── coverage-run.json
├── python/.coverage
├── python/coverage.xml
├── dotnet/coverage.xml
└── dotnet/inputs/
    ├── codesearch-core/coverage.cobertura.xml
    ├── host/coverage.cobertura.xml
    ├── stateless-preview/coverage.cobertura.xml
    ├── stateless/coverage.cobertura.xml
    └── host-prompts/coverage.cobertura.xml
```

The five paths under `dotnet/inputs/` are private producer inputs. Sonar receives only:

```text
/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/<run-id>/python/coverage.xml
/d:sonar.cs.cobertura.reportsPaths=.tmp/sonarqube-coverage/<run-id>/dotnet/coverage.xml
```

## 4. Understand the private producer and preflight

The runner preflights `uv`, `bash`, and `dotnet`; exact Coverlet `10.0.1`; Test SDK `17.12.0`; and VSTest selection for every fixed project. It refuses MTP rather than modifying project configuration. The shell then receives five `--dotnet-project` groups in the inventory order and produces a private Cobertura input for each group. The runner validates them, normalizes their canonical source union, and writes `dotnet/coverage.xml`.

A missing executable, invalid package tuple, or MTP activation has zero begin and claim calls. A missing private input, unsafe source mapping, invalid final output, or zero final denominator has zero scanner-end calls.

## 5. Run the diagnostic transaction

Run only after the exact head has review, judgment, and verified Wave-2 entry evidence. The runner loads credentials at its existing approved boundary. Do not echo `SONAR_*`.

```powershell
python scripts/run_sonarqube_exact_head.py --role diagnostic --wave2-entry-evidence $Wave2Entry
```

A valid order is:

```text
wave2-entry -> preflight -> begin -> claim -> build -> produce -> normalize -> validate -> post-producer-head -> end -> analysis -> inventory -> receipt -> cleanup
```

## 6. Verify diagnostic evidence

A `DIAGNOSTIC_COMPLETE` run writes a secret-free v3 receipt and a create-new inventory artifact below the coordination root:

```text
$CoordinationRoot/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<head>/diagnostic/<run-id>.json
$CoordinationRoot/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<head>/diagnostic/<run-id>.inventory.json
```

Verify that:

- the receipt role is `diagnostic`, its outcome is `DIAGNOSTIC_COMPLETE`, and its `release_intent` is `none`;
- the receipt has two final Cobertura reports and five private input records, with normalizer output ID `dotnet`;
- the canonical identity, all analysis observations, and the inventory artifact identity bind to `$ExpectedHead`;
- final Python and .NET reports have positive line and branch denominators and valid source sets;
- component paging and issue/hotspot paging are complete;
- the inventory artifact hash, byte count, record counts, key digests, and routing fields validate;
- the unchanged `new_coverage` condition is `OK` at threshold `80`;
- unresolved global blockers remain explicit and do not turn the diagnostic into a release PASS.

## 7. Create the delayed Wave-3 acceptance receipt

T028 is the only task allowed to create `acceptance-receipt.md`. It may do so only when the Wave-2 entry, T024 review, T026 acceptance judgment, T027 frozen scanner head, complete diagnostic receipt, and complete immutable inventory all bind to the same implementation head. If any condition fails, create no receipt, tag, publication, or release claim.

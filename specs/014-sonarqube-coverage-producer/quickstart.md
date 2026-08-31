# Quickstart: verify a future exact-head coverage diagnostic

**Status**: Future implementation guide. No command in this file was run while authoring this packet. This guide is not a receipt, release authority, or permission to change Sonar policy.
**Release intent**: `none`

## Purpose

Use this guide after the implementation tasks complete to prove that the retained exact-head runner produces and imports same-transaction Python and .NET coverage. Run the runner, not `build/coverage.sh`, as the diagnostic entry point. The shell is a private producer invoked by the runner.

## Safety boundaries

1. Use a fresh clean detached scanner worktree at the exact implementation SHA.
2. Keep the approved `.env` only in the primary coordination root. Do not copy, print, commit, or pass it to the producer.
3. Do not change `SonarQube.Analysis.xml`, `docs/RELEASE-PROTOCOL.md`, thresholds, New Code, exclusions, project key, credential policy, or finding dispositions.
4. Do not invoke a report glob, merge, filter, source exclusion, `--no-build`, image-name cleanup, or a fallback scanner command.
5. Do not create `acceptance-receipt.md` before the final diagnostic task meets its complete predicate.

## Inputs

Set only non-secret values.

```powershell
$Repo = 'D:\Dev\netcoredbg-mcp\.agent\worktrees\issue450-sonar-coverage-producer'
$CoordinationRoot = 'D:\Dev\netcoredbg-mcp'
$ExpectedHead = '<frozen-implementation-sha>'
$ProjectKey = 'thebtf_netcoredbg_mcp'
```

The implementation SHA replaces `<frozen-implementation-sha>` only after T024 through T027 bind review and judgment to it.

## 1. Confirm the candidate identity

Run from `$Repo`.

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse --show-toplevel
```

Continue only when the worktree is detached, clean, rooted at `$Repo`, and `HEAD` equals `$ExpectedHead`. A different source head requires a new review/judgment binding.

## 2. Run focused contract proof

Run the focused test module after T021 and T022 make every matrix row green.

```powershell
uv run --locked --extra dev pytest tests/test_sonarqube_exact_head_runner.py -q
```

The output must cover R01 through R15 from [tasks.md](tasks.md#binding-redgreen-matrix). It must exercise the exact runner caller behavior, not only helper functions.

## 3. Audit the planned layout and scanner arguments

The runner derives paths from a UUID. The producer must create this layout during the diagnostic transaction:

```text
.tmp/sonarqube-coverage/<run-id>/
├── coverage-run.json
├── python/.coverage
├── python/coverage.xml
├── dotnet/codesearch-core/coverage.opencover.xml
├── dotnet/host/coverage.opencover.xml
├── dotnet/stateless-preview/coverage.opencover.xml
├── dotnet/stateless/coverage.opencover.xml
└── dotnet/host-prompts/coverage.opencover.xml
```

Before scanner begin, the runner derives but does not create the root. Scanner begin receives only these run-specific properties:

```text
/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/<run-id>/python/coverage.xml
/d:sonar.cs.opencover.reportsPaths=.tmp/sonarqube-coverage/<run-id>/dotnet/codesearch-core/coverage.opencover.xml,.tmp/sonarqube-coverage/<run-id>/dotnet/host/coverage.opencover.xml,.tmp/sonarqube-coverage/<run-id>/dotnet/stateless-preview/coverage.opencover.xml,.tmp/sonarqube-coverage/<run-id>/dotnet/stateless/coverage.opencover.xml,.tmp/sonarqube-coverage/<run-id>/dotnet/host-prompts/coverage.opencover.xml
```

The scanner properties are slash-relative to the scanner worktree. The producer outputs are absolute filesystem paths below the claimed root.

## 4. Understand the private producer command

The runner, not an operator, invokes this command shape after scanner begin and root claim:

```text
uv run --project <repo> --isolated --locked --extra dev --with coverage==7.15.4 -- \
  bash <repo>/build/coverage.sh \
  --repo-root <repo> \
  --python-data <absolute-run-root>/python/.coverage \
  --python-report <absolute-run-root>/python/coverage.xml \
  --dotnet-project <id> <absolute-csproj> <absolute-report> <absolute-include-or-dash>
```

There are exactly five `--dotnet-project` groups in the order defined in [architecture.md](architecture.md#fixed-net-producer-inventory). The shell restores and runs each exact test project. It never uses `--no-build`. Only the `stateless` group receives `IncludeDirectory` for `host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0`.

## 5. Run the diagnostic transaction

Run only after the exact head has review and judgment evidence. The runner loads credentials using its existing approved boundary. Do not set or echo `SONAR_*` in this procedure.

```powershell
python scripts/run_sonarqube_exact_head.py --role diagnostic
```

A valid diagnostic transaction follows this order:

```text
capture head and pre-analysis
-> derive plan
-> scanner begin with exact report properties
-> exclusive root and marker claim
-> existing builds
-> isolated Python and fixed-five .NET producers
-> local report/marker/source/hash/head validation
-> scanner end
-> CE/report-task/current-analysis bookends/measures/components
-> diagnostic metadata
-> claimed-root cleanup
```

Before scanner end, the runner requires all six reports, marker bytes, report hashes, positive denominators, valid intended source mappings, equal Stateless DLL/PDB pre/post hashes, and a matching post-producer head. Any failure leaves scanner end unreachable.

## 6. Verify diagnostic evidence

When the diagnostic result is `DIAGNOSTIC_COMPLETE`, the runner writes a secret-free record at:

```text
$CoordinationRoot/.agent/e/sonarqube/thebtf_netcoredbg_mcp/<head>/diagnostic/<run-id>.json
```

Check these facts against the exact record:

- `schema_version` is `3`, `role` is `diagnostic`, and `release_intent` is `none`.
- The captured head, submitted analysis revision, and both current-analysis bookends equal `$ExpectedHead`.
- The marker and six report records use only relative paths, byte sizes, hashes, formats, denominators, and source-set digests. They contain no report body or credential.
- The Python report has positive line and branch denominators and only `src/netcoredbg_mcp` mappings.
- Each OpenCover report has a positive sequence denominator. Their aggregate branch denominator is positive. Each maps an intended production `.cs` source.
- The server component evidence is fully paginated and has positive mapped lines and covered lines for both languages.
- The `new_coverage` condition is `OK` at threshold `80`.
- Unrelated global issue/finding blockers remain listed. Their presence does not turn the diagnostic into a release pass.
- Cleanup names only the claimed root and preserves any primary failure.

## 7. Create the delayed Wave-3 acceptance receipt

T028 is the only task allowed to create `acceptance-receipt.md`. It may do so only if all of these are true:

1. T024 review and T026 acceptance judgment bind to `$ExpectedHead`.
2. T027 confirms the diagnostic scanner worktree has the same head and no later source mutation.
3. The diagnostic record is `DIAGNOSTIC_COMPLETE` and has the required both-language source, measure, and `new_coverage` evidence.
4. The receipt records `release_intent: none`, the diagnostic path/hash, exact review/judgment identities, and remaining global blockers.

If any predicate fails, create no receipt. Return to the task that owns the failure. Do not create a tag, publish a package, or treat a diagnostic record as release evidence.

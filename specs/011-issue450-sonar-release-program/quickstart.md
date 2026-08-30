# Quickstart: Manual Audit of the v0.23.11 Program

**Status**: Future audit guide only. None of these commands were run while authoring this parent packet, and this file is not an acceptance receipt or release authorization.

## Purpose

Use this guide to inspect the program’s future evidence without exposing secrets, mutating Sonar policy, modifying a worktree, creating a tag, publishing a package, or treating a dashboard as proof. Child packets provide their focused source/test commands; this guide checks parent-level exact-head, manifest, and Wave-5 barrier invariants.

## Safety Boundaries

1. Run a scanner command only from the clean detached scanner worktree required by `docs/RELEASE-PROTOCOL.md` and `docs/SONARQUBE-ONBOARDING.md`.
2. Keep `.env` solely in the primary coordination root. Do not copy it into a linked worktree, packet directory, receipt, shell history, or transcript.
3. Do not print `SONAR_HOST_URL`, `SONAR_TOKEN`, `SONAR_READ_TOKEN`, or raw scanner command lines containing secrets.
4. Do not use `git clean`, `git reset`, `taskkill /IM`, server-side Sonar mutation, issue disposition changes, suppression, exclusion, baseline reset, or threshold/new-code configuration changes as an audit step.
5. A command that observes a mismatch must stop that future wave; it must not repair evidence by changing the claimed SHA or policy.

## Inputs

```powershell
# Set only non-secret identity/path values. Do not set or display SONAR_* values here.
$CandidateRoot = 'D:\Dev\netcoredbg-mcp\.agent\worktrees\issue450-eof-sonar-remediation'
$CoordinationRoot = 'D:\Dev\netcoredbg-mcp'
$BaselineSha = 'e95223ba1bddd7a08e440e4a0eca3db9f3c068b9'
$ProjectKey = 'thebtf_netcoredbg_mcp'
```

The final scanner worktree is intentionally not `$CandidateRoot`: future release scans use a new clean detached linked worktree at the exact candidate/post-merge SHA. `$CoordinationRoot` remains the only place where the runner resolves local credentials and stores secret-free evidence.

## 1. Read the Baseline Without Treating It as Closure

```powershell
$BaselineReceipt = Join-Path $CoordinationRoot ".agent\e\sonarqube\$ProjectKey\$BaselineSha\post-merge.json"
$baseline = Get-Content -Raw $BaselineReceipt | ConvertFrom-Json

[pscustomobject]@{
  CapturedHead       = $baseline.captured_head
  PostScanHead       = $baseline.post_scan_head
  ProjectKey         = $baseline.project_key
  BlockingIssues     = $baseline.issue_dispositions.blocking_count
  BlockingHotspots   = $baseline.hotspot_dispositions.blocking_count
  Gate               = $baseline.quality_gate.status
  NewCoverage        = ($baseline.quality_gate.conditions | Where-Object metricKey -eq 'new_coverage').actualValue
  CoverageThreshold  = ($baseline.quality_gate.conditions | Where-Object metricKey -eq 'new_coverage').errorThreshold
  NewViolations      = ($baseline.quality_gate.conditions | Where-Object metricKey -eq 'new_violations').actualValue
  IssuePagesComplete = $baseline.post_scan_issues.pagination_complete
  HotspotPagesComplete = $baseline.hotspots.pagination_complete
}
```

Expected starting facts are `1,076` blocking issues, zero blocking hotspots, `new_coverage=0.0`, threshold `80`, `new_violations=172`, a non-`OK` quality gate, and complete inventories. These values are a planning denominator only. Do **not** turn them into a Wave-3/4/5 result or edit the receipt.

## 2. Audit Wave-1 and Wave-2 Scope Before Focused Proof

Run these read-only source checks only as a supplement to the child’s behavior-first tests. A text result does not replace the child’s focused public behavior proof.

```powershell
Set-Location $CandidateRoot

# Wave 1 seam: current client EOF, manager event registration, and public liveness serialization.
Select-String -Path `
  'src\netcoredbg_mcp\dap\client.py', `
  'src\netcoredbg_mcp\session\manager.py', `
  'src\netcoredbg_mcp\session\state.py' `
  -Pattern 'stdout closed|_read_loop|TERMINATED|EXITED|debuggeeAlive'

# Wave 2 seam: detect that source is no longer relying on the forbidden default route.
Select-String -Path `
  'src\netcoredbg_mcp\build\cleanup.py', `
  'src\netcoredbg_mcp\build\session.py' `
  -Pattern 'taskkill|/IM|kill_all_netcoredbg|AssignProcessToJobObject|OpenProcess'
```

Future child proof commands are intentionally focused rather than broad:

```powershell
# Wave 1: execute only after child 012 has named its exact deterministic RED/GREEN test locations.
uv run --locked --extra dev pytest tests/test_client.py tests/test_debuggee_liveness.py -q

# Wave 2: execute only after child 013 has named its exact two-owner/admission proof locations.
uv run --locked --extra dev pytest tests/test_build_cleanup.py tests/test_build_session.py -q
```

The Wave-1 audit is incomplete unless it exercises raw EOF from `RUNNING` with a debuggee PID and an exited-without-terminated path. The Wave-2 audit is incomplete unless it uses two independent owners and proves no foreign selection; a source grep or one-process test cannot establish owner safety.

## 3. Audit a Future Wave-3 Exact-Head Coverage Transaction

Use only the repository runner. This command is a future scan operation and is intentionally not run during packet authoring:

```powershell
# In a new clean detached scanner worktree at the captured candidate SHA:
python scripts/run_sonarqube_exact_head.py --role candidate

# In a new clean detached scanner worktree at the actual post-merge origin/main SHA:
python scripts/run_sonarqube_exact_head.py --role post-merge
```

After a future Wave-3 diagnostic run, inspect only the secret-free receipt supplied as an explicit mandatory input. The receipt must be the path recorded by child 014’s exact run; a current dashboard URL is never an alternative input.

```powershell
param(
  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
  [string]$DiagnosticReceiptPath
)

$diagnostic = Get-Content -Raw $DiagnosticReceiptPath | ConvertFrom-Json

$coverage = $diagnostic.quality_gate.conditions |
  Where-Object metricKey -eq 'new_coverage' |
  Select-Object -First 1

[pscustomobject]@{
  CapturedHead = $diagnostic.captured_head
  PostScanHead = $diagnostic.post_scan_head
  ScannerRevision = $diagnostic.scanner_metadata.sonar_scm_revision
  AnalysisId = $diagnostic.quality_gate.analysis_id
  CurrentAnalysisRevision = $diagnostic.analysis_current_final.revision
  IssuesComplete = $diagnostic.post_scan_issues.pagination_complete
  HotspotsComplete = $diagnostic.hotspots.pagination_complete
  CoverageActual = $coverage.actualValue
  CoverageThreshold = $coverage.errorThreshold
  CoverageStatus = $coverage.status
  Gate = $diagnostic.quality_gate.status
}
```

The mandatory input is a real future artifact selected by the caller, not a synthetic path or a claim that a diagnostic receipt already exists. Child 014 must record its immutable receipt path in its own closure procedure; this parent guide does not invent a run ID.

A valid Wave-3 audit additionally requires child-014 evidence that both deterministic Cobertura files were generated after scanner begin and before scanner end, are nonempty, have nonzero relevant denominators, map to the captured source head, and were imported by the submitted analysis. The receipt alone cannot be treated as proof if the child’s report-provenance fields are absent.

## 4. Audit the Wave-4 One-Owner Manifest

The manifest must be supplied as a mandatory input from spec 018’s future exact integration evidence. It must derive from the most recent complete diagnostic receipt, not the baseline or a v0.23.10 partition.

```powershell
param(
  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
  [string]$ManifestPath
)

$manifest = Get-Content -Raw $ManifestPath | ConvertFrom-Json

$blocking = @($manifest.issue_inventory.keys)
$assignments = @($manifest.assignments)
$duplicateKeys = $assignments |
  Group-Object finding_key |
  Where-Object Count -ne 1 |
  Select-Object -ExpandProperty Name
$unownedKeys = $blocking | Where-Object { $_ -notin $assignments.finding_key }
$unknownOwners = $assignments |
  Where-Object { $_.owner_child -notin @('015', '016', '017') }

[pscustomobject]@{
  Head = $manifest.exact_head_ref.sha
  IssuePagesComplete = $manifest.issue_inventory.pagination_complete
  HotspotPagesComplete = $manifest.hotspot_inventory.pagination_complete
  BlockingKeyCount = $blocking.Count
  AssignmentCount = $assignments.Count
  DuplicateOrMissingAssignmentKeys = @($duplicateKeys).Count
  UnownedBlockingKeys = @($unownedKeys).Count
  InvalidOwners = @($unknownOwners).Count
  AssignmentHash = $manifest.assignments_hash
}
```

The mandatory manifest input must be a real future artifact. Duplicate, unowned, non-015/016/017, incomplete-page, or stale-head outcomes keep Wave 4 open. The zero denominator is valid only if the manifest also records `result_empty=true` and complete receipt binding.

## 5. Enforce the Exact Wave-4 → Wave-5 Barrier

Do this before any Wave-5 release task, tag, or publication action. The following command is read-only except for throwing locally on invalid evidence. Spec 018’s future `acceptance-receipt.md` must expose these machine-readable Markdown fields exactly once:

```text
**Integration SHA**: `40-character lowercase SHA`
**Diagnostic receipt**: `absolute or repository-relative JSON receipt path`
```

```powershell
param(
  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
  [string]$Wave4ClosurePath,
  [Parameter(Mandatory = $true)]
  [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
  [string]$Wave4DiagnosticPath
)

$closureText = Get-Content -Raw $Wave4ClosurePath
$integrationShaMatch = [regex]::Match(
  $closureText,
  '(?m)^\*\*Integration SHA\*\*:\s*`(?<sha>[0-9a-f]{40})`\s*$'
)
$diagnosticPathMatch = [regex]::Match(
  $closureText,
  '(?m)^\*\*Diagnostic receipt\*\*:\s*`(?<path>[^`]+)`\s*$'
)
if (-not $integrationShaMatch.Success -or -not $diagnosticPathMatch.Success) {
  throw 'WAVE_5_BLOCKED: spec-018 closure is missing required machine-readable identity fields.'
}
if ($diagnosticPathMatch.Groups['path'].Value -ne $Wave4DiagnosticPath) {
  throw 'WAVE_5_BLOCKED: supplied diagnostic receipt is not the receipt named by Wave-4 closure.'
}

$integrationSha = $integrationShaMatch.Groups['sha'].Value
$scan = Get-Content -Raw $Wave4DiagnosticPath | ConvertFrom-Json

$coverage = $scan.quality_gate.conditions |
  Where-Object metricKey -eq 'new_coverage' |
  Select-Object -First 1
$newViolations = $scan.quality_gate.conditions |
  Where-Object metricKey -eq 'new_violations' |
  Select-Object -First 1

$identities = @(
  $integrationSha,
  $scan.captured_head,
  $scan.post_scan_head,
  $scan.scanner_metadata.sonar_scm_revision,
  $scan.analysis_current_final.revision
)

if (($identities | Select-Object -Unique).Count -ne 1) {
  throw 'WAVE_5_BLOCKED: Wave-4 evidence identifies more than one source head.'
}
if (-not $scan.post_scan_issues.pagination_complete -or -not $scan.hotspots.pagination_complete) {
  throw 'WAVE_5_BLOCKED: Wave-4 diagnostic pagination is incomplete.'
}
if ($scan.issue_dispositions.blocking_count -ne 0 -or $scan.hotspot_dispositions.blocking_count -ne 0) {
  throw 'WAVE_5_BLOCKED: Wave-4 still has a blocking current issue or hotspot.'
}
if ($newViolations.actualValue -ne '0' -or $coverage.errorThreshold -ne '80' -or $coverage.status -ne 'OK') {
  throw 'WAVE_5_BLOCKED: new-code violations or unchanged coverage condition is not clean.'
}
if ($scan.quality_gate.status -ne 'OK') {
  throw 'WAVE_5_BLOCKED: analysis-bound quality gate is not OK.'
}

[pscustomobject]@{
  Wave5Entry = 'permitted only if no source bytes changed after this audit'
  IntegrationSha = $integrationSha
  Gate = $scan.quality_gate.status
  Coverage = $coverage.actualValue
  CoverageThreshold = $coverage.errorThreshold
  NewViolations = $newViolations.actualValue
}
```

The two paths are mandatory real future inputs. This audit proves neither that Wave 4 is already closed nor that no source byte will change later. Any later source change requires a new exact closure and rerun of this barrier.

## 6. Audit Wave-5 Exact-Head Release Evidence

After Wave 5 has legally entered, use the current `docs/RELEASE-PROTOCOL.md` and the child-019 quickstart. Candidate and post-merge receipt paths are deterministic from mandatory exact SHA inputs and the primary coordination root:

```powershell
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-f]{40}$')]
  [string]$CandidateSha,
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-f]{40}$')]
  [string]$PostMergeSha
)

$CandidateReceiptPath = Join-Path $CoordinationRoot ".agent\e\sonarqube\$ProjectKey\$CandidateSha\candidate.json"
$PostMergeReceiptPath = Join-Path $CoordinationRoot ".agent\e\sonarqube\$ProjectKey\$PostMergeSha\post-merge.json"
if (-not (Test-Path -LiteralPath $CandidateReceiptPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $PostMergeReceiptPath -PathType Leaf)) {
  throw 'RELEASE_BLOCKED: one or both exact-head receipt paths do not exist.'
}

$candidate = Get-Content -Raw $CandidateReceiptPath | ConvertFrom-Json
$postMerge = Get-Content -Raw $PostMergeReceiptPath | ConvertFrom-Json

if ($candidate.quality_gate.status -ne 'OK') {
  throw 'RELEASE_BLOCKED: candidate analysis-bound gate is not OK.'
}
if ($postMerge.quality_gate.status -ne 'OK') {
  throw 'RELEASE_BLOCKED: post-merge analysis-bound gate is not OK.'
}
if ($postMerge.captured_head -ne $postMerge.post_scan_head -or
    $postMerge.captured_head -ne $postMerge.scanner_metadata.sonar_scm_revision -or
    $postMerge.captured_head -ne $postMerge.analysis_current_final.revision) {
  throw 'RELEASE_BLOCKED: post-merge exact-head identity does not agree.'
}

[pscustomobject]@{
  CandidateSha = $candidate.captured_head
  PostMergeSha = $postMerge.captured_head
  PostMergeGate = $postMerge.quality_gate.status
  RequiredNextCheck = 'Verify annotated v0.23.11 tag target equals PostMergeSha and run the public installed-consumer canary.'
}
```

Only a future post-merge receipt can advance the tag gate. The final audit must also use the installed package’s public `netcoredbg-mcp` command and documented MCP journey; a source-tree run, internal helper, unit test, or candidate receipt alone is not a consumer release proof.

## Requirement Audit Matrix

| Requirement | Manual audit question |
|---|---|
| **PRG-001** | Does every Wave-1–4 artifact say `release_intent: none`, and is Wave 5 the only public v0.23.11 action? |
| **PRG-002** | Does focused behavior show one terminal/unavailable manager outcome after EOF/process/race paths with no stale live public state? |
| **PRG-003** | Does a two-owner proof show no foreign selection and a retained pre-resume owner capability for the selected tree? |
| **PRG-004** | Are both coverage reports deterministic, same-transaction, nonempty, mapped, imported, and evaluated at threshold `80`? |
| **PRG-005** | Does the fresh integration receipt have no blocking current findings/hotspots and complete page evidence? |
| **PRG-006** | Do all claimed source identities agree from captured scan head through analysis binding and final tag target? |
| **PRG-007** | Does the final proof exercise the public Python/default route without a preview-boundary or route-selection change? |
| **PRG-008** | Is there zero policy-waiver/suppression/exclusion/accepted-risk/baseline/threshold/new-code/server-policy path? |
| **PRG-009** | Does every blocking key in the fresh current inventory appear exactly once in the manifest union? |
| **PRG-010** | Are observed facts, inferences, primary sources, exact files, and future evidence identities all distinguishable to a reviewer? |

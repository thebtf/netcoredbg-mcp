# Validate cross-language coverage evidence

## Prerequisites

- Use a clean detached scanner worktree at the target commit.
- Use the existing primary-root `.env` rules from `docs/SONARQUBE-ONBOARDING.md`.
- Run the runner from an environment that contains the approved coverage dependencies.
- Derive `UV_PROJECT_ENVIRONMENT` below the coordination root. Do not create `.venv` inside the scanner worktree.

## Validate the runner contract

Run the focused runner tests after implementation.

```powershell
$coordinationRoot = Split-Path -Parent (git rev-parse --git-common-dir)
$env:UV_PROJECT_ENVIRONMENT = Join-Path $coordinationRoot ".agent\tmp\sonarqube-coverage-verify"
$env:PYTHONDONTWRITEBYTECODE = "1"
uv run --locked --extra dev python -m pytest tests/test_sonarqube_exact_head_runner.py -q -p no:cacheprovider
```

The suite must prove fresh run directories, marker binding, exact scanner argv for multiple .NET reports, timeout and cancellation handling, finally-owned cleanup, and every fail-closed report-validation path. Reuse or remove the external environment under the project scratch policy after the run.

## Validate a candidate scan

Create a new clean detached worktree at the final candidate SHA. Run the supported command from that worktree.

```powershell
python scripts/run_sonarqube_exact_head.py --role candidate
```

A passing receipt must contain Python and .NET evidence sets. Every binding must name the current run identifier, canonical marker digest, normalized relative path, source mapping digest, SHA-256, byte count, XML root, positive coverage denominator, and captured HEAD. Preserve the `analysis_coverage` binding: matching current-analysis readbacks around `/api/measures/component` with `coverage,lines_to_cover,new_coverage,uncovered_lines` for the submitted analysis. Aggregate metrics prove only aggregate coverage. Retain sanitized scanner coverage-sensor evidence before claiming per-language import. The scanner worktree must be clean after generated-artifact cleanup.

## Validate the merged target

After merge, create a new clean detached worktree at the actual `origin/main` SHA and run:

```powershell
python scripts/run_sonarqube_exact_head.py --role post-merge
```

The post-merge receipt must bind coverage evidence to the merged SHA. A scanner upload without both report sets, a canonical marker match, validated source mappings, positive coverage denominators, and bracketed analysis coverage metrics is BLOCKED, not PASS.

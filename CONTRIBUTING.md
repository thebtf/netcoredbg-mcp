# Contributing to netcoredbg-mcp

`netcoredbg-mcp` is a debugging bridge used by coding agents. Contributions must
preserve debugger correctness, path authority, process cleanup, and
sensitive-data boundaries.

## Development setup

Use Python 3.10 or later. Windows is required for the complete GUI automation
stack; non-GUI tests may run on other platforms when their platform-specific
dependencies are available or mocked.

```powershell
git clone https://github.com/thebtf/netcoredbg-mcp.git
cd netcoredbg-mcp
uv sync --locked --extra dev
uv run --no-sync netcoredbg-mcp --version
uv run --no-sync pytest --collect-only -q
```

The public package is a Python MCP server. Its source tree also carries bridge
and host code for development, but source-only host features are not an excuse
to change the published Python consumer contract without explicit coverage.

## Local development and tests

Run the smallest relevant gate first, then expand verification when the changed
behavior requires it:

```powershell
# Server entry point
uv run --no-sync netcoredbg-mcp --help
uv run --no-sync netcoredbg-mcp --version

# Focused Python tests
uv run --no-sync pytest tests/test_server_smoke.py -q
uv run --no-sync pytest tests/test_client.py tests/test_session.py -q

# Full Python suite
uv run --no-sync pytest -q

# Lint touched Python files
uv run --no-sync ruff check <changed-python-files>
```

Bug fixes require a regression test that fails before the repair and passes
after it. New public MCP tools require focused behavior coverage and server
registration coverage. Add or update a manual smoke scenario when a real debug
session can reproduce the user-visible failure.

### Manual smoke tests

The manual smoke suite drives real MCP tools against fixture applications.
Build all three fixtures below for a clean full Windows manual-smoke run.
`SmokeTestApp` is the required baseline. `WpfSmokeApp` is required because the
WPF V2 selector-scoped hover scenario fails when its output is absent.
`AvaloniaSmokeApp` is also part of the scenario set: its V2 state-oracle path
attempts `pre_build`, and a failed build is `INVALID_SETUP`/FAIL rather than a
skipped scenario.

```powershell
dotnet build tests/fixtures/SmokeTestApp -c Debug
dotnet build tests/fixtures/WpfSmokeApp -c Debug
dotnet build tests/fixtures/AvaloniaSmokeApp -c Debug
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
uv run --no-sync python tests/smoke_test_manual.py --list
uv run --no-sync python tests/smoke_test_manual.py
```

Record the scenario inventory with any smoke-test result so reviewers can see
what actually ran.

## Documentation

Update user documentation in the same pull request when behavior, commands,
environment variables, setup flow, or the public MCP surface changes.

- `README.md` is the canonical English consumer README.
- Update `README.ru.md` only after finalizing `README.md`; preserve heading
  order/levels, tables, list nesting, and fenced-code-block count.
- Keep examples generic. Do not put downstream project names, credentials,
  client configuration, or local machine paths in tracked documentation.
- Put release history in `CHANGELOG.md`; use `RELEASE_NOTES.md` only for the
  current published release.

## Sensitive data

Never commit:

- `.mcp.json`, `.netcoredbg-mcp.launch.json`, `.env`, logs, dumps, or local MCP
  client configuration.
- Credentials, tokens, API keys, connection strings, private hostnames, server
  inventories, or downstream project paths.
- Real user or company data in tests, docs, comments, fixtures, screenshots, or
  examples.

Use generic paths such as `C:\Work\MyDotNetApp` and
`C:\Tools\netcoredbg\netcoredbg.exe`. Launch profiles should inherit only the
environment values they need; never write secret values into repository files.

Before opening a pull request, scan the changed material for accidental private
markers using your local marker list. The scan must report no matches.

### Local SonarQube credentials

The exact-head SonarQube runner has one local credential source:
`<coordination-root>/.env`, where `coordination-root` is the parent of `git
rev-parse --git-common-dir`. It is gitignored, owner-only, and may contain only
`SONAR_HOST_URL`, `SONAR_TOKEN`, and `SONAR_READ_TOKEN`. Explicit values in the
runner's parent process override the corresponding `.env` values. Never put
`.env` in a detached scanner worktree, and never set `SONAR_ADMIN_TOKEN`: either
source is rejected by the runner. See
[`docs/SONARQUBE-ONBOARDING.md`](docs/SONARQUBE-ONBOARDING.md) for one-time
project-token creation, scanner-argv redaction, and the two required scan roles.

## Coding expectations

- Implement complete behavior; do not submit stubs, placeholder paths, or tests
  that prove only object construction.
- Validate user-controlled paths, launch arguments, environment profiles, and
  DAP payloads at the boundary.
- Redact public responses that could expose environment values, launch-profile
  data, sensitive paths, or process metadata.
- Prefer focused modules and explicit errors over hidden fallback behavior.
- Preserve observable behavior during refactors unless the pull request names
  and tests the intentional change.

## Branches, commits, and pull requests

Create a focused branch from current `main`:

```powershell
git switch main
git pull --ff-only origin main
git switch -c work/fix-short-description
```

Use Conventional Commit-style messages:

```text
feat(debug): add launch profile support
fix(dap): preserve null launch environment values
docs(readme): clarify screenshot evidence modes
test(server): cover tool registration
```

A pull request should state:

1. What changed and why.
2. Consumer behavior and compatibility impact.
3. Tests, smoke scenarios, or other evidence run.
4. Sensitive-data scan result when documentation, fixtures, logs, or launch
   environment handling changed.

All changes go through review; do not commit directly to `main`.

## Release boundary

Maintainers own versioning and publication. Keep a release change on its
release-prep branch while the required release gates build and install the
candidate, complete consumer-mode and remaining pre-PR checks, and pass review.
Only then merge it into `main`; create the annotated `vX.Y.Z` tag from the
verified merged commit and monitor its publish workflow. Do not create a release
tag from an unmerged pull request.

For the mandatory two exact-head SonarQube scans, follow
[`docs/SONARQUBE-ONBOARDING.md`](docs/SONARQUBE-ONBOARDING.md). It defines the
project-local credential names and the candidate/post-merge runner commands;
neither an unavailable credential nor a candidate receipt permits tag creation.

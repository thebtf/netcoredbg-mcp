# Contributing to netcoredbg-mcp

Thank you for improving `netcoredbg-mcp`. This project is a production-facing
debugging bridge for AI agents, so contributions need to preserve debugger
correctness, path safety, and sensitive-data hygiene.

## Setup

```powershell
git clone https://github.com/thebtf/netcoredbg-mcp.git
cd netcoredbg-mcp
uv sync --extra dev
uv run netcoredbg-mcp --setup
uv run pytest --collect-only -q
```

Use Python 3.10 or newer. Windows is required for the full GUI automation stack;
non-GUI unit tests can run elsewhere when platform-specific dependencies are
available or mocked.

## Local Development

```powershell
# Run the server help and version checks
uv run netcoredbg-mcp --help
uv run netcoredbg-mcp --version

# Run focused tests
uv run pytest tests/test_server_smoke.py -q

# Run the full unit suite
uv run pytest -q

# For Python changes, lint the files you touched
uv run ruff check <changed-python-files>
```

Before writing or changing tests, read `.agent/guides/TESTING_GUIDELINES.md` if
it exists in your local clone. The repository has historical tracked
`.agent/specs/` files; do not introduce new `.agent/` content or modify tracked
`.agent/` files unless the maintainer explicitly asks for that spec update.

## Manual Smoke Tests

The manual smoke suite exercises the real MCP tools against a debug fixture.
Build the fixture first when GUI checks are involved.

```powershell
dotnet build tests/fixtures/SmokeTestApp -c Debug
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
uv run python tests/smoke_test_manual.py
```

When fixing a bug found during live debugging, add or update a smoke scenario so
the same failure is observable before the fix and passing afterward.

## Sensitive Data Rules

Never commit:

- `.mcp.json`, `.netcoredbg-mcp.launch.json`, `.env`, logs, dumps, or local MCP
  client configuration files.
- Credentials, tokens, API keys, connection strings, IP inventories, server
  names, private project names, or local downstream checkout paths.
- Real user or company data in tests, docs, comments, fixtures, screenshots, or
  examples.

Use generic examples such as `C:\Work\MyDotNetApp` and
`C:\Tools\netcoredbg\netcoredbg.exe`. Launch profiles should use `inherit` for
secrets so values stay in the MCP server process environment and are not written
to repository files.

Before opening a PR, run a marker scan for accidental downstream references.
Keep project-specific marker lists out of the repository; pass them from your
local shell history, private notes, or reviewer instructions.

```powershell
rg -n --hidden --no-ignore -g '!.git/**' -g '!**/.git/**' -g '!.venv/**' -g '!**/.venv/**' "<private-marker-regex>"
```

The command should return no matches.

## Coding Standards

- Implement complete behavior; do not submit stubs, placeholders, or tests that
  only prove construction.
- Validate user-supplied paths, launch arguments, environment profiles, and DAP
  payloads at boundaries.
- Keep public responses redacted when they mention environment variables,
  launch profiles, paths that may be sensitive, or process metadata.
- Prefer small focused modules and explicit error messages over hidden fallback
  behavior.
- Preserve observable behavior during refactors unless the PR explicitly
  documents and tests the behavior change.

## Tests

Bug fixes require a regression test first. New MCP tools require focused unit
coverage plus server registration coverage. User-visible workflows should add or
update a smoke scenario when a real debug session can catch the failure class.

Useful commands:

```powershell
uv run pytest tests/test_server_smoke.py -q
uv run pytest tests/test_client.py tests/test_session.py -q
uv run pytest -q
```

If a test cannot run on your platform, state that in the PR and include the
closest verification you did run.

For Python changes, run `uv run ruff check <changed-python-files>` on the files
you touched. Do not use a docs-only PR to clean unrelated historical lint debt.

## Documentation

Update documentation in the same PR when behavior, commands, environment
variables, setup flow, or MCP surface changes.

- `README.md` is the canonical English README.
- `README.ru.md` must preserve the same heading structure and code fence count as
  `README.md`.
- Keep examples generic and free of private downstream project data.
- Use `CHANGELOG.md` for user-visible changes.

## Branches, Commits, and Pull Requests

Create a branch from `main`:

```powershell
git switch main
git pull origin main
git switch -c work/fix-short-description
```

Use conventional commit style:

```text
feat(debug): add launch profile support
fix(dap): preserve null launch environment values
docs(readme): refresh release documentation
test(server): cover tool registration
```

Pull requests should include:

- What changed and why.
- User-visible behavior and compatibility impact.
- Tests and smoke checks run.
- Sensitive-data scan result when docs, fixtures, logs, or launch environment
  handling changed.

All changes go through PR review. Do not commit directly to `main`.

## Release Notes

Releases are maintainer-driven. A release PR may prepare versioned documentation
for the upcoming tag, but tags are created only during the release step after
review and approval.

The release flow is:

1. Merge the release-ready PR.
2. Update local `main`.
3. Run the required tests and smoke checks.
4. Create an annotated `vX.Y.Z` tag.
5. Push the tag and monitor the publish workflow.

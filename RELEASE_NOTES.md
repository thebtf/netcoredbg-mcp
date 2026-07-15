# netcoredbg-mcp v0.23.1

Released: 2026-07-15

## Summary

`v0.23.1` is a PATCH release. It ships a source/developer preview of a .NET 8
MCP compatibility host that proxies the unchanged, still-authoritative Python
server, hardens non-mutating source-checkout launch guidance, and formalizes
release-process governance. It performs no .NET published-entrypoint cutover,
no roots/notification/resources/prompts parity implementation, and no Python
runtime behavior change.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   authoritative and backward compatible.
2. An installed wheel starts through the public CLI, initializes over MCP, and
   exposes the consumer-critical tools.
3. A supervised source-checkout launch can select the provider environment
   without mutating it on every restart and without replacing the target
   workspace CWD.
4. The .NET compatibility host remains a source/developer preview facade for
   real `initialize`/`list`/`call` exchange; it is not the published
   entrypoint and does not claim roots, progress/logging, resources, prompts,
   callbacks, or native tool-family parity.

## Highlights

- **Source/developer .NET compatibility host preview.**
  `host/NetCoreDbg.Mcp.Host` (.NET 8, stdio transport) proxies real MCP
  `initialize`, `tools/list`, and `tools/call` exchanges to the same,
  unmodified Python server. It builds only from a source checkout
  (`dotnet build host/NetCoreDbg.Mcp.Host`); it ships in neither the published
  wheel nor as a published entrypoint.
- **Non-mutating source-checkout launches.** Source-checkout MCP guidance now
  documents a one-time preparation `uv sync --locked --project <checkout>`
  step, followed by repeated supervised `uv run --no-sync --project <checkout>
  netcoredbg-mcp --project-from-cwd` restarts that never mutate the shared
  `.venv`, so the calling .NET workspace — not the `netcoredbg-mcp` checkout
  itself — keeps its role as the debug project root across restarts.
- **Release-process governance.** `docs/RELEASE-PROTOCOL.md` now documents
  planned PATCH/MINOR release autonomy behind the primary installed-consumer
  UXDD release gate, and `TECHNICAL_DEBT.md` converts the Python-to-.NET
  migration notes into an executable roadmap with parity, ownership, rollback,
  and cutover gates. Both are planning/process documents only; neither changes
  any published runtime behavior.

## Compatibility Boundary

> This host slice proxies only `tools/list` and `tools/call`; it does not
> relay downstream MCP roots, progress/log notifications, or other client
> callbacks. Until later reviewed changes add that front-door parity, launch
> the host with an explicit `--project`, set `NETCOREDBG_PROJECT_ROOT`, or use
> `--project-from-cwd` from the intended project directory. The published
> Python entrypoint retains its direct client-context behavior and remains the
> only supported production/consumer path.

## Explicit No-Cutover Statement

`v0.23.1` performs no .NET published-entrypoint cutover. The Python package
and `netcoredbg-mcp` console entrypoint remain authoritative, backward
compatible, and the only distribution channel. The .NET host is a
build-from-source preview facade for developers only; it is not installed by
`pip`/`pipx`, is not registered as a console script, and does not claim
production readiness.

## Upgrade Notes

All changes are additive documentation, process, and source-preview changes;
there is no intentional breaking change to the published Python entrypoint.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.1
# or
pipx upgrade netcoredbg-mcp
```

Install on a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.1
netcoredbg-mcp setup
```

## Known Residual Scope

- Engram #385 (downstream MCP roots relay) and Engram #386 (upstream
  progress/log notification relay) remain open; the .NET host proxies
  neither.
- A remaining protocol-surface audit (resource templates/subscriptions,
  prompts, `completion/complete`, sampling, elicitation, and other negotiated
  callbacks) is still open per `TECHNICAL_DEBT.md`; any consumer-visible gap
  it finds becomes its own required parity slice before entrypoint cutover.
- No .NET published-entrypoint cutover, FD-001/FD-002/FD-003 parity
  implementation, or Python route retirement is in scope for this release.

## Release Gates

- Integration base: merged PRs `#227`, `#228`, `#229`, and `#230` on
  `main@50a985e57be38e991bbed2f2dda82a82ed6553a4` (Engram `#384`, `#387`).
- Version parity: `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`,
  `uv.lock`, `README.md`, `README.ru.md`, `CHANGELOG.md`, and
  `RELEASE_NOTES.md` all report `0.23.1`/`v0.23.1`.
- Full suite: `uv run --locked --extra dev pytest` — 2032 collected, 2029
  passed, 3 skipped (Unix-only process-cleanup tests on Windows), and one
  known `RuntimeWarning` from a mocked `create_subprocess_exec` coroutine in
  `tests/test_build_cleanup.py`.
- Ruff: `uv run --locked --extra dev ruff check .` — all checks passed.
- Critical suite: `uv run --locked --extra dev pytest tests/critical -m
  critical` — 14 passed.
- Runtime-smoke docs/schema/critical suite: `uv run --locked --extra dev
  pytest tests/test_runtime_smoke_v2_docs.py
  tests/test_runtime_smoke_diagnostics_schema.py
  tests/critical/test_runtime_smoke_v2_critical.py` — 47 passed.
- Debug fixture builds: `dotnet build tests/fixtures/SmokeTestApp -c Debug`,
  `dotnet build tests/fixtures/WpfSmokeApp -c Debug`, and `dotnet build
  tests/fixtures/AvaloniaSmokeApp -c Debug` — each 0 warnings, 0 errors.
- Release host build: `dotnet build
  host/NetCoreDbg.Mcp.Host/NetCoreDbg.Mcp.Host.csproj -c Release` — 0
  warnings, 0 errors; `uv run --locked --extra dev pytest
  tests/test_host_proxy.py -q` — 3 passed.
- Package build: `uv build` produced `netcoredbg_mcp-0.23.1.tar.gz` and
  `netcoredbg_mcp-0.23.1-py3-none-any.whl`.
- Disposable wheel install: both `netcoredbg-mcp --version` and
  `python -m netcoredbg_mcp --version` reported `0.23.1`.
- Installed-consumer MCP exchange: an official MCP client initialized the
  installed console script from an external temporary .NET project
  (`x-mux` sharing isolated); it listed 135 tools with none missing, and
  `runtime_smoke_validate_plan` returned `PASS`, with `find_code_symbol`
  resolving `Program.cs` under the exact external project root.
- Concurrent source-checkout exchange: two concurrent `uv run --no-sync
  --project ...` sessions each initialized, listed 135 tools, and validated a
  relative plan from the caller project; the provider console-script
  fingerprint stayed unchanged across both sessions.

PR review, tag creation, publication, and post-publication verification are
not claimed by these local gates and remain required before release
completion.

# netcoredbg-mcp v0.23.6

Planned release: 2026-08-18

## Summary

`v0.23.6` is a PATCH hotfix for optional lossless screenshot evidence. The
existing `ui_take_screenshot` navigation preview remains the default; the
session-scoped evidence mode additionally provides the original PNG, SHA-256,
capture/DPI/geometry metadata, and an optional raw-derived crop.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. This release adds no tools, prompts, resources, or breaking API changes;
   the public catalog remains 135 tools, 8 prompts, and 4 resources.
3. Inline preview is navigation-only, not acceptance evidence. The evidence
   mode does not add DOM/WPF metrics, pixel diff, model calibration, or operator
   sign-off.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.6
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.6
netcoredbg-mcp --setup
```

## Known Residuals

- The .NET compatibility host remains source-only; it is not included in the
  wheel and is not a published entrypoint.
- Python remains the execution authority behind that preview; no native .NET
  tool-family migration or Python-runtime retirement is part of this release.
- MCP Tasks remain deliberately unadvertised and unsupported until the Python
  authority negotiates one exact protocol dialect.

## Release-Gate Status

The candidate requires version parity, wheel build, installed CLI/import smoke,
installed MCP consumer journeys, required Python suites, PR review, merge, tag
publication, and post-publication verification before release completion.

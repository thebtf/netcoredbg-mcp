# netcoredbg-mcp v0.23.5

Planned release: 2026-08-17

## Summary

`v0.23.5` is a PATCH hotfix for scoped WPF key delivery. It fails closed unless
the native FlaUI bridge proves foreground ownership and target focus before it
sends the requested key sequence.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. This release adds no tools, prompts, resources, or breaking API changes;
   the public catalog remains 135 tools, eight prompts, and four resources.
3. `ui_key_sequence` returns `PASS` only when the native bridge confirms both
   `foreground_verified` and `target_focus_verified` for the scoped target.
   Missing focus proof reports `FAIL` and sends no success claim.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.5
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.5
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

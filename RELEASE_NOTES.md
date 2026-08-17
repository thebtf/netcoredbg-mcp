# netcoredbg-mcp v0.23.4

Planned release: 2026-08-17

## Summary

`v0.23.4` is a PATCH release that clarifies the public WPF submenu workflow in
the `ui_key_sequence` and `ui_invoke` tool descriptions.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. This release adds no tools, prompts, resources, or breaking API changes;
   the public catalog remains 135 tools, eight prompts, and four resources.
3. For a WPF submenu, send scoped `ENTER` to the parent `MenuItem`, rediscover
   the popup child, and invoke that exact child separately. Invoking the parent
   alone does not guarantee that submenu peers materialize.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.4
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.4
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

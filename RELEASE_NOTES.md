# netcoredbg-mcp v0.23.7

Released: 2026-08-20

## Summary

`v0.23.7` is a PATCH release. `search_source` now runs regex matching in a
bounded dedicated Python subprocess. Source-file enumeration and waiting for
that worker remain in the MCP server process. `find_code_symbol`,
`find_code_references`, and `get_source_context` remain in-process. In the
same range, the source-only .NET host gained the reviewed Native Scene Probe
M0/M1 capability; it is not part of the published wheel and is not a published
entrypoint.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. This release adds no tools, prompts, resources, or breaking API changes;
   the public catalog remains 135 tools, 8 prompts, and 4 resources.
3. `search_source` runs regex matching in a bounded dedicated subprocess.
   Worker failures are surfaced as tool errors, while source-file enumeration
   and waiting for the worker remain in-process. The other code-search tools
   remain in-process.
4. The Native Scene Probe M0/M1 capability is internal and source-only: it
   adds no consumer-visible surface in this release.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.7
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.7
netcoredbg-mcp --setup
```

## Known Residuals

- The .NET compatibility host remains source-only; it is not included in the
  wheel and is not a published entrypoint. The native-scene capability ships
  as reviewed source with its acceptance receipt under
  `specs/004-native-scene-probe/`.
- Python remains the execution authority; no native .NET tool-family migration
  or Python-runtime retirement is part of this release.
- MCP Tasks remain deliberately unadvertised and unsupported until the Python
  authority negotiates one exact protocol dialect.
- External PR review debt: the Codex reviewer timed out on the native-scene
  capability PR (#255); CodeRabbit approved that head. Any later Codex
  findings are scheduled for the next patch.

## Release evidence

The completed release passed version parity, wheel build, installed CLI/import
smoke, installed MCP consumer journeys, required Python suites, PR review,
merge, annotated-tag publication, and post-publication verification. The
published tag is `v0.23.7` on `6a378b546758ea05515044ac5bb64a6c3fb49bcf`.

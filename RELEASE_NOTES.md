# netcoredbg-mcp v0.23.10

Prepared: 2026-08-25

## Summary

`v0.23.10` is a PATCH release candidate for public screenshot-capture reliability. `ui_take_screenshot` carries validated crop and strict-capture inputs through the complete request path without internal assertions. Stealth foreground restoration runs off the MCP event loop and completes before later bridge/capture foreground mutations, preventing a launch-restoration race.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain the authoritative, backward-compatible consumer path.
2. The public catalog remains 135 tools, 8 prompts, and 4 resources; this patch adds no tool, prompt, resource, or breaking API change.
3. `ui_take_screenshot` returns its documented validation error for incomplete, invalid, or unusable crop and strict-capture input instead of exposing an assertion failure from the implementation.
4. During a stealth debug launch, native foreground restoration runs off the MCP event loop and is joined before later bridge or capture foreground mutations, preventing a competing restore from racing UI evidence.
5. Strict screenshot capture remains fail-closed: incomplete, black, malformed, foreign-target, or mismatched strict evidence is not persisted as accepted evidence.

## Retained Reliability Guarantee

`search_source` now runs regex matching in a bounded dedicated Python subprocess. Source-file enumeration and waiting for that worker remain in the MCP server process. Worker failures are surfaced as tool errors and do not crash the MCP server process.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.10
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.10
netcoredbg-mcp --setup
```

## Known Residuals

- Strict screenshot capture remains fail-closed when its expected PID, HWND, geometry, DPI, or foreground proof is unavailable or changes during capture.

## Release Evidence

The candidate has not been merged, tagged, or published. Exact candidate build, installed consumer smoke, PR review, and exact-head Sonar evidence are pending; no pending gate is represented as complete.

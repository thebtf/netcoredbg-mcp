# netcoredbg-mcp v0.23.9

Prepared: 2026-08-23

## Summary

`v0.23.9` is a PATCH release candidate containing one consumer-facing strict
screenshot recovery. When a valid strict PrintWindow raster is all black,
`ui_take_screenshot` makes exactly one explicit typed BitBlt alternate attempt.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. The public catalog remains 135 tools, 8 prompts, and 4 resources; this patch
   adds no tool, prompt, resource, or breaking API change.
3. Strict screenshot recovery accepts a BitBlt alternate only after foreground,
   PID, HWND, physical geometry, and DPI provenance checks pass. The response
   truthfully identifies the alternate as `typed_bitblt_fallback` / `BitBlt`.
4. All incomplete, black, malformed, foreign-target, or mismatched strict
   evidence fails closed without persisting raw or crop artifacts.
5. The single-file published FlaUI bridge reports its truthful running-artifact
   identity, and public consumer evidence proves post-session cleanup liveness.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.9
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.9
netcoredbg-mcp --setup
```

## Known Residuals

- Strict screenshot capture remains fail-closed when its expected PID, HWND,
  geometry, DPI, or foreground proof is unavailable or changes during capture.

## Release Evidence

The release-candidate evidence records the exact wheel, installed CLI/import
smoke, strict all-black recovery matrix, and cleanup-liveness proof. This
candidate has not been merged, tagged, or published.

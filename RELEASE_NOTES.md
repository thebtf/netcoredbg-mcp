# netcoredbg-mcp v0.23.8

Prepared: 2026-08-22

## Summary

`v0.23.8` is a PATCH release candidate containing one consumer-facing fix:
`ui_take_screenshot` now identifies its raw-raster geometry and capture
provenance with physical-pixel units and DPI context, and exposes honest target
comparability for an explicitly expected HWND and physical dimensions.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. The public catalog remains 135 tools, 8 prompts, and 4 resources; this patch
   adds no tool, prompt, resource, or breaking API change.
3. Default `ui_take_screenshot()` output remains a WebP navigation preview with
   `evidence_grade=preview_only`. With `evidence=true`, the result carries
   lossless raw-raster metadata including physical dimensions, DPI, and capture
   provenance.
4. When the caller supplies `expected_hwnd`, `expected_physical_width`, and
   `expected_physical_height`, the screenshot result reports `MATCHED` or
   `MISMATCH`; raw evidence persists only for a matched physical target.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.8
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.8
netcoredbg-mcp --setup
```

## Known Residuals

- Target comparability is `UNASSERTED` when a screenshot call does not provide
  an expected HWND and physical dimensions; this is an honest absence of a
  caller-provided comparison target, not a successful geometry assertion.

## Release Evidence

The release-candidate evidence records the exact wheel, installed CLI/import
smoke, focused screenshot contracts, and built public NovaScript capture. This
candidate has not been merged, tagged, or published.

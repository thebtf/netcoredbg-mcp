# netcoredbg-mcp v0.23.3

Planned release: 2026-08-13

## Summary

`v0.23.3` is a PATCH release with two reliability fixes: fail-closed handling
for untrustworthy near-black UI screenshots, and safer startup cleanup of stale
managed temp directories. It also makes foreground recovery more reliable when
leaving stealth mode.

## Consumer Claims

1. The published Python package and `netcoredbg-mcp` console entrypoint remain
   the authoritative, backward-compatible consumer path.
2. This release adds no tools, prompts, resources, or breaking API changes; the
   public catalog remains 135 tools, eight prompts, and four resources.
3. Near-black screenshots fail closed as `PROBABLE_BLACK_FRAME` with luminance
   metrics, foreground-mutation provenance, and an explicit recovery step.
   An annotated screenshot invalidates its cached annotation data before
   returning the same classification.
4. `ui_bring_to_front` supports a pywinauto HWND foreground-restoration
   fallback and clears stealth mode only after activation succeeds.
5. Startup stale-temp cleanup rejects unrelated names before calling `is_dir`
   or `stat`, so it does not probe entries outside its managed prefix.

## Highlights

### Trustworthy visual evidence

A screenshot that is overwhelmingly near-black is not presented as visual
evidence. The server returns `PROBABLE_BLACK_FRAME` with its luminance analysis
and whether it attempted a foreground mutation. Recover explicitly with
`ui_bring_to_front`, then retry the screenshot. Annotated-screenshot requests
clear cached annotation data when they reject such a frame.

### Foreground recovery after stealth mode

`ui_bring_to_front` can use its pywinauto HWND foreground-restoration fallback
when the active backend does not provide its own foreground activation method.
A successful result exits stealth mode; an unsuccessful activation leaves that mode unchanged.

### Managed temp cleanup stays scoped

At startup, stale-temp GC filters entries by its managed session prefix before
directory or metadata checks. Unrelated temporary entries are therefore not
probed by this cleanup path.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.
The Python package and `netcoredbg-mcp` console script remain the only published
entrypoint; this release performs no .NET entrypoint cutover.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.3
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.3
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

The `0.23.3` wheel build, version parity, installed CLI/import smoke, primary
installed MCP consumer journeys, focused regressions, and configured full
Python suite are verified. Release-PR review, required CI, merge, tag
publication, and post-publication checks follow afterward.

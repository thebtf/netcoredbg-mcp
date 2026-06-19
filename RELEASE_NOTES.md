# netcoredbg-mcp v0.18.7

Released: 2026-06-19

## Summary

`v0.18.7` is a patch release for the runtime-smoke and UI-emulation hardening
roadmap. It publishes CR-054 after `v0.18.6`, giving package consumers verified
right-click and double-click flows without changing the existing
`ui.click_verified` contract.

## Highlights

- Runtime-smoke v2 now supports `ui.right_click_verified` and
  `ui.double_click_verified`.
- Runtime-smoke operation adapters now expose `ui.right_click` and
  `ui.double_click` through the same target-proof and postcondition behavior as
  verified primary clicks.
- Verified click variants fail closed before side effects when selector proof
  does not match the intended target.
- Pywinauto fallback click-center resolution now accepts `rectangle` geometry
  payloads as well as `rect`, preserving coordinate-click behavior on fallback
  backends.
- Broad issue `#270` remains open for DataGrid-specific and arbitrary click
  variants, broader offscreen/scroll semantics, drag/drop tails, downstream
  replay coverage, and broader helper ergonomics.

## Upgrade Notes

- This is a PATCH release. It preserves the `v0.18.x` public API shape and
  expands runtime-smoke click coverage with verified secondary click variants.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.18.7
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.18.7
  netcoredbg-mcp --setup
  ```

- Runtime-smoke and UI helper failures continue to prefer bounded `BLOCKED`,
  `INVALID_SETUP`, `WARN`, or `FAIL` evidence over false `PASS` results.
- Broad issue-backlog rows remain open by design; this release ships bounded
  provider-side slices, not every downstream consumer replay.

## Release Gates

- Release-prep PR must pass MCP PR review before merge.
- Critical suite must pass with `uv run --locked --extra dev pytest tests/critical -m critical`.
- Runtime-smoke docs/schema gates must pass for the shipped examples.
- Package build and disposable wheel install smoke must pass.
- Production playbook must be executed and recorded before tagging.
- After tag push, GitHub Release, PyPI workflow, remote tag, and local
  workstation deployment must be verified before the release is called shipped.

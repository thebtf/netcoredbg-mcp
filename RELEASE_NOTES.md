# netcoredbg-mcp v0.20.3

Released: 2026-06-22

## Summary

`v0.20.3` is a PATCH release that publishes the post-`v0.20.2` roadmap
boundary. The provider-side CAP-UI no-operator confidence layer is released and
deployed; external NovaScript acceptance remains tracked separately in Engram
`#331` and is not treated as hidden provider work.

This release does not add new runtime behavior beyond `v0.20.2`. It refreshes
the package docs, reproduction backlog, and release notes so consumers and
future agents do not reopen covered provider slices without concrete downstream
failure evidence.

## Highlights

- Records `CR-108`, the post-`v0.20.2` downstream-wait boundary.
- Clarifies that missing NovaScript PASS/FAIL evidence is external acceptance
  debt, not a provider-readiness blocker.
- Keeps broad issues `#268`, `#269`, `#270`, `#271`, and `#272` open by design
  while preventing duplicate provider-code work from broad issue openness alone.
- Updates README/README.ru release headlines to `v0.20.3` and the current
  collected-test count.

## Upgrade Notes

- This is an additive PATCH documentation/readiness release. Existing
  runtime-smoke v2 behavior is unchanged from `v0.20.2`.
- Upgrade an existing pip or pipx install with one of:

  ```powershell
  python -m pip install --upgrade netcoredbg-mcp==0.20.3
  pipx upgrade netcoredbg-mcp
  ```

- For a new workstation install:

  ```powershell
  pipx install netcoredbg-mcp==0.20.3
  netcoredbg-mcp --setup
  ```

## Known Residual Scope

- Broad issue-backlog rows `#268`, `#269`, `#270`, `#271`, and `#272` remain
  open by design. This release does not claim full broad FR closure.
- NovaScript issue `#331` remains the external downstream acceptance follow-up.
  A concrete provider `FAIL` there should start a new RED-first provider CR;
  PASS should drive only the justified lifecycle/split/closure update.
- `run_confidence.no_operator` can classify dirty or unproven execution
  windows, but it does not itself isolate the operating system keyboard,
  pointer, foreground window, or desktop session.

## Release Gates

- Release-git-readiness passed before release-prep: local `main` was clean,
  synchronized with `origin/main`, and contained one post-`v0.20.2` commit:
  `0d75612 docs(roadmap): record post-v0.20.2 downstream wait boundary`.
- Test discovery reports `1812 tests collected`.
- Version parity is expected across `pyproject.toml`,
  `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog,
  release notes, and annotated tag `v0.20.3`.
- Mandatory release gates must pass before this release is marked shipped:
  critical suite, runtime-smoke docs/schema gate, package build, wheel install
  smoke, production playbook applicability check, MCP PR review, annotated tag
  publication, GitHub Release/PyPI publication, and local workstation deploy
  smoke.

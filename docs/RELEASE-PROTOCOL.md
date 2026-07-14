# Release Protocol

## Applies When

This protocol applies to every versioned `netcoredbg-mcp` release published from
`main`, including milestone tags such as `v0.18.0`.

It is mandatory when a change affects any of these surfaces:

- Python package metadata or runtime package contents.
- MCP tools, prompts, resources, or CLI behavior.
- Runtime-smoke, UI automation, FlaUI bridge, or fixture behavior.
- GitHub Actions release or package-publish workflows.
- Public documentation, examples, release notes, or production testing
  playbooks used by package consumers.

## Additional Release Surfaces

| Surface | Version source | Publish command | Verification |
| --- | --- | --- | --- |
| Python package `netcoredbg-mcp` | `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`, `uv.lock` | Push annotated tag `vX.Y.Z`; `.github/workflows/publish.yml` publishes to PyPI | `gh run view` for the tag workflow, `python -m pip install netcoredbg-mcp==X.Y.Z` or local wheel smoke when PyPI propagation is pending |
| GitHub Release | Annotated git tag `vX.Y.Z` | `.github/workflows/publish.yml` creates a release on `refs/tags/v*` | `gh release view vX.Y.Z` |
| TestPyPI dry run | Same package version | Manual `workflow_dispatch` with `target=testpypi` | `gh run view` and TestPyPI package page when used |
| CLI entry point | `src/netcoredbg_mcp/__init__.py` | Wheel install or editable local deploy | `netcoredbg-mcp --version` prints `X.Y.Z` |
| MCP server surface | Source package and bridge files included by `pyproject.toml` | Wheel build and installation | Critical suite MCP-surface test passes |
| FlaUI bridge sources | `pyproject.toml` `force-include` bridge paths | Wheel build and installation | Bridge build or compile-only gate passes on Windows when bridge files changed |
| Public docs and examples | `README.md`, `README.ru.md`, `CHANGELOG.md`, `RELEASE_NOTES.md`, `docs/` | Release-prep PR | Docs tests and production playbook checks pass or block release |

## Required Gates

| Gate | Command / evidence | Blocks release when |
| --- | --- | --- |
| Release git readiness | `git fetch --prune origin`; release branch clean and equal to `origin/main`; stale worktrees/branches classified and cleaned preserve-first | Any dirty, unique, abandoned, stale, or unsynchronized release state remains |
| Release protocol presence | This file is read and every mandatory row is represented in the release report | Protocol missing, stale, or silent on an active release surface |
| Version parity | Check `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`, `uv.lock`, README release copy, changelog, and release notes | Any public version surface disagrees with `X.Y.Z` / `vX.Y.Z` |
| Changelog and release notes | `CHANGELOG.md` has a dated `X.Y.Z` section; `RELEASE_NOTES.md` has user-facing notes | Either file missing or contains only generic placeholder text |
| Documentation refresh | README and README.ru reflect the current tool/test counts and release highlights; docs examples touched since last tag have matching tests | Public docs still describe an older released surface |
| Critical suite | `uv run --locked --extra dev pytest tests/critical -m critical` | Any `@critical` test fails or the suite cannot run |
| Runtime-smoke docs/examples | `uv run --locked --extra dev pytest tests/test_runtime_smoke_v2_docs.py tests/test_runtime_smoke_diagnostics_schema.py tests/critical/test_runtime_smoke_v2_critical.py` or a narrower documented equivalent | Docs examples, diagnostic schemas, or v2 critical guards fail |
| Package build | `uv build` | Wheel or sdist build fails |
| Wheel install smoke | Install the built wheel into a disposable environment and run `netcoredbg-mcp --version` plus an import smoke | Install, import, or CLI version smoke fails |
| Production playbook | Execute `docs/PRODUCTION-TESTING-PLAYBOOK.md` in customer mode and record a run report | Overall verdict is `BROKEN` or `PARTIALLY_WORKS` for a release that claims those product flows as shipped |
| MCP PR review | Release-prep PR summary reports zero unresolved blocking findings, and reviewer status is clean enough for merge | Any `fix_now` or unresolved mandatory review thread remains |
| Tag publication | Annotated `vX.Y.Z` tag pushed to origin and visible through `git ls-remote --tags origin refs/tags/vX.Y.Z` | Tag is missing, lightweight, unpushed, or collides with an existing remote tag |
| GitHub/PyPI publication | Tag workflow succeeds; GitHub release exists; PyPI package is visible or propagation delay is explicitly recorded with workflow success evidence | Workflow fails, release missing, package missing after normal propagation, or verification cannot be performed |
| Local deploy smoke | Workstation installation is updated to the released package or released wheel; `netcoredbg-mcp --version` reports `X.Y.Z` | Local executable still reports the prior version |

## Release Autonomy

| Mutation class | Autonomy | Approval trigger | Evidence |
| --- | --- | --- | --- |
| Local release-prep branch and commit | Automatic | Sensitive content, incoherent diff, or unrelated dirty state | Git status, diff, and gate output |
| Release-prep PR creation | Automatic | Unreviewed broad product change outside release-owned files | PR URL and changed-file list |
| PR merge | Automatic after independent MCP PR review and required checks are clean | `fix_now`, unresolved mandatory review threads, failed checks, or high-risk scope expansion | MCP PR summary, GitHub merge state, status checks |
| PATCH or MINOR tag and remote publication | Automatic after the completed integration scope is on `main`, no dependent slice in the same integration wave remains active, and every gate in this protocol passes | MAJOR/breaking change, tag collision, failed release gate, missing publication evidence, ambiguous scope, or production/customer deployment outside this workstation | Remote tag, workflow status, release URL, package smoke |
| MAJOR or breaking release | Approval required | Always | Explicit user approval naming the version |
| Production/customer deployment outside this workstation | Approval required | Always | Named target, deploy plan, health checks |

Project default: `auto_patch_minor_after_verified_integration`. A separate
`release`, `go ahead`, or equivalent command is not required once a concrete
integration scope has reached the automatic trigger above. The release still
stops on any failed evidence gate; approval is not a substitute for green gates.

## Version Alignment

All of these must match the target version before the release commit:

- `pyproject.toml` project version.
- `src/netcoredbg_mcp/__init__.py` `__version__`.
- `uv.lock` editable package version.
- `README.md` and `README.ru.md` release headline.
- `CHANGELOG.md` release section.
- `RELEASE_NOTES.md` title and installation examples.
- Annotated git tag `vX.Y.Z`.

## Release Notes

`CHANGELOG.md` is the technical history. `RELEASE_NOTES.md` is the operator and
consumer summary used for PR review and GitHub release notes. It must include:

- Release version and date.
- Main user-visible changes.
- Upgrade notes and compatibility caveats.
- Release gates and known residual risks.

GitHub auto-generated release notes are allowed as an additional artifact, not
as the only release-note source for a milestone release.

## Publish / Smoke / Handoff

1. Prepare release-owned files on a branch named `work/release-vX.Y.Z-prep`.
2. Run release gates locally and write evidence paths into the progress report.
3. Open a PR, run MCP PR review, fix or resolve findings, and merge only after
   review readiness is clean.
4. Fast-forward local `main` to `origin/main`.
5. Create an annotated tag with `git tag -a vX.Y.Z -m "Release vX.Y.Z"` and
   push it with `git push origin vX.Y.Z`.
6. Verify the tag workflow, GitHub Release, and PyPI publication.
7. Deploy to this workstation by installing the released wheel/package, then
   verify `netcoredbg-mcp --version` and a package import smoke.
8. Update `.agent/CONTINUITY.md` and the live dashboard with the final verdict.

## Terminal Verdict

- `PROJECT_RELEASE_PROTOCOL_PASS`: all mandatory rows have current evidence.
- `PROJECT_RELEASE_PROTOCOL_BLOCKED`: at least one mandatory row is missing,
  stale, failed, or cannot be verified.
- `PROJECT_RELEASE_PROTOCOL_DRY_RUN`: intended actions are fully described and
  no mutation was performed.

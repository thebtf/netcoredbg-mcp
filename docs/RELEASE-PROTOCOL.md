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

## Release Decision Order

Release decisions use this order; later evidence cannot overrule an earlier failed gate:

1. **Primary — UXDD consumer outcome.** Build and install the release candidate, then exercise every user journey claimed by the release through the same public CLI/MCP entry point and packaging shape a consumer receives. Every claimed journey must reach `PRODUCT_WORKS`.
2. **Supporting — test protocols.** Run the required unit, integration, critical, runtime-smoke, build, and packaging checks. They are mandatory evidence, but green tests cannot turn `PARTIALLY_WORKS` or `BROKEN` consumer behavior into a releasable product.
3. **Supporting — independent review and release mechanics.** Resolve blocking review findings, prove version parity, and complete git, tag, publication, and post-publication checks.

A planned PATCH/MINOR release inside a legitimate run is autonomous. A legitimate run is a bounded spec, PRD, ADR, or active run contract with explicit acceptance criteria and release intent. User review, approval, and a separate `release` / `go ahead` command are not routine gates. If the governing artifact omits release intent or marks release out of scope, do not infer a release from implementation alone.

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

Pre-publication gates must pass before annotated tag creation and push. Tag
creation and push depend only on these rows plus a clean tag-collision check;
they do not wait on remote tag visibility, workflow completion, GitHub Release,
PyPI publication, or post-publication local workstation deploy evidence. The
built-wheel install smoke row remains mandatory before tagging.

Post-publication verification rows confirm publication after the tag is pushed.
Any failed gate or verification row stops release completion; failed
pre-publication gates also block tag creation.

### Pre-publication gates

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
| UXDD consumer-mode release gate | Build and install the release candidate; execute `docs/PRODUCTION-TESTING-PLAYBOOK.md` through the public package/CLI/MCP surface; enumerate every user journey claimed by the release | Any claimed journey is not `PRODUCT_WORKS`; `PARTIALLY_WORKS`, `BROKEN`, private-helper-only proof, or unit-test-only proof blocks release |
| MCP PR review | Release-prep PR summary reports zero unresolved blocking findings, and reviewer status is clean enough for merge | Any `fix_now` or unresolved mandatory review thread remains |
| Tag collision check | `git ls-remote --tags origin refs/tags/vX.Y.Z` returns empty before tag creation | Target tag already exists on origin |

### Post-publication verification

| Verification | Command / evidence | Blocks release completion when |
| --- | --- | --- |
| Remote tag visibility | Fetch `refs/tags/vX.Y.Z` into a temporary non-tag ref (for example `git fetch origin refs/tags/vX.Y.Z:refs/tmp/verify-vX.Y.Z`); require `git cat-file -t refs/tmp/verify-vX.Y.Z` to equal `tag`; delete the temporary ref; and confirm `git ls-remote --tags origin refs/tags/vX.Y.Z` is non-empty | Tag is missing, lightweight, or not visible on origin |
| Tag workflow completion | Locate the exact `.github/workflows/publish.yml` run triggered by `event=push` for tag `vX.Y.Z` whose head SHA matches the annotated tag target; capture that run ID; require `gh run view <run-id>` to report `completed` / `success` | Workflow fails, is ambiguous, or cannot be verified |
| GitHub Release | `gh release view vX.Y.Z` | Release missing |
| PyPI publication | PyPI package is visible or propagation delay is explicitly recorded with workflow success evidence | Package missing after normal propagation, or verification cannot be performed |
| Local deploy smoke | Workstation installation is updated to the released package or released wheel; `netcoredbg-mcp --version` reports `X.Y.Z` | Local executable still reports the prior version |

## Release Autonomy

| Mutation class | Autonomy | Approval trigger | Evidence |
| --- | --- | --- | --- |
| Local release-prep branch and commit | Automatic | Sensitive content, incoherent diff, or unrelated dirty state | Git status, diff, and gate output |
| Release-prep PR creation | Automatic | Unreviewed broad product change outside release-owned files | PR URL and changed-file list |
| PR merge | Automatic after the primary UXDD consumer gate, independent MCP PR review, and required checks are clean | `PARTIALLY_WORKS`, `BROKEN`, `fix_now`, unresolved mandatory review threads, failed checks, or high-risk scope expansion | UXDD run report, MCP PR summary, GitHub merge state, status checks |
| Planned PATCH or MINOR tag and remote publication | Automatic when the release belongs to a legitimate run, its completed integration scope is on `main`, no dependent slice in the same integration wave remains active, every claimed consumer journey is `PRODUCT_WORKS`, and every pre-publication gate passes | Missing release intent, MAJOR/breaking change, tag collision, failed gate, ambiguous scope, production/customer deployment outside this workstation, secrets, or destructive cleanup with unpreserved work | Governing run artifact, UXDD consumer evidence, pre-publication gate evidence; post-publication remote tag, workflow status, release URL, and package smoke |
| MAJOR or breaking release | Approval required | Always | Explicit user approval naming the version |
| Production/customer deployment outside this workstation | Approval required | Always | Named target, deploy plan, health checks |

Project default: `auto_planned_patch_minor_after_uxdd_verified_integration`. A
planned PATCH/MINOR release inside a legitimate run needs no separate user
review, approval, `release`, `go ahead`, or equivalent command. Autonomy begins
only when the governing spec, PRD, ADR, or active run contract contains explicit
release intent and acceptance criteria. The primary release criterion is the
installed consumer journey: every flow claimed as shipped must be
`PRODUCT_WORKS`. Required test protocols, independent review, and mechanical
release checks remain mandatory supporting gates, but none can override a
failed or partial UXDD result. Any failed pre-publication gate blocks tag
creation. Any failed post-publication verification blocks release completion;
a pushed tag enters Recovery After Tag Push. Same-tag publication repair/retry
there remains automatic when the tagged commit and artifacts are unchanged;
new-patch correction also remains automatic unless an existing high-risk
approval trigger applies. Approval is not a substitute for green gates.

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
2. Build and install the release candidate; run the primary UXDD consumer-mode gate through the public package/CLI/MCP entry point; then run all remaining mandatory protocol gates and write evidence paths into the progress report.
3. Open a PR, run MCP PR review, fix or resolve findings, and merge automatically only after the UXDD verdict is `PRODUCT_WORKS` for every claimed journey and all supporting gates are clean.
4. Fast-forward local `main` to `origin/main`.
5. After every pre-publication gate passes, create an annotated tag with
   `git tag -a vX.Y.Z -m "Release vX.Y.Z"` and push it with
   `git push origin vX.Y.Z`.
6. Run all five post-publication verification rows from Required Gates: remote tag
   visibility, exact tag workflow completion, GitHub Release, PyPI publication,
   and Local deploy smoke. Any failed row blocks release completion and the final
   verdict per Terminal Verdict.
7. For the required Local deploy smoke row: install the released wheel/package
   on this workstation, then verify `netcoredbg-mcp --version` reports `X.Y.Z`
   and a package import smoke.
8. Update `.agent/CONTINUITY.md` and the live dashboard with the final verdict.

## Recovery After Tag Push

A pushed release tag is immutable. Never move, delete, or reuse it.

Same-tag publication repair and re-verification for `vX.Y.Z` remain within
PATCH/MINOR release autonomy when the tagged commit and release artifacts are
unchanged. When code, metadata, or artifacts must change, a new-patch correction
also remains automatic: bump to a new PATCH version, rerun every mandatory
pre-publication gate, publish a new annotated tag on the corrected `main`
commit, and run post-publication verification—unless an existing high-risk
approval trigger applies.

| Situation | Action |
| --- | --- |
| Post-publication verification fails; tagged commit and release artifacts are correct | Repair or retry only the failed publication step for the same `vX.Y.Z`, then re-run post-publication verification |
| Code, metadata, or artifacts must change | Create and merge a hotfix PR that bumps to a new patch version, rerun every mandatory pre-publication gate, publish a new annotated tag on the corrected `main` commit, and run post-publication verification for the new version |
| Target tag already exists on origin before creation | Stop; do not overwrite or reuse the existing tag |

## Terminal Verdict

- `PROJECT_RELEASE_PROTOCOL_PASS`: the primary UXDD gate reports
  `PRODUCT_WORKS` for every claimed journey and all supporting mandatory rows
  have current passing evidence.
- `PROJECT_RELEASE_PROTOCOL_BLOCKED`: a claimed journey is `PARTIALLY_WORKS` or
  `BROKEN`, or at least one supporting mandatory row is missing, stale, failed,
  or cannot be verified.
- `PROJECT_RELEASE_PROTOCOL_DRY_RUN`: intended actions are fully described and
  no mutation was performed.

# netcoredbg-mcp Constitution

<!--
Sync Impact Report
Version change: none -> 0.1.0
Changed principles: initial tracked SpecKit rendering of the existing project constitution.
Added sections: Additional constraints; Development workflow.
Removed sections: none.
Templates requiring updates: .specify/templates/spec-template.md, plan-template.md, and tasks-template.md reviewed; no update required because this constitution adds no template fields.
Command files reviewed: .omp/commands/speckit.specify.md, speckit.plan.md, speckit.tasks.md, speckit.analyze.md, and speckit.constitution.md. No command change required.
Follow-up TODOs: none.
-->

## Core Principles

### Evidence before claims
Every completion claim MUST cite current evidence. A behavior change needs a failing and then passing test or a live reproduction. A merge or release claim needs the required gate output or a recorded blocker.

**Rationale:** The project controls real debug sessions and UI evidence. A report or checkpoint does not prove that the user-visible behavior works.

### Reproduce behavior before changing it
A behavior change MUST start with a controlled fixture, smoke scenario, or regression test. If an existing fixture cannot express the behavior, extend the fixture before changing product code.

**Rationale:** Debugging and UI automation failures often have misleading downstream symptoms. A controlled reproduction keeps the repair tied to its cause.

### Keep evidence and secrets fail closed
The project MUST validate paths, classify evidence, bound diagnostic output, and redact credentials. Unsafe, stale, unreadable, or out-of-scope evidence blocks the relevant claim.

**Rationale:** The project handles local sessions and machine evidence. A permissive evidence path can expose data or authorize an unproven release.

### Preserve release and review gates
Tracked source changes MUST use a branch, pull request, independent review, merge readback, and the release protocol when they ship. A failed required gate blocks the associated release claim until the gate passes or an authorized external boundary is recorded.

**Rationale:** Downstream agents need a release that can be installed and run without manual repair.

### Add dependencies deliberately
A source dependency MUST have explicit approval, a current primary-source contract, and a focused test plan before implementation adds it. The plan may name a proposed dependency and the approval it needs.

**Rationale:** New packages change the build, security, and consumer support contract.

### Keep local agent state separate
`.agent` records guide work but do not prove shipped behavior. Tracked source, live GitHub state, exact test output, built artifacts, and runtime observation remain the proof surfaces for a release.

**Rationale:** Local coordination state survives session changes but is not consumer-facing product state.

## Additional constraints

- Operator-facing prose follows the session language. Tracked source, specifications, technical documentation, commits, and pull requests use English unless an existing file establishes another language.
- Prefer an existing test, smoke, schema, or validation pattern over a parallel framework.
- Do not bypass a quality gate with suppression, exclusion, accepted risk, or server configuration changes unless the operator explicitly authorizes that policy change.
- Resolve unexpected tracked dirt before testing, release work, or commits. Preserve unrelated work.

## Development workflow

1. Read the active task, continuity, source state, and current user direction.
2. For a feature or runtime change, identify the relevant contract and use the smallest test or live scenario that proves it.
3. For a changed source branch, obtain independent review before merge.
4. For a release, follow `docs/RELEASE-PROTOCOL.md` and bind every required proof to the exact candidate or merged SHA.
5. Refresh the continuity record after a meaningful product change, merge, release boundary, or blocker.

## Governance

The constitution governs SpecKit artifacts for this repository. Amendments require a Sync Impact Report, a version change, and review of affected templates and workflow commands. A PATCH clarifies existing rules. A MINOR adds or materially expands a rule. A MAJOR removes or weakens a rule.

**Version**: 0.1.0 | **Ratified**: 2026-06-21 | **Last Amended**: 2026-08-24

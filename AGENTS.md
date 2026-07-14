# AGENTS.md — Project Agent Instructions

## 🧠 BASE PRINCIPLE

You are a skeptical expert. Your default mode is to **verify, cross-check, and reason carefully**.

- Never assume the user is right — or that you are
- Treat every claim as a hypothesis to be tested
- Prioritize **accuracy over confidence**, **clarity over speed**, **evidence over assumption**
- Before acting: "What could go wrong? What am I missing?"

---

## 🛑 LANGUAGE

- **Communication with user:** [YOUR LANGUAGE]
- **Everything else:** ENGLISH (code, docs, commits, PRs)

---

## ⛔ CRITICAL RULES

**NON-NEGOTIABLE. Violation = Revert.**

| Rule | ❌ FORBIDDEN | ✅ REQUIRED |
|------|-------------|-------------|
| **No Stubs** | Empty bodies, `NotImplementedException`, `// TODO` | Complete implementations only |
| **No Guessing** | Guess folder/symbol names | Verify with tools before using |
| **No Silent Patching** | Fix without reporting | Report discrepancies |
| **No Checkpoints** | "I'll do this later" | Complete or write blocker |
| **Reasoning-First** | Code without understanding | Document WHY before coding |
| **No Deferring** | Workaround over proper fix | Break down complexity |

---

## ⛔ WORKAROUNDS FORBIDDEN

If solution contains "simple", "quick", "temporary", "workaround" — **STOP and rethink**.

1. **STOP** — No workarounds
2. **ANALYZE** — Find root cause
3. **PROPOSE** — Correct solution
4. **ASK** — If unsure

---

## 🚀 Git & RELEASE WORKFLOW

**NON-NEGOTIABLE. Always follow.**

| Rule | Description |
|------|-------------|
| **No direct commits to main** | All changes via feature branch + PR |
| **Routine release autonomy** | PATCH/MINOR release prep, merge, tag, and publication proceed automatically after the concrete integration scope is complete on `main`, independent review and required checks are clean, every mandatory pre-publication gate in `docs/RELEASE-PROTOCOL.md` passes before annotated tag creation and push, and post-publication verification per that protocol gates release completion. No separate `release`/`go ahead` command is required. |
| **Approval only for high-risk edges** | Explicit user approval is required for MAJOR/breaking releases, production/customer deployment outside this workstation, destructive cleanup with unpreserved work, secrets, or an ambiguous release scope. |
| **Independent PR review required** | Release-owned PRs must receive independent MCP PR review and have no unresolved blocking findings before merge. |
| **Test before release** | Verify functionality works before creating tags. |

**Release process:**
1. When a completed integration scope reaches `main`, evaluate whether it contains unreleased user-visible behavior. If it does and no dependent slice in the same integration wave is still active, start release preparation automatically.
2. Create a release-prep branch (for example, `work/release-v1.0.1-prep`).
3. Update version, changelog, release notes, and other release-owned surfaces; run every mandatory pre-publication gate in `docs/RELEASE-PROTOCOL.md`.
4. Commit, push, and create a release PR.
5. Run independent MCP PR review plus required CI checks; resolve all blocking findings.
6. Merge the PR automatically when review and checks are clean unless a high-risk approval trigger above applies.
7. Update local `main` and verify the merged release commit. For an approved-by-policy PATCH/MINOR scope, create and push the annotated tag automatically, then run post-publication verification per `docs/RELEASE-PROTOCOL.md`. Do not wait for a separate user command.

**If issues are found during verification:** Create a hotfix PR, pass the same independent review and checks, merge it, restart from step 7, and tag only the corrected `main` commit.

---

## 🧾 AGENT-GENERATED WORKTREE TRACES

The repository owner does not manually edit this checkout during agent work.
Unexpected tracked-file dirtiness is therefore agent/tooling residue until
proven otherwise.

**Before source edits, commits, release work, or tests that may regenerate
tracked files:**

1. Run `git status --short --branch`.
2. For every dirty tracked path, capture `git diff -- <path>`.
3. Classify each path:
   - **intended work** — belongs to the current task.
   - **known generated churn** — expected output from a named command/tool.
   - **unknown residue** — not explained by the active task.
   - **blocker** — may affect tests, release, branch switching, or verification.
4. Record command, path, diff summary, and classification in `.agent/CONTINUITY.md`
   when the residue affects future agents.

**Forbidden without explicit user decision:**

- `git restore`, `git checkout --`, `git reset`, `git clean`, stash, or branch
  deletion against unknown residue.
- Committing generated residue just because the worktree is dirty.
- Building, testing, releasing, or opening a PR from a checkout whose dirty
  tracked files are unclassified.
- Building, testing, releasing, or opening a PR from a checkout with
  blocker-classified residue until that residue is resolved, explicitly
  approved by the user, or isolated away from the work in a clean sibling
  worktree.

**Lockfile rule:** `uv.lock` is a reproducibility artifact. Local `uv` commands
can update the editable package's own version entry without a human source edit.
Treat editable-package version churn as tooling residue unless the active task
intentionally changes dependency resolution or package version metadata. Do not
commit or discard it in an unrelated task without an explicit decision.

**Clean implementation rule:** If unknown residue exists on `main`, create a
clean sibling worktree (using `git worktree`) for implementation work and keep
the residue documented in the original checkout.

---

## 📍 KEY PATHS

| What | Where |
|------|-------|
| Epic specs | `.agent/epics/EPIC_XX_*.md` |
| Status | `.agent/CONTINUITY.md` (live session state; `.agent/status/` holds PR-review nitpick JSON only) |
| Reports | `.agent/reports/` |
| Lessons | `.agent/LESSONS_LEARNED.md` |
| Skills | _N/A (no local skills; see `docs/dap-protocol/` for the only project-specific reference)_ |
| Testing | `.agent/guides/TESTING_GUIDELINES.md` |
| Architecture | `.agent/arch/README.md` |

---

## 🧪 TESTING

**Before writing tests, read:** `.agent/guides/TESTING_GUIDELINES.md`

| Rule | Description |
|------|-------------|
| **Unit tests** | Required for ALL new code |
| **Bug fixes** | Regression test FIRST (NON-NEGOTIABLE) |
| **Smoke tests** | Expand `tests/smoke_test_manual.py` when fixing bugs discovered in live usage |
| **Bug → Smoke** | Every bug found during real debugging sessions MUST get a smoke test case |

**Smoke Test Protocol:**
- Current: 87 checks (85 pass, 2 known failures: XPath on WinForms, file dialog)
- Run: `NETCOREDBG_PATH="D:/Bin/netcoredbg/netcoredbg.exe" python tests/smoke_test_manual.py`
- GUI tests require `dotnet build tests/fixtures/SmokeTestApp -c Debug` first
- When fixing a bug: add smoke test BEFORE the fix, verify it fails, then fix, verify it passes

**Test Scratch Hygiene (NON-NEGOTIABLE — learned 2026-07-01):**
- Isolated pytest runs that set `UV_PROJECT_ENVIRONMENT=.agent/tmp/uv-<name>`
  create a full throwaway venv (hundreds of MB each). These are NOT
  auto-removed. Over a multi-CR marathon they silently accumulated
  **13.3 GB / ~498k files** in `.agent/tmp` before the first cleanup.
- Rule: after an isolated `UV_PROJECT_ENVIRONMENT=.agent/tmp/...` run, remove
  that venv dir in the SAME task, OR reuse ONE stable env name across runs
  instead of a fresh per-CR dir. Do not leave per-CR venvs behind.
- `.agent/tmp/` is gitignored scratch: safe to wipe wholesale when idle. On
  Windows, `robocopy <empty-dir> .agent\tmp /MIR /MT:16` clears a large tree in
  seconds; PowerShell `Remove-Item -Recurse` on 100k+ small files times out.
- Periodic check: if `.agent/tmp` exceeds ~1 GB, wipe it during housekeeping.

**Reproduction-First Debugging Protocol:**
- Behavior bugs MUST be reproduced on a controlled test program, fixture, smoke
  scenario, or regression test before product-code edits.
- Preferred fixture surfaces:
  `tests/fixtures/SmokeTestApp`, `tests/fixtures/WpfSmokeApp`,
  `tests/fixtures/AvaloniaSmokeApp`, and `tests/smoke_test_manual.py`.
- If the existing fixtures cannot express the reported behavior, extend the
  fixture first. Do not patch product code around an unmodeled symptom.
- Prove RED on current code, fix the root cause, prove GREEN on the same check,
  then replay the original user-observable scenario or record an explicit
  blocker naming the missing capability.
- `/nvmd-platform:debug --quick` is allowed only for exact file/line errors with
  a <=2-line non-control-flow fix and no plausible competing hypothesis. The
  smallest regression test still belongs with the fix.

**Coverage Targets:** (customize per project)
- Core Domain: 80%
- Critical Paths: 100%

---

## 🎯 SKILLS

Skills are provided by the global `nvmd-platform` plugin and user-scope rules.
This project keeps no local skills — DAP protocol details live as versioned
reference material in [`docs/dap-protocol/`](./docs/dap-protocol/README.md).

> **Note:** `.agent/` (including `CONTINUITY.md`) is gitignored — paths under
> it below are local-only and bootstrapped per clone.

| Task | Source |
|------|--------|
| Coding, refactoring, testing | Global `nvmd-platform` + user rules |
| PR / Integration / Review | Global `nvmd-platform` (`/pr:review`, `/nvmd-platform:pr-reviewer`) |
| Planning / Design | Global `nvmd-platform` (`/nvmd-specify`, `/nvmd-plan`, `/nvmd-tasks`) |
| Debugging | Global `nvmd-platform` |
| After context reset | `.agent/CONTINUITY.md` (local) + global recovery flow |
| DAP wire protocol (project-specific) | [`docs/dap-protocol/`](./docs/dap-protocol/README.md) (versioned mirror of the Microsoft DAP spec) |

---

## 🔧 TOOL PREFERENCES

| Operation | Preferred Tool |
|-----------|----------------|
| File read | MCP or IDE tools |
| File edit | MCP or IDE tools |
| Search | MCP or IDE tools |
| Build/Run | IDE debug configs |
| Code navigation | LSP / Serena |

---

## 📚 LESSONS LEARNED

**File:** `.agent/LESSONS_LEARNED.md`

**When to WRITE:** Bug pattern, debugging insight, architectural lesson, process improvement.

```markdown
### YYYY-MM-DD: Short Title
**Problem:** What went wrong
**Root Cause:** Why it happened
**Lesson:** What to do/avoid next time
```

---

## 📓 Continuity Ledger

Maintain a single `.agent/CONTINUITY.md` (no per-role split).

**Format:**
```markdown
# CONTINUITY — netcoredbg-mcp

## Goal (incl. success criteria)
## Constraints/Assumptions
## Key decisions
## State
### Done
### Now
### Next
## Open questions
## Working set
```

**Rules:**
- Read at session start, update when state changes
- Keep short: facts only, no transcripts
- Mark uncertainty as UNCONFIRMED

---

## 📛 NAMING CONVENTIONS

**Branches:** `work/{type}-{desc}` or `work/epic{N}-{desc}`

**Commits:** `type(scope): description`
- Examples: `feat(core): add feature`, `fix(parser): null check`

**Types:** feat, fix, docs, refactor, test, chore

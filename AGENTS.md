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
| **No releases without approval** | Wait for explicit user "go ahead" before tagging |
| **PR review required** | User must review and approve before merge |
| **Test before release** | Verify functionality works before creating tags |

**Release process:**
1. Create feature branch (e.g., `git checkout -b work/docs-release-workflow`)
2. Make changes, commit (e.g., `git commit -m "docs(workflow): add release process"`)
3. Push and create PR
4. Wait for user review and approval
5. Merge PR into the `main` branch
6. Update local `main` branch:
   - `git checkout main`
   - `git pull origin main`
7. Verify functionality on the updated `main` branch (run tests, manual checks as needed)
8. **Only after user explicitly says "release"** (via direct communication): Create and push an annotated tag (e.g., `git tag -a v1.0.1 -m "Version 1.0.1" && git push origin v1.0.1`)

**If issues are found during verification (step 7):** Create a hotfix PR to address them. After the hotfix PR is merged into the main branch, restart the process from step 6.

---

## 📍 KEY PATHS

| What | Where |
|------|-------|
| Epic specs | `.agent/epics/EPIC_XX_*.md` |
| Status | `.agent/status/CURRENT_STATUS.md` |
| Reports | `.agent/reports/` |
| Lessons | `.agent/LESSONS_LEARNED.md` |
| Skills | `.agent/skills/` |
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

**Coverage Targets:** (customize per project)
- Core Domain: 80%
- Critical Paths: 100%

---

## 🎯 SKILLS

Skills are provided by the global `nvmd-platform` plugin and user-scope rules.
This project keeps **only** project-specific skills locally.

| Task | Source |
|------|--------|
| Coding, refactoring, testing | Global `nvmd-platform` + user rules |
| PR / Integration / Review | Global `nvmd-platform` (`/pr:review`, `/nvmd-platform:pr-reviewer`) |
| Planning / Design | Global `nvmd-platform` (`/nvmd-specify`, `/nvmd-plan`, `/nvmd-tasks`) |
| Debugging | Global `nvmd-platform` |
| After context reset | `.agent/CONTINUITY.md` + global recovery flow |
| DAP wire protocol (project-specific) | `.agent/skills/dap-protocol/SKILL.md` |

The local `dap-protocol` skill is kept because it encodes netcoredbg-specific
Debug Adapter Protocol behavior that is not covered by generic platform skills.

---

## 🎭 ROLES

Role-based CONTINUITY files (`CONTINUITY-CODER.md`, `-DOCWRITER.md`, `-INTEGRATOR.md`)
are **deprecated**. Use a single `.agent/CONTINUITY.md` for session state.

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

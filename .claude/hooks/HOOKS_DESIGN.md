# Claude Code Hooks Design for NovaScript

## Hook Types Available

| Hook | Trigger | Use Case |
|------|---------|----------|
| `UserPromptSubmit` | User sends message | Skill activation, context setup |
| `PreToolUse` | Before tool execution | Validation, guards |
| `PostToolUse` | After tool execution | Verification, reminders |
| `Stop` | Agent stops responding | Cleanup, summaries |
| `SubagentStop` | Subagent completes | Result processing |

---

## Implemented Hooks

### 1. Skill Activation (`UserPromptSubmit`)
**File:** `skill-activation-prompt.js`
**Purpose:** Detect keywords/intents and suggest relevant skills to load

---

## Planned Hooks

### 2. Branch Guard (`PreToolUse`)
**Trigger:** Before `git commit`, `git push`
**Logic:**
```
IF current_branch == "main" OR "master"
  BLOCK with error: "Cannot commit to main. Create feature branch first."
```
**Priority:** CRITICAL

### 3. Epic Context Loader (`UserPromptSubmit`)
**Trigger:** Keywords: "epic", "implement", "task"
**Logic:**
```
IF prompt mentions Epic N
  OUTPUT: "Loading Epic N context..."
  READ: .agent/epics/EPIC_N_*.md
  READ: .agent/status/CURRENT_STATUS.md
```
**Priority:** HIGH

### 4. Pre-Coding Checklist (`PreToolUse`)
**Trigger:** Before `Edit`, `Write` on `*.cs` files
**Logic:**
```
IF first code edit in session
  OUTPUT reminder:
  - [ ] Read Epic spec?
  - [ ] Verified folder structure?
  - [ ] On correct branch?
```
**Priority:** MEDIUM

### 5. Post-Edit Reminder (`PostToolUse`)
**Trigger:** After `Edit`, `Write` on `*.cs`, `*.xaml`
**Logic:**
```
COUNT edits in session
IF edits > 5 AND no build run
  OUTPUT: "Remember to run: dotnet build"
```
**Priority:** MEDIUM

### 6. CodeRabbit Mode (`UserPromptSubmit`)
**Trigger:** Keywords: "PR", "pull request", "coderabbit", "review comments"
**Logic:**
```
OUTPUT: "CodeRabbit workflow detected. Loading guide..."
SUGGEST: Read .agent/guides/CODERABBIT_GUIDE.md
SUGGEST: Use mcp__coderabbitai__* tools
```
**Priority:** HIGH

### 7. Architecture Guard (`PreToolUse`)
**Trigger:** Before `Edit` on `*.csproj`
**Logic:**
```
IF edit changes TargetFramework
  IF new_version != "net6.0*"
    BLOCK: ".NET 6 is required. See G7 rule."
IF edit adds new PackageReference
  OUTPUT warning: "New dependency detected. Verify necessity."
```
**Priority:** CRITICAL

### 8. Context Recovery (`UserPromptSubmit`)
**Trigger:** First message in session OR keywords: "continue", "where was I", "resume"
**Logic:**
```
DETECT current role from context
READ: .agent/CONTINUITY-{ROLE}.md
OUTPUT: "Resuming from last session..."
```
**Priority:** HIGH

### 9. Language Validator (`PostToolUse`)
**Trigger:** After `Write`, `Edit` on `*.md`, `*.cs`
**Logic:**
```
FOR .cs files:
  IF comment contains Cyrillic
    OUTPUT warning: "Code comments should be in English"
FOR .md files in .agent/:
  IF content has Cyrillic (except LESSONS_LEARNED)
    OUTPUT warning: "Documentation should be in English"
```
**Priority:** LOW

### 10. Technical Debt Tracker (`PostToolUse`)
**Trigger:** After `Edit`, `Write` if content contains TODO/FIXME/HACK
**Logic:**
```
IF new TODO/FIXME/HACK added
  OUTPUT: "Technical debt detected. Consider adding to:"
  OUTPUT: ".agent/status/TECHNICAL_DEBT.md"
```
**Priority:** LOW

### 11. Security Scanner (`PreToolUse`)
**Trigger:** Before `git add`, `git commit`
**Logic:**
```
SCAN staged files for:
  - API keys (pattern: sk-*, api_key=*, etc.)
  - Connection strings with passwords
  - Private keys (-----BEGIN PRIVATE)
IF found:
  BLOCK: "Potential secrets detected. Review before commit."
```
**Priority:** CRITICAL

### 12. Protected Files Guard (`PreToolUse`)
**Trigger:** Before `Edit`, `Write` on protected paths
**Protected:**
- `.agent/epics/*` (Coding agents: READ only)
- `.agent/arch/future/*` (Coding agents: FORBIDDEN)
**Logic:**
```
IF role == "Coding Agent" AND path in PROTECTED
  BLOCK: "This file is protected. Only Architect can modify."
```
**Priority:** HIGH

### 13. Build Reminder (`PostToolUse`)
**Trigger:** After significant code changes (>10 edits)
**Logic:**
```
IF session has >10 code edits AND no recent dotnet build
  OUTPUT: "Multiple code changes detected."
  OUTPUT: "Run: dotnet build to verify compilation"
```
**Priority:** MEDIUM

### 14. Documentation Sync (`PostToolUse`)
**Trigger:** After `Edit` on files matching `**/SKILL.md`, `**/AGENTS.md`
**Logic:**
```
IF changed AGENTS.md
  OUTPUT: "AGENTS.md changed. Verify consistency with guides."
IF changed skill SKILL.md
  OUTPUT: "Skill updated. Verify skill-rules.json triggers."
```
**Priority:** LOW

### 15. Worktree Detector (`UserPromptSubmit`)
**Trigger:** Session start
**Logic:**
```
DETECT cwd path:
  IF contains ".worktree/novascript/integrator" → role = Integrator
  IF contains ".worktree/novascript/docwriter" → role = DocWriter
  ELSE → role = Coding Agent or Architect
OUTPUT: "Detected workspace: {role}"
LOAD: appropriate CONTINUITY file
```
**Priority:** HIGH

### 16. Completion Validator (`Stop`)
**Trigger:** When agent is about to stop
**Logic:**
```
IF task was implementation:
  CHECK: All todos marked complete?
  CHECK: Report created?
  CHECK: Branch freed (not on feature branch)?
  IF any missing:
    OUTPUT: "Task incomplete. Missing: ..."
```
**Priority:** HIGH

### 17. Lessons Learned Prompt (`PostToolUse`)
**Trigger:** After fixing bug or resolving complex issue
**Logic:**
```
IF conversation contains "bug", "fix", "issue", "root cause"
  AND resolution appears complete
  OUTPUT: "Consider documenting in LESSONS_LEARNED.md"
```
**Priority:** LOW

### 18. MCP Tool Preference (`PreToolUse`)
**Trigger:** Before `Bash` with grep/find/sed/cat
**Logic:**
```
IF bash command is "grep ..." → SUGGEST: Use Grep tool
IF bash command is "find ..." → SUGGEST: Use Glob tool
IF bash command is "cat ..." → SUGGEST: Use Read tool
IF bash command is "gh ..." → SUGGEST: Use mcp__github__* tools
```
**Priority:** MEDIUM

### 19. Commit Message Validator (`PreToolUse`)
**Trigger:** Before `git commit`
**Logic:**
```
PARSE commit message
IF not matches "type(scope): description"
  OUTPUT warning: "Commit message should follow convention"
  EXAMPLE: "feat(epic40): add user authentication"
```
**Priority:** LOW

### 20. Epic Report Reminder (`Stop`)
**Trigger:** Session end after implementation work
**Logic:**
```
IF session had significant code changes
  AND no report file created in .agent/reports/
  OUTPUT: "Don't forget to create Epic report:"
  OUTPUT: ".agent/reports/EPIC_N_REPORT.md"
```
**Priority:** MEDIUM

---

## Implementation Priority

### Phase 1 (Critical)
1. ✅ Skill Activation (done)
2. Branch Guard
3. Architecture Guard
4. Security Scanner

### Phase 2 (High)
5. Epic Context Loader
6. CodeRabbit Mode
7. Context Recovery
8. Worktree Detector
9. Protected Files Guard
10. Completion Validator

### Phase 3 (Medium)
11. Pre-Coding Checklist
12. Post-Edit Reminder
13. Build Reminder
14. MCP Tool Preference
15. Epic Report Reminder

### Phase 4 (Low)
16. Language Validator
17. Technical Debt Tracker
18. Documentation Sync
19. Commit Message Validator
20. Lessons Learned Prompt

---

## Technical Architecture

```
.claude/hooks/
├── skill-activation-prompt.js    # UserPromptSubmit
├── branch-guard.js               # PreToolUse (git)
├── architecture-guard.js         # PreToolUse (Edit .csproj)
├── security-scanner.js           # PreToolUse (git add/commit)
├── context-loader.js             # UserPromptSubmit
├── completion-validator.js       # Stop
└── shared/
    ├── utils.js                  # Common utilities
    ├── patterns.js               # Regex patterns
    └── config.json               # Hook configuration
```

## Hook Response Format

```json
{
  "result": "block" | "warn" | "info" | "silent",
  "message": "Human-readable message",
  "action": "suggest_skill" | "remind" | "block_action",
  "data": { ... }
}
```

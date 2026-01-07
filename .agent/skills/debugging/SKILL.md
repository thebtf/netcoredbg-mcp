# Debugging Skill

## Activation
This skill activates when: investigating bugs, errors, crashes, unexpected behavior.

## Workflow

### Phase 1: Reproduction
1. Understand the reported issue
2. Identify reproduction steps
3. Reproduce the bug locally
4. Document exact error/behavior

### Phase 2: Investigation
1. Gather evidence (logs, stack traces)
2. Form hypothesis about root cause
3. Narrow down location (binary search)
4. Verify hypothesis with tests

### Phase 3: Root Cause Analysis
1. Identify the actual bug (not symptom)
2. Understand WHY it happens
3. Check for similar issues elsewhere
4. Document root cause

### Phase 4: Fix
1. Write regression test FIRST (NON-NEGOTIABLE)
2. Implement minimal fix
3. Verify regression test passes
4. Run full test suite

### Phase 5: Completion
1. Document fix in commit message
2. Update LESSONS_LEARNED
3. Check for related issues
4. Update CONTINUITY

## Checklist
- [ ] Bug reproduced locally
- [ ] Root cause identified (not just symptom)
- [ ] Regression test written FIRST
- [ ] Fix implemented
- [ ] All tests pass
- [ ] LESSONS_LEARNED updated
- [ ] Committed with proper message

## Common Mistakes
- **Fixing symptoms:** Find the ROOT cause
- **No regression test:** ALWAYS write test first
- **Rushing:** Understand before fixing
- **Not documenting:** Others will encounter same bug

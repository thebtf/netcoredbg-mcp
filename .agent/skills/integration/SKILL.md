# Integration Skill

## Activation
This skill activates when: creating PRs, reviewing code, merging changes, handling CodeRabbit.

## Workflow

### Phase 1: Pre-PR Checklist
1. All tests pass
2. Code reviewed (self-review)
3. No debug code left
4. Commit messages are clean
5. Branch is up to date with target

### Phase 2: PR Creation
1. Create PR with descriptive title
2. Write clear description:
   - What changed
   - Why it changed
   - How to test
3. Link related issues/epics
4. Request reviewers if needed

### Phase 3: Review Handling
1. Monitor for review comments
2. Address CodeRabbit suggestions
3. Respond to reviewer feedback
4. Push fixes as new commits

### Phase 4: Merge
1. Ensure all checks pass
2. Resolve any conflicts
3. Get required approvals
4. Squash/merge per project convention
5. Delete branch after merge

### Phase 5: Post-Merge
1. Verify deployment (if applicable)
2. Update related issues
3. Notify stakeholders
4. Clean up local branches

## PR Description Template
```markdown
## Summary
Brief description of changes

## Changes
- Change 1
- Change 2

## Testing
How to verify these changes

## Related
- Closes #issue
- Epic: XX
```

## Checklist
- [ ] All tests pass
- [ ] Self-reviewed code
- [ ] PR description complete
- [ ] CodeRabbit comments addressed
- [ ] Conflicts resolved
- [ ] Approvals obtained
- [ ] Merged successfully

## Common Mistakes
- **Merging with failing tests:** Never merge red CI
- **Ignoring review comments:** Address all feedback
- **Poor PR description:** Be thorough
- **Not cleaning up:** Delete merged branches

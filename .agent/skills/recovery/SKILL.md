# Recovery Skill

## Activation
This skill activates when: context lost, session reset, resuming work, "where were we?"

## Workflow

### Phase 1: Read State
1. Read `.agent/CONTINUITY-CODER.md` (or appropriate role)
2. Note: Goal, Done, Now, Next sections
3. Check for UNCONFIRMED items
4. Identify working set (files/branches)

### Phase 2: Verify State
1. Check git status and current branch
2. Verify working set files exist
3. Review recent commits
4. Confirm continuity accuracy

### Phase 3: Resume
1. Summarize current state to user
2. Confirm next steps are still valid
3. Clarify any UNCONFIRMED items
4. Continue from "Now" section

### Phase 4: Update
1. Correct any outdated info in continuity
2. Remove completed items from "Now"
3. Update working set if changed
4. Mark confirmed items

## Checklist
- [ ] Read CONTINUITY file
- [ ] Verified git state
- [ ] Confirmed working set
- [ ] Summarized state to user
- [ ] Clarified uncertainties
- [ ] Updated CONTINUITY if needed

## Common Mistakes
- **Assuming continuity is correct:** Always verify
- **Not confirming with user:** Ask if state unclear
- **Starting fresh:** Use continuity, don't restart
- **Not updating:** Fix outdated info immediately

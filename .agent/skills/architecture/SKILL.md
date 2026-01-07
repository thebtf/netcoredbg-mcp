# Architecture Skill

## Activation
This skill activates when: designing systems, planning features, creating specifications, making architectural decisions.

## Workflow

### Phase 1: Context Gathering
1. Understand the problem domain
2. Review existing architecture
3. Identify constraints and requirements
4. Check for related ADRs

### Phase 2: Analysis
1. Identify components involved
2. Map dependencies
3. List options/alternatives
4. Evaluate trade-offs

### Phase 3: Design
1. Create high-level design
2. Define interfaces/contracts
3. Document decision rationale
4. Create ADR if significant decision

### Phase 4: Specification
1. Write detailed spec if needed
2. Define acceptance criteria
3. Identify risks and mitigations
4. Plan implementation phases

### Phase 5: Review
1. Validate against requirements
2. Check for overlooked concerns
3. Get feedback if possible
4. Update arch documentation

## ADR Template Location
`.agent/arch/decisions/README.md`

## Checklist
- [ ] Problem clearly defined
- [ ] Constraints identified
- [ ] Options evaluated
- [ ] Trade-offs documented
- [ ] ADR created (if needed)
- [ ] Spec written (if complex)
- [ ] Implementation plan outlined

## Common Mistakes
- **Premature optimization:** Solve the actual problem first
- **Over-engineering:** Simplest solution that works
- **Missing constraints:** Check all requirements
- **No documentation:** Decisions must be recorded

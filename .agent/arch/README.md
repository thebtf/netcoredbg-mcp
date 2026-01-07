# Architecture Documentation

> Navigation hub for architectural documentation.

## Quick Links

| Document | Purpose |
|----------|---------|
| [ADR Index](decisions/README.md) | Architecture Decision Records |
| [Specs](SPEC_PROPOSAL/README.md) | Detailed specifications |

## Documentation Hierarchy

```
Level 1: This README (Navigation)
    ↓
Level 2: ADRs (decisions/)
    ↓
Level 3: Specifications (SPEC_PROPOSAL/)
    ↓
Level 4: Implementation guides
```

## When to Create

### ADR (Architecture Decision Record)
- Choosing between multiple technical options
- Decision has long-term implications
- Decision affects multiple components
- Takes >15 minutes to decide

### Specification
- Complex feature requiring detailed design
- Multiple components affected
- External dependencies involved
- Needs review before implementation

## File Naming

- ADRs: `ADR-NNN-short-title.md`
- Specs: `SPEC_NN_ShortTitle.md`

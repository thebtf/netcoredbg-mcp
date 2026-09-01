# Specification Quality Checklist: Sonar Runtime-Smoke Column Predicate

**Purpose:** Requirements-quality review of this D1 packet. This is not an implementation test, source acceptance receipt, Sonar scan, build, release, or PR action.
**Feature:** [spec.md](../spec.md)

## Recheck — 15/15 complete

- [x] **Q01** The packet identifies D1 as appropriate for one reversible expression-only refactor and excludes D2/D3 artifacts.
- [x] **Q02** The bounded outcome names one Sonar issue, one source module, and one nested conditional.
- [x] **Q03** The sanitized evidence identifies only issue ID, rule, message, component, line, analysis, and head.
- [x] **Q04** The P1 maintainer scenario states the observable validation outcome without inventing a user or API.
- [x] **Q05** Acceptance scenarios cover invalid non-string values, omitted columns, non-affected operations, and order.
- [x] **Q06** S1066-001 through S1066-005 are observable and mapped to source or proof work.
- [x] **Q07** The preservation contract fixes exact three-operation membership, O → C → ¬T order, and diagnostic text.
- [x] **Q08** The packet excludes broad-grid merging, helper extraction, test changes, suppression, scanner work, and release work.
- [x] **Q09** [data-model.md](../data-model.md) models only the current in-memory predicate relationship and disclaims persistence/public API creation.
- [x] **Q10** [research.md](../research.md) records Challenge-LITE GO and rejects retaining nesting, a helper, and a broader merge.
- [x] **Q11** [plan.md](../plan.md) names boundaries, integration points, no-change witnesses, rollback, and exactly one independent checker.
- [x] **Q12** [tasks.md](../tasks.md) is dependency ordered, confines source work to the named module, and leaves external proof unchecked.
- [x] **Q13** [quickstart.md](../quickstart.md) gives the exact existing three-node pytest command without claiming execution.
- [x] **Q14** Internal links connect specification, research, model, plan, tasks, quickstart, checklist, and evidence.
- [x] **Q15** The packet states that the maker neither executes tests nor self-certifies acceptance.

**Recheck note:** This checklist confirms the planning text only. Parent-run focused proof and the single independent checker commitment remain outstanding.

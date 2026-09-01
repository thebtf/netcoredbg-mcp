---
description: "Dependency-ordered D1 tasks for the Sonar S1066 runtime-smoke column predicate"
---

# Tasks: Sonar Runtime-Smoke Column Predicate

**Input:** [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [plan.md](plan.md), [quickstart.md](quickstart.md), and [checklists/requirements.md](checklists/requirements.md).  
**Source base:** `c64218f6a988309c4fa9d676e6cede3a89097411`  
**Release intent:** `none`

## Dependency order

- [x] **T000 [Packet / Challenge-LITE]** Author the seven D1 documents and sanitized one-finding evidence. Record the preferred conjunctive predicate; reject retaining nesting, helper extraction, and broader-grid merge. **Acceptance:** the packet remains bounded to one finding and one source conditional.
- [x] **T001 [Code] [S1066-001 through S1066-004]** Modify only `src/netcoredbg_mcp/session/runtime_smoke_schema.py` to merge the affected nested condition into an O → C → ¬T predicate and retain the append string unchanged. **Depends on:** T000. **Acceptance:** the source diff changes no other condition or public identifier.
- [ ] **T002 [Focused proof] [S1066-002, S1066-003]** Parent runs the exact three-node command in [quickstart.md](quickstart.md) against the committed candidate. **Depends on:** T001. **Acceptance:** three named test nodes execute and pass; no broader command substitutes for this proof.
- [ ] **T003 [Independent checker] [S1066-001 through S1066-005]** One checker who did not perform T001 reads the changed predicate and this packet, confirms the source invariant, and runs T002’s exact command. **Depends on:** T002. **Acceptance:** the checker reports a concrete unmet point or focused-proof result; it does not authorize release.

## Requirement coverage

| Requirement | Tasks | Exact path |
|---|---|---|
| S1066-001 | T001, T003 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` |
| S1066-002 | T001, T002, T003 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; `tests/test_runtime_smoke_schema.py` |
| S1066-003 | T001, T002, T003 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; `tests/test_runtime_smoke_schema.py` |
| S1066-004 | T001, T003 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` |
| S1066-005 | T002, T003 | `tests/test_runtime_smoke_schema.py` |

T002 and T003 remain deliberately unchecked: this maker was explicitly assigned not to execute tests or self-certify acceptance.

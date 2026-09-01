---
description: "Dependency-ordered D1 tasks for behavior-preserving runtime-smoke schema literal reuse"
---

# Tasks: Sonar Runtime-Smoke Schema Literals

**Input:** [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [plan.md](plan.md), [quickstart.md](quickstart.md), and [checklists/requirements.md](checklists/requirements.md).
**Source base:** `2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa`
**Release intent:** `none`
**Execution rule:** all tasks remain unchecked until their named prerequisite is complete. Packet authoring changes no product source or test.

## Dependency order

- [X] **T000 [D1-LITE / Analyze]** Review the exact packet paths `specs/011-sonar-schema-literals/{spec.md,research.md,data-model.md,plan.md,quickstart.md,tasks.md,checklists/requirements.md}` with the parent-run D1-LITE challenger and analyze stage. **Evidence:** the D1-LITE challenger returned `GO`; the independent semantic analyzer returned `PASS` for this exact base. Do not edit product source or tests. **Depends on:** packet authoring. **Acceptance:** the parent resolves the premise and alternatives without expanding scope.
- [X] **T001 [Test] [SML-001, SML-003, SML-004]** In `tests/test_runtime_smoke_schema.py`, add `test_runtime_smoke_schema_preserves_public_operation_identifiers` before any extraction. Characterize all six values through `schema_help_fields`, `validate_plan`, `normalize_plan_step`, and `RuntimeSmokeRunner` adapter dispatch. **Depends on:** T000. **Acceptance:** the test encodes byte-exact public names and the current behavioral route; it does not require a planned private constant name. **Implementation status:** Test implementation is complete; focused proof remains T003.
- [X] **T002 [Code] [SML-001, SML-002, SML-003, SML-005]** Modify only `src/netcoredbg_mcp/session/runtime_smoke_schema.py`. Introduce six module-private constants and replace the selected duplicate occurrence groups in the operation definitions and `_validate_op_args` without changing value bytes, tuple order, required fields, aliases, validation flow, or normalized names. **Depends on:** T001. **Acceptance:** no public or cross-module API is added, and no no-change witness is edited. **Implementation status:** Source implementation is complete; focused proof remains T003.
- [X] **T003 [Verify] [SML-001 through SML-005]** Run only the two focused commands in [quickstart.md](quickstart.md) against the implementation candidate: the new characterization case and `test_legacy_runtime_smoke_grid_state_actions_reach_adapters`. **Depends on:** T002. **Acceptance:** both commands execute their named nonzero test case; no build, formatter, linter, Sonar scan, or project-wide suite substitutes for this proof.
- [ ] **T004 [Independent checker] [SML-001 through SML-005]** A single checker who did not perform T002 reads `src/netcoredbg_mcp/session/runtime_smoke_schema.py`, `tests/test_runtime_smoke_schema.py`, and the exact packet paths, re-derives byte preservation through the named no-change witnesses, and reruns the two [quickstart.md](quickstart.md) commands on the exact candidate. **Depends on:** T003. **Acceptance:** the one checker either identifies a concrete unmet contract point or confirms the focused behavior on that candidate; it does not authorize a release.

## Requirement coverage

| Requirement | Tasks | Exact later path |
|---|---|---|
| SML-001 | T001, T002, T003, T004 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; `tests/test_runtime_smoke_schema.py` |
| SML-002 | T002, T003, T004 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` |
| SML-003 | T001, T002, T003, T004 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; `tests/test_runtime_smoke_schema.py` |
| SML-004 | T001, T003, T004 | `tests/test_runtime_smoke_schema.py` |
| SML-005 | T002, T003, T004 | `src/netcoredbg_mcp/session/runtime_smoke.py`; `src/netcoredbg_mcp/tools/runtime_smoke.py`; `src/netcoredbg_mcp/session/runtime_smoke_operations.py`; `src/netcoredbg_mcp/session/runtime_smoke_v2/actions/__init__.py`; `src/netcoredbg_mcp/ui/grid.py` |

T001 through T004 are serial because behavior characterization must precede extraction, focused verification depends on the source cut, and the exact-candidate independent check comes last. No task creates a release artifact, changes raw Sonar evidence, or broadens the six-key scope.

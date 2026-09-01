# Quickstart: Future Verification of Runtime-Smoke Schema Literals

**Status:** Future focused-verification guide. No command in this file was run while authoring this packet.
**Spec:** [spec.md](spec.md)
**Tasks:** [tasks.md](tasks.md)
**Release intent:** `none`

## Prerequisites

1. Start from the exact source base `2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa` or its later implementation candidate.
2. The parent has completed the D1-LITE challenge and analyze handoff described in [plan.md](plan.md); this guide does not replace those stages.
3. Preserve the losslessly normalized current Sonar API inventory at [evidence/sonar-issues-2ff8607.json](evidence/sonar-issues-2ff8607.json), SHA-256 `7e32d9e6955397517c51aae338b2bf45298cfa521962f1268a8f09d9e30fcd8b`.
4. T001 has added the behavior-characterization test before T002 changes `src/netcoredbg_mcp/session/runtime_smoke_schema.py`.
5. Do not modify the no-change witnesses named in [plan.md](plan.md), Sonar policy/configuration, public operation names, or a public schema/API surface.

## Run the focused behavior proof

After T001 and T002, run the planned new characterization case:

```powershell
uv run pytest tests/test_runtime_smoke_schema.py::test_runtime_smoke_schema_preserves_public_operation_identifiers -q
```

Then run the retained focused adapter-dispatch case:

```powershell
uv run pytest tests/test_runtime_smoke_schema.py::test_legacy_runtime_smoke_grid_state_actions_reach_adapters -q
```

## Observe these behaviors

- The six exact public values appear in the accepted-operation catalog and associated help fields with their established ordering behavior.
- A valid plan for each value remains accepted by `validate_plan()` and normalizes to the same operation name.
- A configured `RuntimeSmokeRunner` reaches the existing adapter key for each value.
- Existing row-based grid dispatch continues to reach its existing adapter calls.
- No code under `session/runtime_smoke.py`, `tools/runtime_smoke.py`, `session/runtime_smoke_operations.py`, `session/runtime_smoke_v2/actions/__init__.py`, or `ui/grid.py` changes to make the proof pass.

## Scope boundary

This guide intentionally names only focused test commands. It does not call a Sonar API, scanner, build, formatter, linter, release workflow, PR operation, or project-wide validation. A later result is accepted only when the single independent checker commitment in [plan.md](plan.md) is also discharged against the exact candidate.

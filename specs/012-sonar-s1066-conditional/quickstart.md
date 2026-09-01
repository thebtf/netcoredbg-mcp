# Quickstart: Future Verification of Runtime-Smoke Column Predicate

**Status:** Future focused-verification guide. The implementation maker did not run this command.
**Spec:** [spec.md](spec.md)  
**Tasks:** [tasks.md](tasks.md)  
**Release intent:** `none`

## Prerequisites

1. Start at the committed candidate descended from `c64218f6a988309c4fa9d676e6cede3a89097411`.
2. Confirm that only this packet and `src/netcoredbg_mcp/session/runtime_smoke_schema.py` changed.
3. Do not modify the broader grid branch, public operation identifiers, tests, Sonar configuration, or release surfaces to make the proof pass.

## Run the focused behavior proof

From the candidate root, run exactly:

```powershell
uv run pytest tests/test_runtime_smoke_schema.py::test_legacy_runtime_smoke_grid_state_actions_reach_adapters tests/test_runtime_smoke_schema.py::test_runtime_smoke_schema_preserves_public_operation_identifiers tests/test_runtime_smoke_schema.py::test_legacy_runtime_smoke_grid_state_actions_validate_arguments -q
```

## Observe these behaviors

- Valid click, right-click, and double-click row operations still dispatch through their existing adapters.
- Public operation identifiers remain accepted and unchanged.
- Invalid non-string `column` values produce the existing exact diagnostics for all three affected operations.
- The checker’s source inspection confirms O → C → ¬T and the unchanged append string.

## Scope boundary

This command is the named focused proof only. Do not substitute a project-wide suite, build, formatter, linter, Sonar scan, release workflow, push, or PR action. A passing command is evidence for this slice only and does not authorize a release.

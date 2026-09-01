# Implementation Plan: Sonar Runtime-Smoke Schema Literals

**Spec:** [spec.md](spec.md)
**Design depth:** D1
**Source base:** `2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa`
**Evidence:** [sonar-issues-2ff8607.json](evidence/sonar-issues-2ff8607.json), required SHA-256 `7e32d9e6955397517c51aae338b2bf45298cfa521962f1268a8f09d9e30fcd8b`
**Release intent:** `none`
**Execution status:** Planning packet only. No product source or test implementation is performed by this packet.

## D1 boundary contract

| Boundary | Contract |
|---|---|
| **Inputs** | The six existing public identifiers; `_OPERATION_SCHEMA_DEFINITIONS`; existing validation argument groups; the selected Sonar issue IDs. |
| **Output** | The same `OPERATION_SCHEMAS` keys, schema-help catalog, validation result, normalized operation name, and adapter-dispatch name, sourced from six module-private constants. |
| **Attaches to** | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` only: operation definitions and the existing `_validate_op_args()` groups that contain the selected duplicates. |
| **Does not touch** | Public runner/tool serializer modules, adapter-map modules, UI helpers, schema versions, public API names, persistence, Sonar policy/configuration, the losslessly normalized current Sonar API inventory, or any source/test path during packet authoring. |

## Integration points and no-change witnesses

| Integration point | Existing relationship | Planned effect |
|---|---|---|
| `OPERATION_SCHEMAS` | Comprehends the operation-definition tuples into the accepted public catalog. | Same keys, internal names, and required fields; private constants only. |
| `schema_help_fields` | Sorts `OPERATION_SCHEMAS` into public accepted names, aliases, and required fields. | No code change; its output is a preservation oracle. |
| `normalize_plan_step` | Resolves a public `op` to the schema's existing internal name. | No semantic change; characterization covers it. |
| `_validate_step` | Looks up accepted public operations and applies required-field checks. | No semantic change; characterization covers it. |
| `_validate_op_args` | Applies operation-specific argument validation groups containing the selected duplicate values. | Replace only its selected literals with matching private constants. |
| `session/runtime_smoke.py` | Validates, emits schema help, normalizes planned steps, and dispatches adapters. | No-change public-runner witness. |
| `tools/runtime_smoke.py` | Serializes validation/help data through public runtime-smoke tool surfaces. | No-change public-tool witness. |
| `session/runtime_smoke_operations.py` | Binds the existing operation strings to adapter closures and the adapter map. | No-change adapter-map witness. |
| `session/runtime_smoke_v2/actions/__init__.py` | Retains v2 safe-action and registered-action spellings. | No-change adapter-map witness. |
| `ui/grid.py` | Uses the existing grid adapter identifiers in helper diagnostics and behavior. | No-change UI-helper witness. |

The only later mutation paths are `src/netcoredbg_mcp/session/runtime_smoke_schema.py` and an intentionally added behavior test in `tests/test_runtime_smoke_schema.py`. All other named integration points are witnesses, not edit targets.

## Requirements-to-file map

| Requirement | Planned implementation file | Planned proof file | Packet authority |
|---|---|---|---|
| SML-001, SML-002 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` | `tests/test_runtime_smoke_schema.py` | [spec.md](spec.md) and [research.md](research.md) |
| SML-003 | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` | `tests/test_runtime_smoke_schema.py` | [data-model.md](data-model.md) |
| SML-004 | none before characterization | `tests/test_runtime_smoke_schema.py` | [tasks.md](tasks.md) |
| SML-005 | no source edit | Existing public runner/tool/adapter witnesses | This plan and [quickstart.md](quickstart.md) |

## Test plan

1. **Characterize before extraction.** Add `test_runtime_smoke_schema_preserves_public_operation_identifiers` in `tests/test_runtime_smoke_schema.py` before changing source. It must enumerate all six values and prove catalog/help presence, successful validation, unchanged normalization, and adapter dispatch with the existing public names.
2. **Extract privately.** Modify only `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; define the six private constants and replace each selected duplicate use without changing tuple order, required fields, or public values.
3. **Run focused proof later.** Run the new characterization test and the existing `test_legacy_runtime_smoke_grid_state_actions_reach_adapters` case with the commands in [quickstart.md](quickstart.md). The packet makes no claim that either command has run.

## Rollback

No data migration or public cutover exists. If the later extracted implementation violates the characterization contract, restore only the source expressions in `runtime_smoke_schema.py` to the exact prior literals and retain the characterization test as the regression guard. Do not add a suppression, fallback alias, cross-module registry, or public API change as rollback.

## D1-LITE challenge and analyze receipt

The parent completed the required D1-LITE plan challenge before implementation: its verdict was `GO` for module-private constants over suppression or cross-module/public registry expansion. An independent semantic SpecKit analysis of this exact packet returned `PASS`, including evidence identity, six issue ID scope, source/test-path coverage, strict task ordering, and D1 ceiling checks. These are planning receipts only; they do not claim source/test execution or release readiness.


## Exactly one future independent checker

After the focused proof exists on an implementation candidate, **one independent checker who did not make the source extraction** re-derives SML-001 through SML-005 against the exact candidate: it reads the target source and new behavior test, confirms all six public values remain byte-identical through the named witnesses, and reruns the two focused commands from [quickstart.md](quickstart.md). This is the packet's only implementation-acceptance checker commitment; no second independent acceptance pass is planned.

## Local capability gap

The local `.specify` helper scripts and templates are absent. The required planning artifacts are therefore manual documents; no helper was restored, added, or run.

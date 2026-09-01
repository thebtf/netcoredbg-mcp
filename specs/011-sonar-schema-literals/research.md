# Research: Sonar Runtime-Smoke Schema Literals

**Status:** Planning research only. It records no source/test implementation, command execution, Sonar rerun, resolved issue, or release result.
**Source base:** `2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa`
**Evidence input:** [evidence/sonar-issues-2ff8607.json](evidence/sonar-issues-2ff8607.json), required SHA-256 `7e32d9e6955397517c51aae338b2bf45298cfa521962f1268a8f09d9e30fcd8b`.

## Evidence labels

- **OBSERVED**: read from the losslessly normalized current Sonar API inventory or the stated source base.
- **SELECTED**: the bounded implementation shape this packet requires.
- **INFERRED**: a later implementation-phase behavior that focused proof must establish.

## Exact Sonar API binding and query

| Item | Captured value |
|---|---|
| Project | `thebtf_netcoredbg_mcp` |
| Analysis ID | `a76d9b95-c155-4102-abf1-472108b3e070` |
| Head / revision | `2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa` |
| Current-binding probe | `project=thebtf_netcoredbg_mcp`, `p=1`, `ps=1`, with `current=true` |
| Issue endpoint | `/api/issues/search` |
| Captured issue-query parameters | `components=thebtf_netcoredbg_mcp`; `issueStatuses=OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX` |
| Inventory facts | `api_total=1202`; `open_blocking_count=868` |
| Selected rule and component | `python:S1192`; `thebtf_netcoredbg_mcp:src/netcoredbg_mcp/session/runtime_smoke_schema.py` |

The selected packet narrows that captured inventory to exactly these six open code-smell records:

| Public identifier | Issue ID | Primary declaration line | Reported duplicate uses |
|---|---|---:|---:|
| `ui.grid.ensure_visible` | `b8bc779d-ab69-4720-ab22-3ad9d782798f` | 124 | 4 |
| `ui.grid.select_row` | `892d5621-f08f-42f9-aa24-234f85b8c98f` | 138 | 4 |
| `ui.grid.click_row` | `c333e1f8-736f-437b-b716-f240cdbbe5ce` | 139 | 5 |
| `ui.grid.right_click_row` | `9c32cc3a-0e1e-427e-ab07-7a475a2cd5ee` | 141 | 5 |
| `ui.grid.double_click_row` | `83672283-15b8-4cb2-a930-1f65329acf5e` | 146 | 5 |
| `ui.list.toggle_item_child` | `f694658e-39ae-442a-a811-b5ded78f423e` | 157 | 3 |

No live API request is part of packet authoring. The losslessly normalized current Sonar API inventory is the only API evidence used here.

## Source-boundary facts

| Classification | Observation | Consequence |
|---|---|---|
| **OBSERVED** | `OperationSchema` is the existing immutable value shape; `_OPERATION_SCHEMA_DEFINITIONS` builds `OPERATION_SCHEMAS` in `runtime_smoke_schema.py`. | The six values belong as private reuse values in this module, not in a new schema component. |
| **OBSERVED** | `schema_help_fields()` exposes sorted operation names, aliases, and required fields from `OPERATION_SCHEMAS`. | The catalog and diagnostic serialization are public preservation surfaces. |
| **OBSERVED** | `validate_plan()` reaches `_validate_step()` and `_validate_op_args()`; `normalize_plan_step()` resolves public `op` names through `OPERATION_SCHEMAS`. | Replacing literals must preserve acceptance, error behavior, and normalized names. |
| **OBSERVED** | `RuntimeSmokeRunner.run()` validates plans and emits schema help on invalid plans; `_planned_steps()` and `_step()` call the normalizer; `_execute_operation()` dispatches normalized names. | `session/runtime_smoke.py` is a no-change public-runner witness. |
| **OBSERVED** | `validate_runtime_smoke_plan_contract()` and public runtime-smoke tools serialize validation and schema-help output. | `tools/runtime_smoke.py` is a no-change public-tool witness. |
| **OBSERVED** | `ui_operation_adapters`, v2 action registrations/safe-action sets, and grid helpers retain the same strings at execution boundaries. | `session/runtime_smoke_operations.py`, `session/runtime_smoke_v2/actions/__init__.py`, and `ui/grid.py` are no-change adapter witnesses. |

## Decision

**SELECTED: six module-private constants in `src/netcoredbg_mcp/session/runtime_smoke_schema.py`.** Each constant holds one existing public value and is reused in the selected definition and validation sites. This resolves the duplicate-literal maintenance finding without moving any ownership boundary or changing a public string.

**Rationale:** all selected duplicate sites already live in one module, and the operation mapping, help payload, validation, normalization, runner, tool, and adapter maps consume the existing exact strings. Module-private reuse is the smallest change that preserves every observed consumer relationship.

## Alternatives considered

| Alternative | Disposition | Reason |
|---|---|---|
| Six module-private constants in `runtime_smoke_schema.py` | **Chosen** | Removes the selected duplicate literals while preserving the existing module and public values. |
| Suppress `python:S1192` | **Rejected** | Leaves the repeated values and records an exception instead of the requested maintainability remediation. |
| Introduce a cross-module registry or public schema/API change | **Rejected** | Adds coupling and public contract surface despite all selected uses already sharing one private module. |

## Local capability gap

**OBSERVED:** the local `.specify` directory contains only the active feature pointer; helper scripts and templates are absent. Therefore the `spec`, clarification record, plan, checklist, tasks, and future-verification guide are authored manually. No SpecKit automation is restored, added, or claimed to have run.

## Research conclusion

The current evidence and source boundaries support one D1 internal extraction. The later maker must first characterize the existing six-value behavior in `tests/test_runtime_smoke_schema.py`, then modify only `runtime_smoke_schema.py`, then use the focused proof in [quickstart.md](quickstart.md). The evidence does not authorize a broader Sonar remediation or a public schema redesign.

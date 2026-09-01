---
title: "Sonar Runtime-Smoke Schema Literals"
feature: "011-sonar-schema-literals"
design_depth: "D1"
status: "planned"
source_base: "2ff86074a3d1501afc8ed5ddcaa7d23c44cf1bfa"
release_intent: "none"
evidence_sha256: "7e32d9e6955397517c51aae338b2bf45298cfa521962f1268a8f09d9e30fcd8b"
---

# Feature Specification: Sonar Runtime-Smoke Schema Literals

## Bounded outcome

Replace the module-local duplicate uses that back six API-backed `python:S1192` records in `src/netcoredbg_mcp/session/runtime_smoke_schema.py` with six module-private constants. The public operation identifiers remain byte-identical:

- `ui.grid.ensure_visible`
- `ui.grid.select_row`
- `ui.grid.click_row`
- `ui.grid.right_click_row`
- `ui.grid.double_click_row`
- `ui.list.toggle_item_child`

This is a reversible internal refactor. It changes neither the public runtime-smoke plan vocabulary nor any public tool, serializer, adapter map, schema version, persistence model, or release behavior.

## D1 calibration

**Bound unit:** this one-file schema-literal refactor packet, not the broader Sonar remediation program. If wrong, maintainers can author or validate runtime-smoke plans against changed operation names; the contract lives with the existing schema feature and is consumed by a later maker and one checker. The boundary is small and reversible, so D1 is the appropriate depth. This packet intentionally contains no ADR, subsystem design, milestone map, tracer decomposition, or external contracts directory.

## P1 maintainer scenario

A maintainer validates or runs an existing runtime-smoke plan that uses any of the six identifiers. After the private constant extraction, the accepted catalog, validation result, normalized operation name, and adapter dispatch name remain exactly the same as before.

### Acceptance scenarios

1. **Given** a v1 plan that names one of the six public `op` values with its existing required arguments, **when** `validate_plan()` and `normalize_plan_step()` process it, **then** it remains accepted and normalizes to the same byte-identical operation name.
2. **Given** invalid-plan diagnostics, **when** `schema_help_fields()` emits the accepted-operation catalog, aliases, and required fields, **then** the six values and their sort-derived public output remain unchanged.
3. **Given** a `RuntimeSmokeRunner` configured with the existing adapters, **when** a plan reaches one of the six operations, **then** the runner dispatches the same existing adapter key; no runner, tool serializer, adapter map, or UI helper is changed.

## Functional requirements

| ID | Requirement |
|---|---|
| **SML-001** | `runtime_smoke_schema.py` MUST define exactly six module-private reuse values whose string values are the six identifiers listed above. |
| **SML-002** | Every duplicate occurrence covered by the six selected `python:S1192` records MUST reuse its matching private value: four for `ensure_visible`, four for `select_row`, five each for `click_row`, `right_click_row`, and `double_click_row`, and three for `toggle_item_child`. |
| **SML-003** | `OPERATION_SCHEMAS`, `schema_help_fields`, `validate_plan`, `normalize_plan_step`, `_validate_step`, and `_validate_op_args` MUST preserve their existing public names, required-field behavior, errors, aliases, and normalized internal names for the six operations. |
| **SML-004** | A behavior-characterization test in `tests/test_runtime_smoke_schema.py` MUST be added before the extraction and MUST cover the six identifiers through catalog/help, validation/normalization, and runner adapter dispatch behavior. |
| **SML-005** | The implementation MUST leave `session/runtime_smoke.py`, `tools/runtime_smoke.py`, `session/runtime_smoke_operations.py`, `session/runtime_smoke_v2/actions/__init__.py`, and `ui/grid.py` unchanged; they are no-change witnesses for the existing public vocabulary. |

## Nonfunctional preservation contract

- Every listed operation identifier is a public wire and authoring value; its UTF-8 bytes, spelling, punctuation, sort position, and `OperationSchema.internal_name` remain unchanged.
- The refactor remains entirely private to `runtime_smoke_schema.py`; it adds no public constant, import, registry, alias, schema revision, persistent state, or compatibility path.
- Existing `schema_help_fields()` ordering continues to derive from the unchanged `OPERATION_SCHEMAS` keys.
- The evidence file at [evidence/sonar-issues-2ff8607.json](evidence/sonar-issues-2ff8607.json) is a losslessly normalized current Sonar API inventory, not an immutable raw payload. Later source work does not modify it.

## Measurable success criteria

1. A later source diff contains one private string declaration for each of the six values and no remaining selected duplicate literal use in `runtime_smoke_schema.py`.
2. The new focused characterization test exercises all six identifiers and the existing focused schema test remains green when later run on the implementation candidate.
3. The no-change witnesses named in SML-005 retain their existing public adapter and serializer spellings.
4. The scoped Sonar evidence remains bound to the six listed issue IDs only; this packet does not claim disposition of the other 862 open blocking records.

## Exclusions and assumptions

**Excluded:** changing a public operation name; suppressing Sonar; adding a cross-module registry or public schema API; editing runner/tool/adapter/UI modules; changing tests during packet authoring; rerunning Sonar; or addressing any issue outside the six selected records.

**Assumptions:** the losslessly normalized current Sonar API inventory is the authoritative scope input for analysis `a76d9b95-c155-4102-abf1-472108b3e070` at the stated source base, and the existing focused schema tests characterize current public behavior before extraction.

## Clarification outcome

The material questions are resolved: scope is one source module, the six issue IDs and their exact values are fixed, the evidence identity is fixed, and the permitted later test home is fixed. Local `.specify` helper scripts and templates are absent, so this packet records the artifact stages authored manually and does not restore, add, or claim execution of SpecKit automation.

## Packet map

- [Research](research.md) binds the exact API evidence and records the selected design.
- [Data model](data-model.md) describes only the existing identifier-to-consumer relationship.
- [Plan](plan.md) defines the D1 boundary, witnesses, future proof, challenge, and checker.
- [Requirements checklist](checklists/requirements.md) records the planning-quality recheck.
- [Tasks](tasks.md) orders the later implementation work.
- [Quickstart](quickstart.md) is a future focused-verification guide; it makes no execution claim.

---
title: "Sonar Runtime-Smoke Column Predicate"
feature: "012-sonar-s1066-conditional"
design_depth: "D1"
status: "implemented-with-focused-proof-and-independent-check"
source_base: "c64218f6a988309c4fa9d676e6cede3a89097411"
release_intent: "none"
---

# Feature Specification: Sonar Runtime-Smoke Column Predicate

## Bounded outcome

Resolve one open Sonar `python:S1066` finding in `src/netcoredbg_mcp/session/runtime_smoke_schema.py:1169` by merging the nested column validation into its enclosing three-operation membership condition. The refactor is internal and semantics-preserving: it keeps the membership check, column-presence check, and non-string check in exact short-circuit order **O → C → ¬T**, and emits the existing diagnostic bytes unchanged.

This slice changes no public operation identifier, schema version, validation API, runner, adapter, persistence model, test source, release surface, or Sonar configuration.

## D1 calibration

**Bound unit:** one condition in one existing validator and its evidence-backed packet. The condition has a fixed local input/output contract and a reversible expression-only implementation, so D1 is sufficient. This packet intentionally contains no ADR, release plan, public API design, new subsystem, or broader Sonar remediation backlog.

## P1 maintainer scenario

A maintainer validates an existing runtime-smoke plan using `ui.grid.click_row`, `ui.grid.right_click_row`, or `ui.grid.double_click_row`. A supplied non-string `column` remains rejected with exactly the existing diagnostic; an omitted column and every other operation keep the prior validation behavior.

### Acceptance scenarios

1. **Given** one of the three affected operations and `args["column"]` present with a non-string value, **when** plan validation reaches `_validate_op_args()`, **then** it appends `"{prefix}.column must be a string for op {op_name}"` exactly as before.
2. **Given** one of the three affected operations without `column`, **when** validation runs, **then** no column-type diagnostic is added.
3. **Given** any non-affected operation with a non-string `column`, **when** validation runs, **then** this branch does not add a column-type diagnostic.
4. **Given** any input, **when** the predicate is evaluated, **then** membership O is evaluated before column presence C, and C is evaluated before non-string test ¬T.

## Functional requirements

| ID | Requirement |
|---|---|
| **S1066-001** | `_validate_op_args()` MUST replace only the nested affected condition with one conjunctive predicate covering exactly `ui.grid.click_row`, `ui.grid.right_click_row`, and `ui.grid.double_click_row`. |
| **S1066-002** | The predicate MUST preserve short-circuit evaluation order O → C → ¬T. |
| **S1066-003** | The appended error wording, interpolation, operation identifiers, and all behavior for every input MUST remain unchanged. |
| **S1066-004** | The source mutation MUST be confined to `src/netcoredbg_mcp/session/runtime_smoke_schema.py`; no test-source change belongs to this slice. |
| **S1066-005** | Exactly one independent checker MUST inspect the changed predicate against this specification and verify the parent receipt for the named three-node focused pytest command against the committed candidate. |

## Nonfunctional preservation contract

- The membership set contains exactly the existing three public operation identifiers in the existing order.
- The `"column" in args` test occurs only after membership succeeds; `isinstance(args["column"], str)` occurs only after column presence succeeds.
- The prior `errors.append(...)` string is retained byte-for-byte.
- The broader grid branch and all neighboring validation branches are no-change witnesses.

## Exclusions

Do not merge the predicate with the broader grid branch, add a helper, modify tests, alter operation definitions, suppress the rule, run a scanner, or resolve any issue other than `02f653e6-ffbe-42df-b23f-9a8c737c0a0d`.

## Packet map

- [Research](research.md) records the single sanitized finding and selected shape.
- [Data model](data-model.md) describes the existing validation relation only.
- [Plan](plan.md) defines integration boundaries, Challenge-LITE, and checker work.
- [Tasks](tasks.md) records completed implementation, parent proof, and read-only independent checking.
- [Quickstart](quickstart.md) records the exact parent command and its result.
- [Requirements checklist](checklists/requirements.md) records packet-quality review.

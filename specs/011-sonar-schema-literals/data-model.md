# Data Model: Existing Runtime-Smoke Operation Identifiers

**Status:** Planning description of an existing in-memory relationship. It creates no persisted model, database schema, registry, protocol version, or new public API.
**Spec:** [spec.md](spec.md)

## Existing values

| Existing identifier | Existing owner | Existing consumers |
|---|---|---|
| `ui.grid.ensure_visible` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |
| `ui.grid.select_row` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |
| `ui.grid.click_row` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |
| `ui.grid.right_click_row` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |
| `ui.grid.double_click_row` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |
| `ui.list.toggle_item_child` | `runtime_smoke_schema.py` operation definitions | schema help, validation, normalization, runner dispatch, adapter maps |

## Existing relationship

```text
public operation identifier
  -> OperationSchema in OPERATION_SCHEMAS
  -> schema_help_fields / validate_plan / normalize_plan_step
  -> RuntimeSmokeRunner normalized dispatch
  -> existing adapter-map and public tool serializers
```

A module-private constant may be the single source expression for one existing identifier, but it is not a new domain entity and does not alter the identifier's value, cardinality, lifetime, or consumer ownership.

## Invariant

For each of the six identifiers, every existing consumer observes the same byte sequence before and after the refactor. In particular, `OPERATION_SCHEMAS` retains the same key, `OperationSchema.internal_name`, and required fields; sorted schema-help output, validation, normalization, runner dispatch, and adapter lookup remain observationally identical.

## Storage boundary

These values are code-level public vocabulary, not stored records. The refactor adds no persistence, migration, serialization format, registry, cache, or external contracts directory. See [plan.md](plan.md) for the D1 boundary and no-change witnesses.

# Data Model: Existing Column Validation Predicate

**Status:** Description of an existing in-memory control-flow relationship. It creates no persisted model, schema, registry, protocol version, or public API.
**Spec:** [spec.md](spec.md)

## Existing values

| Name | Existing role |
|---|---|
| O | `op_name` is one of `ui.grid.click_row`, `ui.grid.right_click_row`, or `ui.grid.double_click_row`. |
| C | `"column" in args`. |
| T | `isinstance(args["column"], str)`. |
| `errors` | Existing mutable diagnostic accumulator. |

## Existing relationship

```text
O
  -> C
    -> ¬T
      -> append existing column-type diagnostic to errors
```

The refactor changes syntax from nested conditions to one conjunctive predicate; it does not change values, inputs, output cardinality, state lifetime, or consumer ownership.

## Invariant

For every `op_name` and `args` input, `errors` receives the exact same column diagnostic as before. Evaluation remains ordered O → C → ¬T, so `args["column"]` is never accessed by this predicate unless the membership and presence checks have both succeeded.

## Storage boundary

This is local validation control flow only. It adds no persistence, migration, serialization format, cache, or external contract directory.

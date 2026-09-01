# Implementation Plan: Sonar Runtime-Smoke Column Predicate

**Spec:** [spec.md](spec.md)  
**Design depth:** D1  
**Source base:** `c64218f6a988309c4fa9d676e6cede3a89097411`  
**Evidence:** [evidence/sonar-s1066.json](evidence/sonar-s1066.json)  
**Release intent:** `none`

## D1 boundary contract

| Boundary | Contract |
|---|---|
| **Input** | Existing `op_name`, `args`, and `errors` values in `_validate_op_args()`. |
| **Output** | The same error-list mutation for precisely the same inputs. |
| **Attaches to** | The three-operation column-validation branch at `runtime_smoke_schema.py:1164-1170` on the stated base. |
| **Does not touch** | The broader grid branch, public operation identifiers, schemas, normalization, runner/tool/adapter modules, tests, evidence beyond this sanitized record, Sonar configuration, or release surfaces. |

## Integration points and witnesses

| Point | Existing relationship | Planned effect |
|---|---|---|
| `_validate_op_args()` | Validates operation-specific arguments and accumulates diagnostics. | Merge only the affected nested predicate. |
| Affected three-operation membership | Limits the column rule to click/right-click/double-click row operations. | Same identifiers and membership semantics. |
| `"column" in args` | Guards lookup of `args["column"]`. | Same second short-circuit condition. |
| `isinstance(args["column"], str)` | Determines whether the current diagnostic is appended. | Same final condition and append body. |
| Broader grid branch | Performs unrelated grid validation. | No-change witness. |
| Existing schema tests | Cover valid dispatch, public identifiers, and invalid diagnostics. | No test-source mutation; parent runs focused proof. |

## Implementation sequence

1. The source refactor replaced the nested branch with one multi-line conjunctive predicate ordered O → C → ¬T.
2. The refactor retained the append statement exactly.
3. The source refactor and its original packet were committed at `99400a9254e77fe097286bfc233aef69af107ed9`.
4. The parent executed the [quickstart.md](quickstart.md) command from that candidate with `3 passed in 0.83s`. One independent checker then inspected the source invariant and verified the parent receipt.

## Test plan

The implementation maker did not execute tests under this assignment. The parent executed the three explicit existing test nodes together against `99400a9254e77fe097286bfc233aef69af107ed9` with `3 passed in 0.83s`; they cover valid adapter dispatch, public operation identifier preservation, and the exact invalid column diagnostics. The independent checker verified that receipt without running pytest. No broader test suite, formatter, linter, build, or Sonar scan substituted for that proof.

## Rollback

If the focused proof shows a semantic mismatch, restore only this predicate to the prior nested form. Do not add a suppression, alias, helper, fallback, or broader grid-branch change.

## Independent checker receipt

One checker who did not make this source edit inspected the committed source against [spec.md](spec.md), confirmed the O → C → ¬T order and unchanged append string, and verified the parent's exact three-node receipt from [quickstart.md](quickstart.md). The checker was read-only and did not run pytest. Its substantive source and packet checks passed; its prior `CHANGES_REQUIRED` status named only the then-pending T002 proof, which the parent subsequently completed. This receipt does not authorize release.

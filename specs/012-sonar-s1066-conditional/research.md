# Research: Sonar Runtime-Smoke Column Predicate

**Status:** Source and supplied-finding analysis for one D1 internal refactor. No test, formatter, linter, build, scanner, release, or PR action is claimed.
**Source base:** `c64218f6a988309c4fa9d676e6cede3a89097411`
**Evidence:** [evidence/sonar-s1066.json](evidence/sonar-s1066.json)

## Evidence labels

- **OBSERVED:** supplied finding or current source structure.
- **SELECTED:** required implementation shape.
- **INFERRED:** behavior an independent checker must verify.

## Finding binding

| Item | Value |
|---|---|
| Issue ID | `02f653e6-ffbe-42df-b23f-9a8c737c0a0d` |
| Rule | `python:S1066` |
| Message | `Merge this if statement with the enclosing one.` |
| Component | `src/netcoredbg_mcp/session/runtime_smoke_schema.py` |
| Line | 1169 |
| Analysis | `89bc77e7-8002-477e-99da-3bb4168ad076` |
| Head | `c64218f6a988309c4fa9d676e6cede3a89097411` |

The evidence file deliberately contains only these supplied non-secret fields; it is not a raw Sonar response and does not claim a rerun or issue resolution.

## Source-boundary facts

| Classification | Observation | Consequence |
|---|---|---|
| **OBSERVED** | `_validate_op_args()` has an outer membership check for exactly click, right-click, and double-click row operations. | O must remain the first predicate term. |
| **OBSERVED** | Its nested body checks column presence before indexing `args["column"]` and testing it as a string. | C must precede ¬T to retain safety and behavior. |
| **OBSERVED** | The nested body appends the current column-type error. | The append expression and its wording are preservation surfaces. |
| **OBSERVED** | The adjacent broader grid branch performs separate row, identity, visibility, and integer checks. | It is a no-change witness, not an expansion target. |

## Decision

**SELECTED: replace the two nested `if` statements with one conjunctive `if` whose terms are O, then C, then ¬T.** The existing append body remains unchanged.

## Challenge-LITE

| Alternative | Disposition | Reason |
|---|---|---|
| One conjunctive predicate | **GO / preferred** | Removes precisely the reported nesting while preserving visible evaluation order and the existing diagnostic. |
| Retain nesting | Rejected | Leaves the reported S1066 structure in place. |
| Extract a helper | Rejected | Adds an unnecessary local abstraction and obscures the required short-circuit order. |
| Merge with the broader grid branch | Rejected | Widens behavior and couples unrelated validations. |

## Research conclusion

The smallest coherent source change is local to the reported nested conditional. The exact focused three-node pytest command in [quickstart.md](quickstart.md) covers valid dispatch, public identifiers, and the exact invalid diagnostic; one independent checker must execute it on the committed candidate.

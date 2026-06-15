# NovaScript CR-003 Downstream Replay Packet - 2026-06-15

## Purpose

This packet is the CR-008 gate for backlog issue `#226`. It exists to prevent a
false closure from target-side netcoredbg-mcp evidence alone. The issue may be
closed only after a fresh NovaScript downstream verdict is recorded as `PASS`,
`BLOCKED`, or `FAIL`.

Machine-readable sidecar:
`docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.json`.

## Provider Baseline

- netcoredbg-mcp source baseline: `main@12287e55ac8ea7415a3717f4a248d08723b93cfd`.
- Package metadata version: `0.17.2`.
- Relevant provider fixes:
  - PR #87 / CR-003: DataGrid rows alias and semantic UI evidence.
  - PR #89 / CR-005: blocked semantic probe diagnostics.
  - PR #90 / CR-006: runtime-smoke lifecycle tools.
  - PR #91 / CR-007: diagnostic schema contract.
- Target-side evidence is not consumer closure evidence. It only proves the
  provider is ready for a downstream replay.

## Current CR-008 Replay Result

The 2026-06-15 clean downstream replay reached NovaScript and returned
`BLOCKED`, not `PASS`.

- Contract test: `PASS` (`2 passed`) in a clean detached NovaScript worktree.
- Runtime-smoke baseline launch: `PASS`.
- Runtime-smoke case: `BLOCKED` before the first drag.
- Blocker: the plan requested source identity `ROW-008-UNIQUE-PHRASE`, but
  NovaScript selected `ROW-031-UNIQUE-PHRASE` on load and the visible DataGrid
  viewport was `ROW-027-UNIQUE-PHRASE` through `ROW-044-UNIQUE-PHRASE`.
- Cleanup: `PASS`; debug stop succeeded and the process registry was empty.

Issue `#226` remains open. The downstream next step is to amend
`fixed-bug-regression.runtime-smoke-v2.json` so the first drag has an explicit
visible-row setup: scroll/select to `ROW-008-UNIQUE-PHRASE` before dragging, or
derive source/drop identities from the current visible viewport.

## Downstream Inputs

Use the NovaScript repository, not a local WPF substitute:

- Expected local path when available: `D:\Dev\novascript`.
- Runtime-smoke v2 plan:
  `NovaScript.Tests.UI/Scenarios/fixed-bug-regression.runtime-smoke-v2.json`.
- Scenario contract:
  `NovaScript.Tests.UI/Scenarios/fixed-bug-regression.feature-cycle.json`.
- Contract tests:
  `NovaScript.Tests.UI/Scenarios/FixedBugRegressionProtocolTests.cs`.
- Main UI selector: `CueDataGrid` with `control_type: DataGrid`.
- Stable row identity column: `Реплика`.
- Required variants:
  - `visible_row_drag`
  - `downward_edge_scroll`
  - `upward_edge_scroll`
  - `multi_row_drag`
  - `invalid_drop_noop_or_cancel`

## Preflight

1. Confirm the downstream worktree and branch.
2. Confirm no unknown tracked dirty state in NovaScript.
3. Confirm the Debug WPF build is current.
4. Confirm the plan still sets:
   - `NOVASCRIPT_UI_TEST_MODE=1`
   - `NOVASCRIPT_UI_TEST_AUTO_OPEN_DOCUMENT=1`
   - `NOVASCRIPT_UI_TEST_DISABLE_RESTORE=1`
   - `WINDIR`, `windir`, and `SystemRoot`
5. Confirm the plan points to a deterministic `.nvr` fixture and writes evidence
   under `.agent/runtime-evidence/`.

## Replay Commands

First, validate the downstream contract still names the runtime-smoke gate:

```powershell
dotnet test NovaScript.Tests.UI\NovaScript.Tests.UI.csproj -c Debug --no-restore --filter "FullyQualifiedName~FixedBugRegressionProtocol_RecordsRuntimeSmokeV2ResumeGate|FullyQualifiedName~FixedBugRegressionProtocol_HasExecutableRuntimeSmokeV2Plan" -v minimal
```

Then run the v2 plan through the active netcoredbg-mcp server with the plan from
`NovaScript.Tests.UI/Scenarios/fixed-bug-regression.runtime-smoke-v2.json`.
Use `run_runtime_smoke` for a one-shot replay, or the lifecycle tools
`runtime_smoke_start`, `runtime_smoke_tail_events`, `runtime_smoke_get_result`,
and `runtime_smoke_stop` when the run needs observation or cancellation.

## PASS Criteria

The downstream verdict is `PASS` only when all of these are true:

- The final runtime-smoke status is `PASS`.
- Every required variant is covered by current evidence.
- `ui.drag` returns backend-produced `route_evidence`.
- `ui.grid.viewport` before/after evidence proves the expected viewport and row
  identity changes.
- Row count and identity set are preserved.
- `invalid_drop_noop_or_cancel` proves no mutation and cleanup evidence.
- Cleanup reports debug stop success and no leftover NovaScript/netcoredbg
  process registry entries.
- The result is not the known false-positive shape where a row-header-side
  source coordinate is recentered to the row body and only viewport movement
  makes the output look like a reorder.

## BLOCKED Criteria

The downstream verdict is `BLOCKED` when the replay cannot honestly prove
consumer behavior. The blocker must name:

- Missing access, build, GUI desktop, fixture, or MCP tool.
- The exact command/tool that failed.
- The owner of the next action.
- The next concrete step.

Historical evidence that is explicitly not enough:

- May 2026 source-side T036 artifacts under
  `.agent/debug/source-side-runtime-smoke-t036/`.
- The `v0.17.1` NovaScript drag check in
  `.agent/reports/netcoredbg-v0.17.1-novascript-drag-check-2026-05-16.md`.
- The current NovaScript contract entry
  `2026_06_15_current_drag_check`, which is marked
  `PARTIAL_PASS_INVALID_FOR_GATE` because row-header route evidence was
  recentered.

## FAIL Criteria

The downstream verdict is `FAIL` when the run executes against current provider
and consumer code but NovaScript behavior violates the plan:

- DataGrid target is found but row identity/order assertions fail.
- The expected no-op mutates the document.
- Cleanup cannot restore the fixture or leaves debuggee processes.
- The result drops required diagnostic fields instead of returning actionable
  `BLOCKED` evidence.

## Evidence Output

Save the CR-008 downstream verdict in:

`.agent/specs/issue-backlog-hardening-roadmap/evidence/CR-008.downstream.json`

Required fields:

- `status`: `PASS`, `BLOCKED`, or `FAIL`
- `timestamp`
- `netcoredbg_mcp_commit`
- `netcoredbg_mcp_version`
- `novascript_path`
- `novascript_branch`
- `plan_path`
- `contract_test_command`
- `runtime_smoke_command_or_tool`
- `required_variants`
- `observed_variants`
- `cleanup`
- `issue_226_lifecycle_decision`

Do not mark issue `#226` closed unless this evidence records a fresh downstream
`PASS` or an explicit owner-approved lifecycle decision.

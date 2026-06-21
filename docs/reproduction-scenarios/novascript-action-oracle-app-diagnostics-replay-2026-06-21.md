# NovaScript Action-Oracle App-Diagnostics Replay Packet - 2026-06-21

## Purpose

This packet records the downstream replay lifecycle for the `v0.19.0`
NovaScript action-oracle app-diagnostics consumer gate. It started as the
provider-side handoff for CR-100 and now also records the returned live
NovaScript product behavior proof for the bounded CR-100 replay.

Machine-readable sidecar:
`docs/reproduction-scenarios/novascript-action-oracle-app-diagnostics-replay-2026-06-21.json`.

This packet is related to broad `#268` and `#272` lifecycle scope. It does not
close either broad issue.

## Provider Baseline

- netcoredbg-mcp source baseline:
  `main@6d777dae788d4b6008fa36f1dc2172fd7e4df208`.
- Package/runtime version: `0.19.0`.
- Provider preflight output: `netcoredbg-mcp 0.19.0`.
- Contract source:
  `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`.
- Generated template: `novascript-action-oracle`.
- Generated case id: `action_oracle_diagnostics`.
- Generated probe kind: `app_diagnostics`.

Provider-side evidence says the contract validates and expands. The returned
downstream result below says the real NovaScript application wrote the expected
diagnostic payload for this bounded replay.

## Boundary

This replay packet:

- records status `DOWNSTREAM_REPLAY_PASS`;
- records live NovaScript product behavior proof for the bounded CR-100 replay;
- does not replace the CR-003 DataGrid drag/drop replay gate;
- does not close `#268`, `#272`, or any broad roadmap issue by itself.

Do not close broad issues from this packet alone. Issue lifecycle still requires
explicit split or closure evidence for broad `#268` and `#272` scope.

## Downstream Result

NovaScript reported `PASS` through Engram `#326`.

- Engram source issue: `#326`, closed after source-side PASS.
- Run id: `1b9814e8099f4d8e9a735eb71a051c40`.
- Provider source:
  `D:/Dev/netcoredbg-mcp@6d777dae788d4b6008fa36f1dc2172fd7e4df208`.
- NovaScript source:
  - path: `D:/Dev/novascript`
  - branch: `work/cr-027-session-owned-undo-scope`
  - commit: `2620c3a4858fc6069404ec56c227b42de5e42442`
- Tool invocation:
  `mcp__netcoredbg.runtime_smoke_run_plan(plan_path="D:/Dev/novascript/NovaScript.Tests.UI/Scenarios/action-oracle-app-diagnostics.runtime-smoke-v2.json", agent_mode=true)`.
- Adapted plan:
  `D:/Dev/novascript/NovaScript.Tests.UI/Scenarios/action-oracle-app-diagnostics.runtime-smoke-v2.json`.
- App diagnostic source:
  `.agent/runtime-smoke/app-diagnostics-cr100-root/diagnostic-cue-change.json`.
- Action: `ui.grid.select` on `CueDataGrid`, index `1`.
- Oracle: `$.current_cue_index == 1`.
- Observed value: `1`; selected phrase: `Fixture cue two`.
- Freshness: `PASS`; host process `dotnet`; `NovaScript.dll` loaded; symbols loaded.
- Cleanup: `PASS`; `debug.stop` graceful; process registry after cleanup `0`.
- Issue lifecycle comments: `#268` comment `1099`, `#272` comment `1100`.

## Adapted Plan Delta

The provider example remains a reusable template. It contains a placeholder
`ui.invoke` Button action at
`docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`.

For this recorded `DOWNSTREAM_REPLAY_PASS`, use the adapted NovaScript plan at
`D:/Dev/novascript/NovaScript.Tests.UI/Scenarios/action-oracle-app-diagnostics.runtime-smoke-v2.json`
or apply this exact delta to the provider example; do not replay the base
provider example verbatim for this evidence packet:

do not replay the base provider example verbatim for this evidence packet.

- Source example action: `ui.invoke` on Button selector
  `<ACTION_ORACLE_TRIGGER_AUTOMATION_ID>`.
- Actual replay action: `ui.grid.select` on `CueDataGrid`, index `1`.
- Actual replay oracle: `$.current_cue_index == 1`.
- Observed value: `1`; selected phrase: `Fixture cue two`.
- App diagnostic source:
  `.agent/runtime-smoke/app-diagnostics-cr100-root/diagnostic-cue-change.json`.

## Downstream Inputs

Use the NovaScript repository, not a local WPF substitute:

- Set `<NOVASCRIPT_REPO>` to the local NovaScript checkout.
- Copy
  `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`
  into that checkout, for example as
  `NovaScript.Tests.UI/Scenarios/action-oracle-app-diagnostics.runtime-smoke-v2.json`.
- Replace:
  - `<NOVASCRIPT_PROGRAM_DLL_OR_EXE>`
  - `<NOVASCRIPT_DEBUG_OUTPUT_DIR>`
  - `<NOVASCRIPT_PROJECT_FILE>`
  - `<NOVASCRIPT_REPO>`
  - `<ACTION_ORACLE_TRIGGER_AUTOMATION_ID>`
  - `<NOVASCRIPT_PROCESS_NAME>`
  - `<NOVASCRIPT_PRIMARY_MODULE>`

For EXE launches, `<NOVASCRIPT_PROCESS_NAME>` normally names the NovaScript
process. For DLL launches through a host process, use the host process for
freshness while keeping `<NOVASCRIPT_PRIMARY_MODULE>` as the NovaScript assembly.

## Preflight

From the provider checkout:

```powershell
uv run --no-sync --project <NETCOREDBG_MCP_REPO> netcoredbg-mcp --version
```

Expected output:

```text
netcoredbg-mcp 0.19.0
```

From the NovaScript checkout:

1. Confirm the downstream branch and tracked dirty state.
2. Confirm the Debug build path and project file are current.
3. Confirm the selected UI action writes the action-oracle diagnostic payload.
4. Confirm the adapted plan writes evidence under
   `.agent/runtime-smoke/app-diagnostics/`.

## Replay Commands

Run the adapted plan through the active `netcoredbg-mcp 0.19.0` MCP server from
the NovaScript repository root.

Use `run_runtime_smoke` for a one-shot replay. If the run needs observation or
cancellation, use:

- `runtime_smoke_start`
- `runtime_smoke_tail_events`
- `runtime_smoke_get_result`
- `runtime_smoke_stop`

## PASS Criteria

The downstream verdict is `PASS` only when all of these are true:

- The final runtime-smoke status is `PASS`.
- The generated case `action_oracle_diagnostics` runs the requested UI action.
- The generated `app_diagnostics` probe observes the action-written diagnostic
  payload.
- Freshness evidence matches `<NOVASCRIPT_PROCESS_NAME>` and
  `<NOVASCRIPT_PRIMARY_MODULE>` for the actual launched process shape.
- Cleanup reports successful `debug.stop` and
  `process.registry.assert_empty`.

## BLOCKED Criteria

The downstream verdict is `BLOCKED` when the replay cannot honestly prove live
consumer behavior. The blocker must name:

- missing NovaScript checkout, GUI desktop, Debug build, selector, provider
  runtime, or MCP tool access;
- the exact command/tool that failed;
- the owner of the next action;
- the next concrete step.

## FAIL Criteria

The downstream verdict is `FAIL` when the run executes against current provider
and consumer code but NovaScript behavior violates the plan:

- the UI action route executes but the expected action-oracle diagnostic state
  is wrong;
- freshness or module evidence contradicts the launched process;
- cleanup cannot stop the debuggee or leaves process registry entries.

## Evidence Output

The downstream verdict was saved by the source side in:

`.agent/specs/issue-backlog-hardening-roadmap/evidence/CR-100.novascript-action-oracle-app-diagnostics.downstream.json`

Required fields:

- `status`: `PASS`, `BLOCKED`, or `FAIL`
- `timestamp`
- `netcoredbg_mcp_commit`
- `netcoredbg_mcp_version`
- `novascript_path`
- `novascript_branch`
- `adapted_plan_path`
- `provider_preflight`
- `runtime_smoke_command_or_tool`
- `generated_case_id`
- `generated_probe_kind`
- `app_diagnostics_status`
- `freshness`
- `cleanup`
- `issue_lifecycle_decision`

Do not close `#268`, `#272`, or related broad issues unless a separate issue
lifecycle decision records broad closure evidence.

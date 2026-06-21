# NovaScript Action-Oracle App-Diagnostics Replay Packet - 2026-06-21

## Purpose

This packet is the downstream replay request for the `v0.19.0` NovaScript
action-oracle app-diagnostics consumer gate. It exists to keep the boundary
honest: CR-099 and the provider-side tests prove provider-side readiness, but
they are not live NovaScript product behavior proof.

Machine-readable sidecar:
`docs/reproduction-scenarios/novascript-action-oracle-app-diagnostics-replay-2026-06-21.json`.

This packet is related to broad `#268` and `#272` lifecycle scope. It does not
close either broad issue.

## Provider Baseline

- netcoredbg-mcp source baseline:
  `main@e6d3fac78ae2aaa1e6b1cde8b3f7c5d703c03093`.
- Package/runtime version: `0.19.0`.
- Contract source:
  `docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json`.
- Generated template: `novascript-action-oracle`.
- Generated case id: `action_oracle_diagnostics`.
- Generated probe kind: `app_diagnostics`.

Provider-side evidence says the contract validates and expands. It does not say
the real NovaScript application wrote the expected diagnostic payload.

## Boundary

This replay packet:

- is provider-side readiness handoff material;
- is not live NovaScript product behavior proof;
- does not replace the CR-003 DataGrid drag/drop replay gate;
- does not close `#268`, `#272`, or any broad roadmap issue by itself.

Do not close broad issues from this packet alone. Issue lifecycle requires the
returned live consumer evidence or an explicit split/closure decision.

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

Save the downstream verdict in:

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

Do not close `#268`, `#272`, or related broad issues unless this evidence
records a live consumer `PASS` or an explicit owner-approved lifecycle decision.

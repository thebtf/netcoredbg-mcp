# Issue Reproduction Scenarios - 2026-06-15

This document records the controlled reproduction surfaces for the active issue
backlog: `#226`, `#250`, `#251`, `#254`, `#264`, `#265`, `#266`, `#267`,
`#268`, `#269`, `#270`, `#271`, and `#272`.

Executable scenarios are committed as `pytest.mark.xfail(strict=True)` tests.
Normal pytest runs keep the suite green while preserving known reproductions.
Run the same tests with `--runxfail` to prove the current RED behavior before a
fix.

## Executable RED Scenarios

| Issue | Path | RED proof command | Current expected failure |
| --- | --- | --- | --- |
| `#264` | `tests/test_ui_backend.py::TestFlaUIBridgeClient::test_call_restarts_bridge_after_cancelled_request` | `uv run pytest --runxfail tests/test_ui_backend.py::TestFlaUIBridgeClient::test_call_restarts_bridge_after_cancelled_request -q` | Cancelled bridge calls do not stop the bridge. |
| `#250` | `tests/test_ui_new_tools.py::test_ui_right_click_uses_flaui_backend_for_dict_elements` | `uv run pytest --runxfail tests/test_ui_new_tools.py::test_ui_right_click_uses_flaui_backend_for_dict_elements -q` | `ui_right_click` calls `click_input` on a FlaUI dict element. |
| `#251` | `tests/test_stealth_mode.py::test_session_manager_stealth_launch_defers_foreground_restore_until_ui_ready` | `uv run pytest --runxfail tests/test_stealth_mode.py::test_session_manager_stealth_launch_defers_foreground_restore_until_ui_ready -q` | Stealth launch restores foreground before UI readiness/MainWindow proof. |
| `#254` | `tests/test_ui_evidence.py::test_ui_grid_accepts_rows_alias_for_visible_rows` | `uv run pytest --runxfail tests/test_ui_evidence.py::test_ui_grid_accepts_rows_alias_for_visible_rows -q` | `ui_grid(action="rows")` returns `unknown grid action`. |
| `#265` | `tests/test_ui_new_tools.py::TestFlaUIBackendInvoke::test_ui_invoke_blocks_mismatched_exact_automation_id` | `uv run pytest --runxfail tests/test_ui_new_tools.py::TestFlaUIBackendInvoke::test_ui_invoke_blocks_mismatched_exact_automation_id -q` | `ui_invoke` accepts a returned `automationId` that differs from the requested exact id. |
| `#266` | `tests/test_stealth_mode.py::test_ui_get_window_tree_reconnects_same_pid_after_bridge_disconnect` | `uv run pytest --runxfail tests/test_stealth_mode.py::test_ui_get_window_tree_reconnects_same_pid_after_bridge_disconnect -q` | Same-PID stale backend is not reconnected before `ui_get_window_tree`. |
| `#267` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | `uv run pytest --runxfail tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered -q` | `runtime_smoke_start` is not registered. |
| `#268` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | Same as `#267` | `runtime_smoke_tail_events` is not registered. |
| `#269` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | Same as `#267` | `runtime_smoke_get_result` and `runtime_smoke_stop` are not registered. |
| `#270` | `tests/test_runtime_smoke_v2_probes/test_ui_text.py::test_ui_text_probe_preserves_blocked_backend_diagnostics` | `uv run pytest --runxfail tests/test_runtime_smoke_v2_probes/test_ui_text.py::test_ui_text_probe_preserves_blocked_backend_diagnostics -q` | Blocked semantic probes drop actionable backend diagnostics. |

## Blocked Or Spec-Needed Scenarios

| Issue | Status | Required next step |
| --- | --- | --- |
| `#226` | Downstream replay blocked | Re-run the NovaScript CR-003 gate with `docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md`; target-side v0.17.2 evidence is not enough to close the consumer issue. |
| `#271` | SpecKit needed | Define oracle-pack and app-diagnostics schema before writing executable tests. |
| `#272` | SpecKit needed for full scope | Define semantic-probe and tracepoint-guardrail vocabulary before full executable tests. `#270` covers the immediate blocked-diagnostics slice. |

## Fixture Replays To Add With Fix PRs

- `#265`: add a WPF fixture panel with a side-effecting
  `buttonCharlistRemove` and deliberately missing `playButton`, then assert
  `ui_invoke(automation_id="playButton", control_type="Button",
  root_id="selectorSafetyPanel")` produces a selector miss and no side effect.
- `#254`: add a controlled `dataGrid2` fixture before reproducing the
  downstream `dataGrid2` semantic evidence locally.
- `#226`: replay the downstream WPF/NovaScript gate with
  `docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md` rather
  than substituting a target-only local smoke.

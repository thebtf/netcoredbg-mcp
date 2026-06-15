# Issue Reproduction and Readiness Ledger - 2026-06-15

This document tracks the reproduction surfaces and current readiness status for
the UI emulation/testing backlog: `#226`, `#250`, `#251`, `#254`, `#264`,
`#265`, `#266`, `#267`, `#268`, `#269`, `#270`, `#271`, and `#272`.

The original reproduction rows below are historical RED proofs. Current closure
status is tracked separately so fixed issues are not advertised as still-red
work.

## Current Issue Status

| Issue | Current status | Evidence | Remaining action |
| --- | --- | --- | --- |
| `#226` | Downstream replay `BLOCKED` | PR #92, `docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md`, and sidecar status `DOWNSTREAM_REPLAY_BLOCKED` | Keep open. NovaScript owner must amend `fixed-bug-regression.runtime-smoke-v2.json` so the first drag source row is visible or selected dynamically, then rerun the replay packet. |
| `#250` | Target evidence merged | PR #86 and PR #87; FlaUI dict right-click/double-click routing stayed green in CR-003 focused checks | None in netcoredbg-mcp. |
| `#251` | Target evidence merged | PR #88; stealth foreground restore waits for UI readiness; WPF delayed-readiness replay passed | None in netcoredbg-mcp. |
| `#254` | Target evidence merged | PR #87; `ui_grid(action="rows")` aliases to `visible_rows`; WPF rows-alias replay passed | None in netcoredbg-mcp. |
| `#264` | Target evidence merged | PR #84; cancelled FlaUI bridge calls stop/recover without stale bridge leakage | None in netcoredbg-mcp. |
| `#265` | Target evidence merged | PR #86; exact selector mismatches return `BLOCKED` before side effects; WPF selector-safety replay passed | None in netcoredbg-mcp. |
| `#266` | Target evidence merged | PR #84; same-PID stale backend reconnects before `ui_get_window_tree` without foreground activation | None in netcoredbg-mcp. |
| `#267` | Target evidence merged | PR #90; `runtime_smoke_start` registered and lifecycle coverage merged | None in netcoredbg-mcp. |
| `#268` | Target evidence merged | PR #90; `runtime_smoke_tail_events` registered and lifecycle coverage merged | None in netcoredbg-mcp. |
| `#269` | Target evidence merged | PR #90; `runtime_smoke_get_result` and `runtime_smoke_stop` registered and lifecycle coverage merged | None in netcoredbg-mcp. |
| `#270` | Target evidence merged | PR #89; blocked semantic probes preserve requested/accepted/next_step/backend diagnostics | None in netcoredbg-mcp. |
| `#271` | Target evidence merged | PR #91; diagnostic schema `netcoredbg.runtime_smoke.diagnostics.v1`, oracle/app examples, and focused schema/docs/contracts tests merged | None in netcoredbg-mcp. |
| `#272` | Target evidence merged | PR #91; semantic-probe registry contract and tracepoint guardrail vocabulary merged | None in netcoredbg-mcp. |

## Historical RED Proof Commands

These commands document the reproduction-first protocol used before each fix.
They are not the current expected behavior after the linked PRs merged.

| Issue | Historical path | Historical RED proof command | Former expected failure |
| --- | --- | --- | --- |
| `#264` | `tests/test_ui_backend.py::TestFlaUIBridgeClient::test_call_restarts_bridge_after_cancelled_request` | `uv run pytest --runxfail tests/test_ui_backend.py::TestFlaUIBridgeClient::test_call_restarts_bridge_after_cancelled_request -q` | Cancelled bridge calls did not stop the bridge. |
| `#250` | `tests/test_ui_new_tools.py::test_ui_right_click_uses_flaui_backend_for_dict_elements` | `uv run pytest --runxfail tests/test_ui_new_tools.py::test_ui_right_click_uses_flaui_backend_for_dict_elements -q` | `ui_right_click` called `click_input` on a FlaUI dict element. |
| `#251` | `tests/test_stealth_mode.py::test_session_manager_stealth_launch_defers_foreground_restore_until_ui_ready` | `uv run pytest --runxfail tests/test_stealth_mode.py::test_session_manager_stealth_launch_defers_foreground_restore_until_ui_ready -q` | Stealth launch restored foreground before UI readiness/MainWindow proof. |
| `#254` | `tests/test_ui_evidence.py::test_ui_grid_accepts_rows_alias_for_visible_rows` | `uv run pytest --runxfail tests/test_ui_evidence.py::test_ui_grid_accepts_rows_alias_for_visible_rows -q` | `ui_grid(action="rows")` returned `unknown grid action`. |
| `#265` | `tests/test_ui_new_tools.py::TestFlaUIBackendInvoke::test_ui_invoke_blocks_mismatched_exact_automation_id` | `uv run pytest --runxfail tests/test_ui_new_tools.py::TestFlaUIBackendInvoke::test_ui_invoke_blocks_mismatched_exact_automation_id -q` | `ui_invoke` accepted a returned `automationId` that differed from the requested exact id. |
| `#266` | `tests/test_stealth_mode.py::test_ui_get_window_tree_reconnects_same_pid_after_bridge_disconnect` | `uv run pytest --runxfail tests/test_stealth_mode.py::test_ui_get_window_tree_reconnects_same_pid_after_bridge_disconnect -q` | Same-PID stale backend was not reconnected before `ui_get_window_tree`. |
| `#267` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | `uv run pytest --runxfail tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered -q` | `runtime_smoke_start` was not registered. |
| `#268` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | Same as `#267` | `runtime_smoke_tail_events` was not registered. |
| `#269` | `tests/test_runtime_smoke_registration.py::test_runtime_smoke_agent_lifecycle_tools_are_registered` | Same as `#267` | `runtime_smoke_get_result` and `runtime_smoke_stop` were not registered. |
| `#270` | `tests/test_runtime_smoke_v2_probes/test_ui_text.py::test_ui_text_probe_preserves_blocked_backend_diagnostics` | `uv run pytest --runxfail tests/test_runtime_smoke_v2_probes/test_ui_text.py::test_ui_text_probe_preserves_blocked_backend_diagnostics -q` | Blocked semantic probes dropped actionable backend diagnostics. |
| `#271/#272` | `tests/test_runtime_smoke_diagnostics_schema.py` and `tests/test_runtime_smoke_v2_docs.py` | `uv run pytest --runxfail tests/test_runtime_smoke_diagnostics_schema.py tests/test_runtime_smoke_v2_docs.py -q` | Strengthened schema/docs contracts failed before CR-007 implementation. |

## Remaining Follow-Up

- `#226`: replay the downstream WPF/NovaScript gate with
  `docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md` after
  the NovaScript runtime-smoke plan establishes a visible source row before the
  first drag. Target-side v0.17.2 evidence is not enough to close the consumer
  issue.

from __future__ import annotations

import json
from pathlib import Path

REPLAY_PACKET = Path("docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md")
REPLAY_PACKET_JSON = Path("docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.json")
BACKLOG_SCENARIOS = Path("docs/reproduction-scenarios/issues-backlog-2026-06-15.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _issue_row(backlog: str, issue: str) -> str:
    for line in backlog.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == f"`{issue}`":
            return line
    raise AssertionError(f"missing backlog row for {issue}")


def _section_issue_row(backlog: str, heading: str, issue: str) -> str:
    expected_heading = heading.strip()
    in_section = False
    for line in backlog.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped == expected_heading
            continue
        if not in_section:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == f"`{issue}`":
            return line
    raise AssertionError(f"missing {heading} row for {issue}")


def _issue_cells(backlog: str, issue: str) -> tuple[str, str, str, str]:
    row = _section_issue_row(backlog, "## Current Issue Status", issue)
    cells = tuple(cell.strip() for cell in row.strip().strip("|").split("|"))
    assert len(cells) == 4, f"expected 4 cells for {issue}, got {len(cells)}: {row!r}"
    return cells


def test_novascript_cr003_replay_packet_is_actionable() -> None:
    packet = _read(REPLAY_PACKET)
    required_terms = {
        "#226",
        "main@12287e55ac8ea7415a3717f4a248d08723b93cfd",
        "0.17.2",
        "<NOVASCRIPT_REPO>",
        "NovaScript.Tests.UI/Scenarios/fixed-bug-regression.runtime-smoke-v2.json",
        "NovaScript.Tests.UI/Scenarios/fixed-bug-regression.feature-cycle.json",
        "FixedBugRegressionProtocolTests.cs",
        "CueDataGrid",
        "DataGrid",
        "Реплика",
        "visible_row_drag",
        "edge_scroll_drag",
        "multi_row_drag",
        "invalid_drop_noop_or_cancel",
        "PASS",
        "BLOCKED",
        "FAIL",
        "route_evidence",
        "ui.grid.viewport",
        "debug stop",
        "process registry",
        "PARTIAL_PASS_INVALID_FOR_GATE",
        "CR-008.downstream.json",
        "issue_226_lifecycle_decision",
        "docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.json",
        "ROW-008-UNIQUE-PHRASE",
        "ROW-031-UNIQUE-PHRASE",
        "ROW-027-UNIQUE-PHRASE",
        "ROW-044-UNIQUE-PHRASE",
    }

    for term in required_terms:
        assert term in packet
    assert "target-side netcoredbg-mcp evidence alone" in packet
    assert "Do not mark issue `#226` closed" in packet


def test_novascript_cr003_replay_packet_json_is_machine_readable() -> None:
    payload = json.loads(REPLAY_PACKET_JSON.read_text(encoding="utf-8"))

    assert payload["schema"] == "netcoredbg.downstream_replay_packet.v1"
    assert payload["issue"] == "#226"
    assert payload["status"] == "DOWNSTREAM_REPLAY_PASS"
    assert payload["provider_baseline"]["commit"] == "12287e55ac8ea7415a3717f4a248d08723b93cfd"
    assert payload["provider_baseline"]["version"] == "0.17.2"
    assert payload["downstream"]["expected_local_path"] == "<NOVASCRIPT_REPO>"
    assert (
        payload["downstream"]["plan_path"]
        == "NovaScript.Tests.UI/Scenarios/fixed-bug-regression.runtime-smoke-v2.json"
    )
    assert payload["downstream"]["selector"] == {
        "automation_id": "CueDataGrid",
        "control_type": "DataGrid",
    }
    assert payload["downstream"]["identity"] == {"column": "Реплика"}
    assert set(payload["required_variants"]) == {
        "visible_row_drag",
        "edge_scroll_drag",
        "multi_row_drag",
        "invalid_drop_noop_or_cancel",
    }
    assert set(payload["historical_required_variants"]["values"]) == {
        "visible_row_drag",
        "downward_edge_scroll",
        "upward_edge_scroll",
        "multi_row_drag",
        "invalid_drop_noop_or_cancel",
    }
    assert "PARTIAL_PASS_INVALID_FOR_GATE" in " ".join(payload["known_invalid_evidence"])
    assert payload["latest_replay"]["status"] == "PASS"
    assert payload["latest_replay"]["runtime_smoke"]["baseline_status"] == "PASS"
    assert payload["latest_replay"]["runtime_smoke"]["status"] == "PASS"
    assert (
        payload["latest_replay"]["runtime_smoke"]["reason"]
        == "runtime smoke v2 scenario passed"
    )
    assert set(payload["latest_replay"]["runtime_smoke"]["observed_variants"]) == set(
        payload["required_variants"]
    )
    assert set(payload["latest_replay"]["runtime_smoke"]["observed_cases"]) == {
        "visible-row-drag-reorder",
        "edge-scroll-drag-reorder",
        "multi-row-drag-reorder",
        "invalid-drop-noop",
    }
    assert payload["latest_replay"]["cleanup"]["status"] == "PASS"
    assert payload["latest_replay"]["cleanup"]["process_registry_after"] == 0
    assert payload["latest_replay"]["issue_226_lifecycle_decision"] == "target_resolved"
    assert payload["evidence_output"]["status_values"] == ["PASS", "BLOCKED", "FAIL"]
    assert set(payload["evidence_output"]["required_fields"]) >= {
        "status",
        "timestamp",
        "netcoredbg_mcp_commit",
        "netcoredbg_mcp_version",
        "novascript_path",
        "novascript_branch",
        "plan_path",
        "contract_test_command",
        "runtime_smoke_command_or_tool",
        "required_variants",
        "observed_variants",
        "cleanup",
        "issue_226_lifecycle_decision",
    }


def test_issues_backlog_links_novascript_replay_packet() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    assert "docs/reproduction-scenarios/novascript-cr003-replay-2026-06-15.md" in backlog
    assert "target-side v0.17.2 evidence alone was not enough" in backlog.lower()
    assert "DOWNSTREAM_REPLAY_PASS" in backlog


def test_issues_backlog_current_status_is_not_stale_red_queue() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    assert "## Current Issue Status" in backlog
    assert "## Historical RED Proof Commands" in backlog
    assert "## Executable RED Scenarios" not in backlog
    assert "## Blocked Or Spec-Needed Scenarios" not in backlog
    assert "SpecKit needed" not in backlog
    assert "`#226` | Target-side resolved after downstream replay `PASS`" in backlog

    for issue in (
        "#251",
        "#264",
        "#265",
        "#266",
    ):
        assert f"`{issue}` | Target evidence merged" in backlog

    row_267 = _issue_row(backlog, "#267")
    assert "Target evidence merged and target-side Engram issue resolved" in row_267
    assert "CR-011" in row_267
    assert "ui_monitor_start" in row_267
    assert "ui_monitor_events" in row_267
    assert "Source-side owner may close" in row_267

    row_250 = _issue_row(backlog, "#250")
    assert "Target-side Engram issue resolved" in row_250
    assert "broader issue accepted as covered by accumulated evidence" in row_250
    assert "Source-side owner may close" in row_250

    for issue in ("#268",):
        row = _issue_row(backlog, issue)
        assert (
            "Target slice merged" in row
            or "Target screenshot-orientation slice covered" in row
            or "Target focused-element query and screenshot-orientation slices covered" in row
        )
        assert "broader" in row
        assert "None in netcoredbg-mcp." not in row

    row_269 = _issue_row(backlog, "#269")
    assert (
        "Target metrics/profile-defaults/probe-validation/source-aware run-probe "
        "guidance slices covered"
        in row_269
    )
    assert "broader FR remains open" in row_269
    assert "CR-039" in row_269
    assert "CR-041" in row_269
    assert "CR-082" in row_269
    assert "CR-083" in row_269
    assert "metrics_contract" in row_269
    assert "agent_mode.defaults" in row_269
    assert "quiet active app-diagnostics delta wait-continuation guidance" in row_269
    assert "source-aware run-probe first-follow-up guidance" in row_269
    assert "event_limit=20" in row_269
    assert "NO DATA" in row_269
    assert "None in netcoredbg-mcp." not in row_269

    assert "CR-021" in row_250
    assert "CR-037" in row_250
    assert "CR-040" in row_250
    assert "CR-043" in row_250
    assert "focus proof" in row_250
    assert "get_focused_element" in row_250
    assert "ui_get_focused_element" in row_250
    assert "selection confirmation" in row_250
    assert "ui_take_annotated_screenshot" in row_250
    assert "ui_click_annotated" in row_250
    assert "screen-space click centers unchanged" in row_250

    row_254 = _issue_row(backlog, "#254")
    assert "Target evidence merged and target-side Engram issue resolved" in row_254
    assert "CR-021" in row_254
    assert "CR-027" in row_254
    assert "PR #113" in row_254
    assert "e497681" in row_254
    assert "selection" in row_254
    assert "selected row/index/content" in row_254
    assert "ui_query" in row_254
    assert "XPath" in row_254
    assert "Source-side owner may close" in row_254

    row = _issue_row(backlog, "#270")
    assert "Target helper slice covered" in row
    assert "broader FR remains open" in row
    assert 'ui_text(action="read")' in row
    assert 'ui_grid(action="snapshot")' in row
    assert "CR-021" in row
    assert "CR-025" in row
    assert "CR-029" in row
    assert "CR-031" in row
    assert "CR-032" in row
    assert "CR-052" in row
    assert "CR-053" in row
    assert "CR-070" in row
    assert "CR-074" in row
    assert 'ui_text(action="set_text")' in row
    assert 'ui_grid(action="viewport")' in row
    assert "ui.grid.ensure_visible" in row
    assert "drop.ensure_visible=true" in row
    assert "row-based drop endpoint" in row
    assert "raw viewport guessing" in row
    assert "viewport_delta.before" in row
    assert "opt-in DataGrid row action ensure-visible composition" in row
    assert "visible-row-only defaults" in row
    assert "ui.text.read" in row
    assert "ui.text.type_replace_selection" in row
    assert "ui.text.get_state" in row
    assert "ui.text.assert_selection" in row
    assert "ui.text.set_text" in row
    assert "ui.grid.viewport" in row
    assert "ui.grid.get_state" in row
    assert "ui.grid.select_row" in row
    assert "ui.grid.click_row" in row
    assert 'ui_grid(action="click_row")' in row
    assert "ui_focus" in row
    assert "confirmed DataGrid selection" in row
    assert "cells" in row
    assert "cell_values" in row
    assert "None in netcoredbg-mcp." not in row

    for issue in ("#271", "#272"):
        row = _issue_row(backlog, issue)
        assert "Schema slice merged" in row
        assert "broader FR remains open" in row
        assert "None in netcoredbg-mcp." not in row


def test_issues_backlog_does_not_close_broad_issue_bodies_from_narrow_slices() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    expected_remaining_terms = {
        "#268": [
            "runtime_smoke_validate_plan",
            "runtime_smoke_run_plan",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_run_probe",
            "runtime_smoke_wait_for_result",
            "CR-045",
            "CR-046",
            "CR-047",
            "YAML",
            'plan_source.format="yaml"',
            "runtime_smoke_validate_probe",
            "read-only probe validation",
            "runner-exception diagnostics",
            "debug.stop",
            "oracle-pack",
            "remaining lifecycle/orchestration closure",
        ],
        "#269": [
            "runtime_smoke_get_result",
            "runtime_smoke_stop",
            "evidence-bundle API",
            "runtime_smoke_run_probe",
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_get_event_delta",
            "CR-016",
            "CR-039",
            "CR-041",
            "CR-046",
            "CR-047",
            "CR-059",
            "CR-060",
            "CR-061",
            "CR-062",
            "CR-063",
            "CR-064",
            "CR-066",
            "CR-069",
            "CR-080",
            "CR-082",
            "CR-083",
            "metrics_contract",
            "agent_mode.defaults",
            "runtime_smoke_validate_probe",
            "read-only probe validation",
            "invalid-probe repair guidance",
            "invalid-run-probe repair guidance",
            "event-delta cursor repair guidance",
            "contextless event-delta guidance",
            "contaminated-final cleanup-contract routing",
            "source_deltas.debug_output",
            "source_deltas.trace_source",
            "source-aware mark-cursor guidance",
            "active app-diagnostics wait/evidence source-cursor guidance",
            "quiet active app-diagnostics delta wait-continuation guidance",
            "source-aware run-probe first-follow-up guidance",
            "exception verdict",
            "cleanup-contract guidance",
            "event_limit=20",
            "NO DATA",
            "agent_mode",
            "broad lifecycle/orchestration closure",
        ],
        "#270": [
            "CR-017",
            "CR-021",
            "CR-025",
            "CR-029",
            "CR-031",
            "CR-032",
            "CR-052",
            "CR-053",
            "CR-054",
            "CR-057",
            "CR-058",
            "CR-074",
            "CR-075",
            "CR-076",
            "CR-077",
            "PR #178",
            "PR #179",
            "PR #180",
            "ui.text.set_text",
            'ui_grid(action="viewport")',
            'ui_grid(action="right_click_row")',
            'ui_grid(action="double_click_row")',
            "ui.grid.viewport",
            "drop.ensure_visible=true",
            "row-based drop endpoint",
            "target-side offscreen row-target drag ensure-visible semantics",
            "bounded broad-#270 replay-proof plus fail-closed source-anchor guard slice",
            "source-anchor-preserving offscreen row-target drag",
            "merged-state replay stabilization",
            "drop-time evidence",
            "fresh automation-element bounds",
            "drop_origin_target",
            "drop_bounds_target",
            "actionable `BLOCKED` verdict when target-side realization hides the drag source",
            "opt-in ensure-visible row actions",
            "ui.right_click_verified",
            "ui.double_click_verified",
            "generic verified right/double-click slice",
            "right_click_row",
            "double_click_row",
            "DataGrid offscreen/scroll action semantics",
        ],
        "#271": [
            "CR-019",
            "CR-042",
            "debug_preflight",
            "tracepoint guard",
            "cleanup contract",
            "mark_trace_cursor",
            "get_trace_delta",
            "CR-046",
            "CR-048",
            "debug.stop",
            "process-registry assertion",
            "app diagnostics live-target freshness",
            "broader diagnostics orchestration",
        ],
        "#272": [
            "app diagnostics",
            "oracle_pack",
            "runtime_smoke_wait_for_result",
            "CR-024",
            "CR-026",
            "CR-033",
            "CR-048",
            "CR-050",
            "CR-051",
            "CR-067",
            "CR-078",
            "CR-079",
            "CR-080",
            "DISAGREEING_SOURCES",
            "launch env/evidence-dir advertisement",
            "launch-to-artifact default acquisition",
            "launch-directory fallback",
            "evidence-directory poll",
            "wait-json condition semantics",
            "case-boundary live app-diagnostics history",
            "intra-case wait/poll progress",
            "active app-diagnostics wait/evidence source-cursor guidance",
            "remaining broader app diagnostics lifecycle/orchestration",
        ],
    }

    for issue, terms in expected_remaining_terms.items():
        row = _issue_row(backlog, issue)
        for term in terms:
            assert term in row

    _, _status, _evidence, remaining_250 = _issue_cells(backlog, "#250")
    assert "None in netcoredbg-mcp." in remaining_250
    assert "source-side" in remaining_250.lower()
    assert "fresh Engram `#250`" not in remaining_250

    _, _status, _evidence, remaining_269 = _issue_cells(backlog, "#269")
    assert "profile defaults" not in remaining_269
    assert "full `agent_mode=true` profile defaults" not in remaining_269
    assert "generic probe UX" in remaining_269
    assert "multi-source event deltas" in remaining_269


def test_issues_backlog_has_cr022_lifecycle_refresh_for_open_broad_rows() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    assert "## CR-022 Issue Lifecycle Refresh" in backlog
    assert "fresh engram issue/comment reads on 2026-06-17" in backlog.lower()

    expected_decisions = {
        "#226": [
            "resolved target-side",
            "CR-035",
            "downstream replay",
            "visible-row-drag-reorder",
            "edge-scroll-drag-reorder",
            "multi-row-drag-reorder",
            "invalid-drop-noop",
            "process_registry_after=0",
        ],
        "#250": [
            "resolved target-side",
            "CR-021 focus proof",
            "selection confirmation",
            "CR-037",
            "selected-item semantics",
            "SelectionItemPattern",
            "CR-040",
            "screenshot orientation",
            "CR-043",
            "focused-element query",
            "get_focused_element",
            "source-side NovaScript verification",
        ],
        "#254": [
            "resolved target-side",
            "CR-027 / PR #113",
            "selected row/index/content",
            "ui_query",
            "post-merge",
            "e497681",
            "None in netcoredbg-mcp",
        ],
        "#268": [
            "commented",
            "runtime_smoke_validate_plan",
            "runtime_smoke_run_plan",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_run_probe",
            "runtime_smoke_wait_for_result",
            "CR-045",
            "CR-046",
            "CR-047",
            "YAML",
            'plan_source.format="yaml"',
            "runtime_smoke_validate_probe",
            "runner-exception diagnostics",
            "broad orchestration",
        ],
        "#269": [
            "commented",
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_get_event_delta",
            "agent_mode",
            "CR-039",
            "CR-041",
            "CR-046",
            "CR-047",
            "metrics_contract",
            "agent_mode.defaults",
            "runtime_smoke_validate_probe",
            "success-metrics evidence",
            "CR-080",
            "active app-diagnostics wait/evidence source-cursor guidance",
            "CR-082",
            "quiet active app-diagnostics delta wait-continuation guidance",
            "CR-083",
            "source-aware run-probe first-follow-up guidance",
            "exception verdict",
            "broad lifecycle/orchestration",
        ],
        "#270": [
            "commented",
            "ui.text.read",
            "ui.text.get_state",
            "ui.text.assert_selection",
            "CR-029",
            "CR-031",
            "CR-032",
            "CR-052",
            "CR-053",
            "CR-057",
            "CR-058",
            "CR-074",
            "CR-075",
            "CR-076",
            "CR-077",
            "PR #178",
            "PR #179",
            "PR #180",
            "ui.text.set_text",
            'ui_grid(action="viewport")',
            'ui_grid(action="right_click_row")',
            'ui_grid(action="double_click_row")',
            "ui.grid.viewport",
            "ui.grid.right_click_row",
            "ui.grid.double_click_row",
            "grid_right_click_row",
            "grid_double_click_row",
            "drop.ensure_visible=true",
            "row-based drop endpoint",
            "raw viewport guessing",
            "opt-in ensure-visible composition",
            "visible-row-only defaults",
            "visible-row-only DataGrid",
            "bridge-owned",
            "ui_focus",
            "confirmed DataGrid selection",
            "bounded visible-row identity snapshots",
            "source-anchor-preserving offscreen row-target drag",
            "merged-state replay stabilization",
            "drop-time evidence",
            "fresh automation-element bounds",
            "drop_origin_target",
            "drop_bounds_target",
            "DataGrid offscreen/scroll action semantics",
        ],
        "#271": [
            "commented",
            "CR-020",
            "CR-042",
            "CR-046",
            "CR-048",
            "mark_trace_cursor",
            "get_trace_delta",
            "cleanup_contract",
            "debug.stop",
            "process-registry assertion",
            "contaminated",
            "single-flight",
            "app diagnostics live-target freshness",
            "PDB/process proof",
        ],
        "#272": [
            "commented",
            "oracle_pack",
            "app_diagnostics",
            "runtime_smoke_wait_for_result",
            "app_diagnostics.poll",
            "app_diagnostics.wait_json",
            "DISAGREEING_SOURCES",
            "CR-033",
            "CR-048",
            "CR-050",
            "CR-051",
            "launch-to-artifact default acquisition",
            "PDB/process proof",
            "wait_json.condition",
            "CR-079",
            "intra-case live app-diagnostics progress",
            "active `wait_json` / `poll` acquisition",
            "before case completion",
            "CR-080",
            "active app-diagnostics wait/evidence source-cursor guidance",
        ],
    }

    for issue, terms in expected_decisions.items():
        decision_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", issue)
        for term in terms:
            assert term in decision_row, f"term {term!r} missing from {issue} CR-022 row"


def test_cr055_lifecycle_reconciliation_records_resolved_target_issues() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    replay = json.loads(REPLAY_PACKET_JSON.read_text(encoding="utf-8"))

    assert replay["status"] == "DOWNSTREAM_REPLAY_PASS"
    assert replay["latest_replay"]["status"] == "PASS"
    assert replay["latest_replay"]["cleanup"]["status"] == "PASS"
    assert replay["latest_replay"]["cleanup"]["process_registry_after"] == 0
    assert replay["latest_replay"]["issue_226_lifecycle_decision"] == "target_resolved"

    for issue in ("#226", "#250", "#267"):
        row = _issue_row(backlog, issue)
        lifecycle_row = _section_issue_row(
            backlog,
            "## CR-022 Issue Lifecycle Refresh",
            issue,
        )

        assert "resolved" in row
        assert "resolved target-side" in lifecycle_row
        assert "Source-side owner may close" in row


def test_issue_272_records_cr024_diagnostic_orchestration_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")

    assert "diagnostic orchestration source slice covered" in row
    assert "CR-024" in row
    assert "oracle_pack.sources" in row
    assert "DISAGREEING_SOURCES" in row
    assert "app_diagnostics.wait_json" in row
    assert "poll" in row
    assert "broader FR remains open" in row
    assert "before closing the full FR" in row


def test_issue_272_records_cr026_launch_contract_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")

    assert "launch contract slice covered" in row
    assert "CR-026" in row
    assert "app diagnostics launch env/evidence-dir advertisement" in row
    assert "redacted env values" in row
    assert "broader FR remains open" in row


def test_issue_272_records_cr033_launch_orchestration_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")

    assert "launch-orchestrated acquisition slice covered" in row
    assert "CR-033" in row
    assert "diagnostic_launch.evidence.path" in row
    assert "explicit `wait_json` / `poll`" in row
    assert "broader FR remains open" in row


def test_issue_272_records_cr050_cr051_app_diagnostics_source_slices() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")

    for text in (row, lifecycle_row):
        assert "CR-050" in text
        assert "CR-051" in text
        assert "evidence-directory" in text
        assert "file-name pattern" in text or "poll pattern" in text
        assert "matched-candidate revalidation" in text or "matched-file revalidation" in text
        assert "wait_json.condition" in text
        assert "JSONPath equality" in text or "JSONPath equality waiting" in text

    assert "beyond launch-to-artifact default acquisition" in remaining
    assert "directory poll" in remaining
    assert "wait-json condition semantics" in remaining
    assert "broader app diagnostics lifecycle/orchestration" in remaining


def test_issue_272_records_cr056_app_diagnostics_poll_cursor_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-056" in text
        assert "app_diagnostics.poll.since" in text
        assert "(mtime_ns, name)" in text
        assert "stale/equal" in text
        assert "cursor" in text

    assert "poll cursor" in remaining
    assert "poll cursor" in lifecycle_remaining
    assert "broader app diagnostics lifecycle/orchestration" in remaining
    assert "broader app diagnostics lifecycle/orchestration" in lifecycle_remaining


def test_issue_272_records_cr067_launch_directory_fallback_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-067" in text
        assert "launch-directory fallback" in text
        assert (
            "explicit `wait_json` / `poll` precedence" in text
            or "explicit source precedence" in text
        )
        assert "stale pre-existing directory artifacts" in text

    assert "launch-directory fallback" in remaining
    assert "launch-directory fallback" in lifecycle_remaining


def test_issue_269_records_cr073_app_diagnostics_event_delta_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#269")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#269")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#269")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-073" in text
        assert "include_app_diagnostics=True" in text
        assert "source delta" in text
        assert "retained-final-result" in text or "retained final result" in text
        assert "live diagnostics history" in text or "live diagnostics streaming" in text

    assert "retained-final-result app diagnostics" in remaining
    assert "retained-final-result app diagnostics" in lifecycle_remaining


def test_issue_272_records_cr073_app_diagnostics_event_delta_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-073" in text
        assert "include_app_diagnostics=True" in text
        assert "source delta" in text
        assert "retained-final-result" in text or "retained final result" in text
        assert "without claiming live diagnostics streaming" in text

    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in remaining
    )
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in lifecycle_remaining
    )


def test_issue_272_records_cr078_live_app_diagnostics_history_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-078" in text
        assert "case-boundary live app-diagnostics history" in text
        assert "active runtime-smoke runs" in text
        assert "runtime_smoke_mark_event_cursor(include_app_diagnostics=True)" in text
        assert "runtime_smoke_get_event_delta" in text

    assert "case-boundary live app-diagnostics history" not in remaining
    assert "case-boundary live app-diagnostics history" not in lifecycle_remaining
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in remaining
    )
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in lifecycle_remaining
    )
    assert "broader app diagnostics lifecycle/orchestration" in remaining
    assert "broader app diagnostics lifecycle/orchestration" in lifecycle_remaining


def test_issue_272_records_cr079_intracase_app_diagnostics_progress_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-079" in text
        assert "intra-case live app-diagnostics progress" in text
        assert "active `wait_json` / `poll` acquisition" in text
        assert "before case completion" in text
        assert "include_app_diagnostics=True" in text
        assert "event-delta source" in text

    assert "intra-case wait/poll progress" not in remaining
    assert "intra-case wait/poll progress" not in lifecycle_remaining
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in remaining
    )
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in lifecycle_remaining
    )
    assert "broader app diagnostics lifecycle/orchestration" in remaining
    assert "broader app diagnostics lifecycle/orchestration" in lifecycle_remaining


def test_issue_269_272_record_cr080_active_wait_evidence_delta_guidance_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    for issue in ("#269", "#272"):
        row = _issue_row(backlog, issue)
        lifecycle_row = _section_issue_row(
            backlog,
            "## CR-022 Issue Lifecycle Refresh",
            issue,
        )
        for text in (row, lifecycle_row):
            assert "CR-080" in text
            assert "active app-diagnostics wait/evidence source-cursor guidance" in text
            assert "runtime_smoke_wait_for_result(agent_mode=True)" in text
            assert "runtime_smoke_evidence_bundle(agent_mode=True)" in text
            assert "runtime_smoke_get_event_delta" in text

    _issue, _state, _evidence, remaining_269 = _issue_cells(backlog, "#269")
    _issue, _state, _evidence, remaining_272 = _issue_cells(backlog, "#272")
    lifecycle_row_272 = _section_issue_row(
        backlog,
        "## CR-022 Issue Lifecycle Refresh",
        "#272",
    )
    _life_issue, _life_state, _life_evidence, lifecycle_remaining_272 = (
        cell.strip() for cell in lifecycle_row_272.strip().strip("|").split("|")
    )

    assert "active app-diagnostics wait/evidence source-cursor guidance" not in remaining_269
    assert "remaining multi-source event deltas" in remaining_269
    assert "broad lifecycle/orchestration closure" in remaining_269
    assert "active app-diagnostics wait/evidence source-cursor guidance" not in remaining_272
    assert "broader app diagnostics lifecycle/orchestration" in remaining_272
    assert "broader app diagnostics lifecycle/orchestration" in lifecycle_remaining_272


def test_issue_269_records_cr082_quiet_active_delta_wait_guidance_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#269")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#269")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#269")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-082" in text
        assert "quiet active app-diagnostics delta wait-continuation guidance" in text
        assert "runtime_smoke_get_event_delta(agent_mode=True)" in text
        assert "runtime_smoke_wait_for_result" in text
        assert "empty delta poll" in text

    assert "broader FR remains open" in row
    assert "keep open" in lifecycle_row
    assert "quiet active app-diagnostics delta wait-continuation guidance" not in remaining
    assert (
        "quiet active app-diagnostics delta wait-continuation guidance"
        not in lifecycle_remaining
    )
    assert "remaining multi-source event deltas" in remaining
    assert "broad lifecycle/orchestration" in remaining
    assert "broad lifecycle/orchestration" in lifecycle_remaining


def test_issue_269_records_cr083_run_probe_follow_up_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#269")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#269")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#269")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-083" in text
        assert "source-aware run-probe first-follow-up guidance" in text
        assert "runtime_smoke_run_probe(agent_mode=True)" in text
        assert "runtime_smoke_get_event_delta" in text
        assert "first follow-up" in text
        assert "preserved source cursor context" in text
        assert "event_cursor.sources" in text
        assert "app-diagnostics acquisition" not in text
        assert "#270" not in text
        assert "DataGrid" not in text

    assert "broader FR remains open" in row
    assert "keep open" in lifecycle_row
    assert "source-aware run-probe first-follow-up guidance" not in remaining
    assert "source-aware run-probe first-follow-up guidance" not in lifecycle_remaining
    assert "generic probe UX" in remaining
    assert "generic probe UX" in lifecycle_remaining
    assert "broad lifecycle/orchestration" in remaining
    assert "broad lifecycle/orchestration" in lifecycle_remaining


def test_issue_270_records_cr070_ensure_visible_viewport_delta_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-070" in text
        assert "viewport_delta.before" in text
        assert "comparison" in text
        assert "ui.grid.ensure_visible" in text
        assert "ensure_visible_result" in text

    assert "viewport-delta evidence" in remaining
    assert "viewport-delta evidence" in lifecycle_remaining
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining


def test_issue_270_records_cr071_assert_range_slice_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-071" in text
        assert 'ui_grid(action="assert_range")' in text
        assert "ui.grid.assert_range" in text
        assert "selector/start_index/end_index" in text

    assert "assert-range parity" in remaining
    assert "assert-range parity" in lifecycle_remaining
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining


def test_issue_270_records_cr072_drag_ensure_visible_slice_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-072" in text
        assert "ui.drag" in text
        assert "ensure-visible preflight" in text
        assert "row_index" in text
        assert "row_identity" in text
        assert "inline docs parity" in text

    assert "drag-source ensure-visible preflight" in remaining
    assert "drag-source ensure-visible preflight" in lifecycle_remaining
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining


def test_issue_270_records_cr074_target_drop_ensure_visible_slice_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-074" in text
        assert "target-side" in text
        assert "offscreen row" in text
        assert "row-based drop endpoint" in text
        assert "drop.ensure_visible=true" in text
        assert "raw viewport guessing" in text

    assert "target-side offscreen row-target drag ensure-visible semantics" in remaining
    assert (
        "target-side offscreen row-target drag ensure-visible semantics"
        in lifecycle_remaining
    )
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining


def test_issue_270_records_cr075_docs_evidence_replay_proof_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-075" in text
        assert "bounded broad-#270 replay-proof plus fail-closed source-anchor guard slice" in text
        assert (
            "docs/examples/runtime-smoke-v2-drag-drop-grid.json" in text
            or "public example" in text
        )
        assert (
            "docs/PRODUCTION-TESTING-PLAYBOOK.md" in text
            or "production playbook" in text
        )
        assert "docs guard tests" in text
        assert "runtime-smoke manual scenario" in text
        assert "adapter guard" in text
        assert "offscreen row-target" in text
        assert "drop.ensure_visible=true" in text
        assert "bounded route" in text or "route evidence" in text
        assert "viewport evidence" in text or "ui.grid.viewport" in text
        assert "actionable `BLOCKED` verdict" in text
        assert (
            "without closing broad `#270`" in text
            or "broad closure" in text
            or "keep open or split" in text
        )

    expected_remaining = (
        "live downstream replay tails beyond the CR-075/CR-076/CR-077 replay, "
        "stability, and drop-time evidence contract"
    )
    assert expected_remaining in remaining
    assert expected_remaining in lifecycle_remaining
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining


def test_issue_270_records_cr076_cr077_replay_reconciliation_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#270")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#270")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#270")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    for text in (row, lifecycle_row):
        assert "CR-076 / PR #178" in text
        assert "PR #179" in text
        assert "CR-077 / PR #180" in text
        assert "source-anchor-preserving offscreen row-target drag" in text
        assert "merged-state replay stabilization" in text
        assert "drop-time evidence" in text
        assert "fresh automation-element bounds" in text
        assert "drop_origin_target" in text
        assert "drop_bounds_target" in text
        assert "Fixture cue nineteen" in text

    expected_remaining = (
        "live downstream replay tails beyond the CR-075/CR-076/CR-077 replay, "
        "stability, and drop-time evidence contract"
    )
    assert expected_remaining in remaining
    assert expected_remaining in lifecycle_remaining
    assert "broader" in remaining
    assert "broader" in lifecycle_remaining
    assert "before closing" in remaining
    assert "before closing" in lifecycle_remaining


def test_issue_271_272_record_cr048_app_diagnostics_freshness_slice() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row_271 = _issue_row(backlog, "#271")
    row_272 = _issue_row(backlog, "#272")

    for row in (row_271, row_272):
        assert "CR-048" in row
        assert "app diagnostics live-target freshness" in row
        assert "PDB/process proof" in row
        assert "broader FR remains open" in row

    _issue, _state, _evidence, remaining_271 = _issue_cells(backlog, "#271")
    _issue, _state, _evidence, remaining_272 = _issue_cells(backlog, "#272")

    assert "live-target PDB/process proof" not in remaining_271
    assert "PDB/process proof" not in remaining_272
    assert "broader diagnostics orchestration" in remaining_271
    assert "broader app diagnostics lifecycle/orchestration" in remaining_272


def test_issue_272_remaining_scope_excludes_covered_launch_contract_and_default() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#272")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#272")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    assert "diagnostic env/evidence-dir advertisement" not in remaining
    assert "launch env/evidence-dir advertisement" not in remaining
    assert "app diagnostics orchestration beyond local-file acquisition" not in remaining
    assert "launch-to-artifact default acquisition" in remaining
    assert "directory poll" in remaining
    assert "poll cursor" in remaining
    assert "broader app diagnostics lifecycle/orchestration" in remaining
    assert "wait-json condition semantics" in remaining
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in remaining
    )
    assert "diagnostic env/evidence-dir advertisement" not in lifecycle_remaining
    assert "launch env/evidence-dir advertisement" not in lifecycle_remaining
    assert "app diagnostics orchestration beyond local-file acquisition" not in lifecycle_remaining
    assert "launch-to-artifact default acquisition" in lifecycle_remaining
    assert "directory poll" in lifecycle_remaining
    assert "poll cursor" in lifecycle_remaining
    assert "wait-json condition semantics" in lifecycle_remaining
    assert (
        "broader live app-diagnostics streaming/history beyond bounded case-boundary "
        "history, intra-case wait/poll acquisition progress, and active wait/evidence "
        "cursor handoff"
        in lifecycle_remaining
    )


def test_issue_268_records_plan_path_slices() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#268")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#268")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#268")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    assert "CR-044" in row
    assert "plan_path" in row
    assert "plan_source" in row
    assert "CR-045" in row
    assert "CR-046" in row
    assert "YAML" in row
    assert 'plan_source.format="yaml"' in row
    assert "runner-exception diagnostics" in row
    assert "debug.stop" in row
    assert "contamination guidance" in row
    assert "runtime_smoke_validate_plan" in row
    assert "runtime_smoke_run_plan" in row
    assert "missing, mixed, malformed, non-object, or path-validation failures" in row
    assert "CR-044" in lifecycle_row
    assert "plan_path" in lifecycle_row
    assert "plan_source" in lifecycle_row
    assert "CR-045" in lifecycle_row
    assert "CR-046" in lifecycle_row
    assert "YAML" in lifecycle_row
    assert 'plan_source.format="yaml"' in lifecycle_row
    assert "runner-exception diagnostics" in lifecycle_row
    assert "plan_path input" not in remaining
    assert "plan_path input" not in lifecycle_remaining
    assert "YAML/v3 authoring" not in remaining
    assert "YAML/v3 authoring" not in lifecycle_remaining
    assert "v3 authoring" in remaining
    assert "v3 authoring" in lifecycle_remaining


def test_issue_268_269_record_validate_probe_slice_without_broad_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    for issue in ("#268", "#269"):
        row = _issue_row(backlog, issue)
        lifecycle_row = _section_issue_row(
            backlog,
            "## CR-022 Issue Lifecycle Refresh",
            issue,
        )
        _issue, _state, _evidence, remaining = _issue_cells(backlog, issue)
        _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
            cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
        )

        assert "CR-047" in row
        assert "CR-047" in lifecycle_row
        assert "runtime_smoke_validate_probe" in row
        assert "runtime_smoke_validate_probe" in lifecycle_row
        assert "read-only probe validation" in row
        assert "read-only probe validation" in lifecycle_row
        assert "generic probe UX" in remaining
        assert "generic probe UX" in lifecycle_remaining

    row_269 = _issue_row(backlog, "#269")
    lifecycle_row_269 = _section_issue_row(
        backlog,
        "## CR-022 Issue Lifecycle Refresh",
        "#269",
    )
    assert "CR-059" in row_269
    assert "CR-059" in lifecycle_row_269
    assert "CR-060" in row_269
    assert "CR-060" in lifecycle_row_269
    assert "CR-061" in row_269
    assert "CR-061" in lifecycle_row_269
    assert "CR-062" in row_269
    assert "CR-062" in lifecycle_row_269
    assert "CR-063" in row_269
    assert "CR-063" in lifecycle_row_269
    assert "CR-064" in row_269
    assert "CR-064" in lifecycle_row_269
    assert "CR-066" in row_269
    assert "CR-066" in lifecycle_row_269
    assert "CR-069" in row_269
    assert "CR-069" in lifecycle_row_269
    assert "invalid-probe repair guidance" in row_269
    assert "invalid-probe repair guidance" in lifecycle_row_269
    assert "invalid-run-probe repair guidance" in row_269
    assert "invalid-run-probe repair guidance" in lifecycle_row_269
    assert "event-delta cursor repair guidance" in row_269
    assert "event-delta cursor repair guidance" in lifecycle_row_269
    assert "contextless event-delta guidance" in row_269
    assert "contextless event-delta guidance" in lifecycle_row_269
    assert "contaminated-final cleanup-contract routing" in row_269
    assert "contaminated-final cleanup-contract routing" in lifecycle_row_269
    assert "source_deltas.debug_output" in row_269
    assert "source_deltas.debug_output" in lifecycle_row_269
    assert "source_deltas.trace_source" in row_269
    assert "source_deltas.trace_source" in lifecycle_row_269
    assert "source-aware mark-cursor guidance" in row_269
    assert "source-aware mark-cursor guidance" in lifecycle_row_269


def test_issue_271_records_cleanup_and_trace_delta_slices() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    row = _issue_row(backlog, "#271")
    lifecycle_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", "#271")
    _issue, _state, _evidence, remaining = _issue_cells(backlog, "#271")
    _life_issue, _life_state, _life_evidence, lifecycle_remaining = (
        cell.strip() for cell in lifecycle_row.strip().strip("|").split("|")
    )

    assert "CR-028" in row
    assert "cleanup contamination contract" in row
    assert "runtime_smoke_cleanup_contract" in row
    assert "contaminated-state surfacing" in row
    assert "CR-042" in row
    assert "CR-046" in row
    assert "TracepointManager.mark_trace_cursor" in row
    assert "TracepointManager.get_trace_delta" in row
    assert "public read-only `mark_trace_cursor`" in row
    assert "public read-only `get_trace_delta`" in row
    assert "debug.stop" in row
    assert "process-registry assertion" in row
    assert "broader" in row
    assert "CR-028" in lifecycle_row
    assert "cleanup_contract" in lifecycle_row
    assert "runtime_smoke_cleanup_contract" in lifecycle_row
    assert "CR-036" in row
    assert "CR-036" in lifecycle_row
    assert "active_run_id" in row
    assert "active_status" in row
    assert "run_created=false" in row
    assert "STOPPING" in lifecycle_row
    assert "evidence_bundle" in lifecycle_row
    assert "CR-042" in lifecycle_row
    assert "CR-046" in lifecycle_row
    assert "TracepointManager.mark_trace_cursor" in lifecycle_row
    assert "TracepointManager.get_trace_delta" in lifecycle_row
    assert "debug.stop" in lifecycle_row
    assert "process-registry assertion" in lifecycle_row
    assert "trace-specific cursor/delta APIs" not in remaining
    assert "trace-specific cursor/delta APIs" not in lifecycle_remaining
    assert "broader diagnostics orchestration" in remaining
    assert "live-target PDB/process proof" not in remaining
    assert "freshness paths" in remaining


def test_cr022_broad_issues_require_split_or_comment_evidence_before_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    for issue in ("#268", "#269", "#270", "#271", "#272"):
        _, status, evidence, remaining_action = _issue_cells(backlog, issue)
        lifecycle_text = " ".join((status, evidence, remaining_action)).lower()

        assert "none in netcoredbg-mcp." not in lifecycle_text
        assert "broader" in lifecycle_text
        assert "remaining" in lifecycle_text

        if "closed" in status.lower() or "resolved" in status.lower():
            assert "engram comment" in lifecycle_text
            assert "split follow-up issue" in lifecycle_text
            assert "closure evidence" in lifecycle_text
        else:
            assert "remains open" in status.lower()
            assert "keep" in remaining_action.lower()
            assert "split" in remaining_action.lower() or "comment" in remaining_action.lower()
            assert "before closing" in remaining_action.lower()


def test_cr022_issue_226_requires_downstream_pass_before_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)
    packet = json.loads(REPLAY_PACKET_JSON.read_text(encoding="utf-8"))

    _, status, evidence, remaining_action = _issue_cells(backlog, "#226")
    lifecycle_text = " ".join((status, evidence, remaining_action)).lower()

    if "closed" in status.lower() or "resolved" in status.lower():
        assert packet["latest_replay"]["status"] == "PASS"
        assert packet["latest_replay"]["issue_226_lifecycle_decision"] == "target_resolved"
        assert "downstream replay `pass`" in lifecycle_text
        assert "source-side owner may close" in lifecycle_text
    else:
        assert status == "Downstream replay `BLOCKED`"
        assert packet["latest_replay"]["status"] == "BLOCKED"
        assert packet["latest_replay"]["issue_226_lifecycle_decision"] == "leave_open"
        assert "keep open" in remaining_action.lower()
        assert "target-side" in backlog.lower()
        assert "not enough to close" in backlog.lower()

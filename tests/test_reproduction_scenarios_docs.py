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


def _issue_cells(backlog: str, issue: str) -> tuple[str, str, str, str]:
    row = _issue_row(backlog, issue)
    cells = tuple(cell.strip() for cell in row.strip().strip("|").split("|"))
    assert len(cells) == 4
    return cells


def _section_issue_row(backlog: str, heading: str, issue: str) -> str:
    in_section = False
    for line in backlog.splitlines():
        if line.startswith("## "):
            in_section = line == heading
            continue
        if not in_section:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == f"`{issue}`":
            return line
    raise AssertionError(f"missing {heading} row for {issue}")


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
        "downward_edge_scroll",
        "upward_edge_scroll",
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
    assert payload["status"] == "DOWNSTREAM_REPLAY_BLOCKED"
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
        "downward_edge_scroll",
        "upward_edge_scroll",
        "multi_row_drag",
        "invalid_drop_noop_or_cancel",
    }
    assert "PARTIAL_PASS_INVALID_FOR_GATE" in " ".join(payload["known_invalid_evidence"])
    assert payload["latest_replay"]["status"] == "BLOCKED"
    assert payload["latest_replay"]["runtime_smoke"]["baseline_status"] == "PASS"
    assert (
        payload["latest_replay"]["runtime_smoke"]["reason"]
        == "drag source row identity not visible"
    )
    assert payload["latest_replay"]["requested_source_identity"] == "ROW-008-UNIQUE-PHRASE"
    assert payload["latest_replay"]["visible_viewport"] == {
        "first_visible_index": 26,
        "last_visible_index": 43,
        "first_visible_identity": "ROW-027-UNIQUE-PHRASE",
        "last_visible_identity": "ROW-044-UNIQUE-PHRASE",
        "selected_identity": "ROW-031-UNIQUE-PHRASE",
    }
    assert payload["latest_replay"]["issue_226_lifecycle_decision"] == "leave_open"
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
    assert "target-side v0.17.2 evidence is not enough" in backlog.lower()
    assert "DOWNSTREAM_REPLAY_BLOCKED" in backlog


def test_issues_backlog_current_status_is_not_stale_red_queue() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    assert "## Current Issue Status" in backlog
    assert "## Historical RED Proof Commands" in backlog
    assert "## Executable RED Scenarios" not in backlog
    assert "## Blocked Or Spec-Needed Scenarios" not in backlog
    assert "SpecKit needed" not in backlog
    assert "`#226` | Downstream replay `BLOCKED`" in backlog

    for issue in (
        "#251",
        "#264",
        "#265",
        "#266",
        "#267",
    ):
        assert f"`{issue}` | Target evidence merged" in backlog

    for issue in ("#250", "#254", "#268", "#269"):
        row = _issue_row(backlog, issue)
        assert "Target slice merged" in row
        assert "broader" in row
        assert "None in netcoredbg-mcp." not in row

    row_250 = _issue_row(backlog, "#250")
    assert "CR-021" in row_250
    assert "focus proof" in row_250
    assert "selection confirmation" in row_250

    row_254 = _issue_row(backlog, "#254")
    assert "CR-021" in row_254
    assert "selection" in row_254
    assert "selected row/index/content" in row_254

    row = _issue_row(backlog, "#270")
    assert "Target helper slice covered" in row
    assert "broader FR remains open" in row
    assert 'ui_text(action="read")' in row
    assert 'ui_grid(action="snapshot")' in row
    assert "CR-021" in row
    assert "ui.text.read" in row
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
        "#250": ["focus", "selected-item", "screenshot-orientation"],
        "#254": ["ui_query", "selected row/index/content"],
        "#268": [
            "runtime_smoke_validate_plan",
            "runtime_smoke_run_plan",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_run_probe",
            "runtime_smoke_wait_for_result",
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
            "agent_mode",
            "broad lifecycle/orchestration closure",
        ],
        "#270": ["CR-017", "CR-021", "ui_property", "TextBox mutation/set-text"],
        "#271": ["CR-019", "debug_preflight", "tracepoint guard", "cleanup contract"],
        "#272": [
            "app diagnostics",
            "oracle_pack",
            "runtime_smoke_wait_for_result",
            "remaining app diagnostics orchestration",
        ],
    }

    for issue, terms in expected_remaining_terms.items():
        row = _issue_row(backlog, issue)
        for term in terms:
            assert term in row

    assert "do not close the full Engram" in backlog


def test_issues_backlog_has_cr022_lifecycle_refresh_for_open_broad_rows() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    assert "## CR-022 Issue Lifecycle Refresh" in backlog
    assert "fresh Engram issue/comment reads on 2026-06-17" in backlog

    expected_decisions = {
        "#226": [
            "leave open",
            "NovaScript CR-003 replay",
            "drag source row visibility",
        ],
        "#250": [
            "commented",
            "CR-021 focus proof",
            "selection confirmation",
            "selected-item semantics",
            "screenshot orientation",
        ],
        "#254": [
            "commented",
            "selected row/index/content",
            "ui_query consistency",
        ],
        "#268": [
            "commented",
            "runtime_smoke_validate_plan",
            "runtime_smoke_run_plan",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_run_probe",
            "runtime_smoke_wait_for_result",
            "broad orchestration",
        ],
        "#269": [
            "commented",
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_get_event_delta",
            "agent_mode",
            "broad lifecycle/orchestration",
        ],
        "#270": [
            "commented",
            "ui.text.read",
            "ui_focus",
            "confirmed DataGrid selection",
            "ui_property",
            "TextBox mutation/set-text",
        ],
        "#271": [
            "comment already current",
            "CR-020",
            "trace cursor/delta",
            "cleanup_contract",
            "contaminated",
            "single-flight",
        ],
        "#272": [
            "commented",
            "oracle_pack",
            "app_diagnostics",
            "runtime_smoke_wait_for_result",
            "app_diagnostics.poll",
            "app_diagnostics.wait_json",
            "DISAGREEING_SOURCES",
        ],
    }

    for issue, terms in expected_decisions.items():
        decision_row = _section_issue_row(backlog, "## CR-022 Issue Lifecycle Refresh", issue)
        for term in terms:
            assert term in decision_row


def test_cr022_broad_issues_require_split_or_comment_evidence_before_closure() -> None:
    backlog = _read(BACKLOG_SCENARIOS)

    for issue in ("#250", "#254", "#268", "#269", "#270", "#271", "#272"):
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
        assert packet["latest_replay"]["issue_226_lifecycle_decision"] == "close"
        assert "downstream replay pass" in lifecycle_text
    else:
        assert status == "Downstream replay `BLOCKED`"
        assert packet["latest_replay"]["status"] == "BLOCKED"
        assert packet["latest_replay"]["issue_226_lifecycle_decision"] == "leave_open"
        assert "keep open" in remaining_action.lower()
        assert "target-side" in backlog.lower()
        assert "not enough to close" in backlog.lower()

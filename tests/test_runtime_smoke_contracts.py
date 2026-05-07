"""Shared runtime smoke contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from netcoredbg_mcp.response import build_response, extend_next_actions
from netcoredbg_mcp.session.state import (
    DebugState,
    EvidenceRef,
    SmokeResultSummary,
    TerminalStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_terminal_status_accepts_only_smoke_result_vocabulary() -> None:
    assert TerminalStatus("PASS") is TerminalStatus.PASS
    assert TerminalStatus("FAIL") is TerminalStatus.FAIL
    assert TerminalStatus("BLOCKED") is TerminalStatus.BLOCKED
    assert TerminalStatus("IMPASSE") is TerminalStatus.IMPASSE

    with pytest.raises(ValueError):
        TerminalStatus("SKIPPED")


def test_evidence_references_are_immutable_boundary_values() -> None:
    evidence = EvidenceRef(kind="output", ref="output:1", summary="3 matched lines")

    with pytest.raises(FrozenInstanceError):
        evidence.summary = "mutated"


def test_compact_summary_serializes_without_session_objects() -> None:
    summary = SmokeResultSummary(
        status=TerminalStatus.FAIL,
        reason="missing required output",
        elapsed=1.25,
        action_count=3,
        failed_assertions=("required pattern not found",),
        cleanup={"status": "PASS"},
        evidence_refs=(
            EvidenceRef(
                kind="output",
                ref="output:checkpoint-1",
                summary="searched 10 lines",
                count=10,
            ),
        ),
    )

    assert summary.to_dict() == {
        "status": "FAIL",
        "reason": "missing required output",
        "elapsed": 1.25,
        "action_count": 3,
        "failed_assertions": ["required pattern not found"],
        "cleanup": {"status": "PASS"},
        "evidence_refs": [
            {
                "kind": "output",
                "ref": "output:checkpoint-1",
                "summary": "searched 10 lines",
                "count": 10,
            }
        ],
    }


def test_compact_summary_rejects_mutable_boundary_inputs() -> None:
    summary = SmokeResultSummary(
        status=TerminalStatus.PASS,
        reason="complete",
        elapsed=0.5,
        action_count=1,
        failed_assertions=["a list should become a tuple"],
        cleanup={"status": "PASS"},
        evidence_refs=[
            EvidenceRef(kind="ui", ref="snapshot:1", summary="1 selected row"),
        ],
    )

    assert isinstance(summary.failed_assertions, tuple)
    assert isinstance(summary.evidence_refs, tuple)
    assert summary.to_dict()["failed_assertions"] == ["a list should become a tuple"]


def test_extend_next_actions_adds_smoke_actions_without_changing_base_actions() -> None:
    actions = extend_next_actions(
        DebugState.IDLE,
        ["debug_hygiene_preflight", "start_debug"],
    )

    assert actions.count("start_debug") == 1
    assert "debug_hygiene_preflight" in actions
    assert build_response(state=DebugState.IDLE)["next_actions"] == [
        "start_debug",
        "attach_debug",
        "get_progress",
    ]


def test_wpf_one_call_runtime_smoke_scenario_is_inventory_visible() -> None:
    smoke = (REPO_ROOT / "tests" / "smoke_test_manual.py").read_text(encoding="utf-8")

    assert "WPF ONE-CALL RUNTIME SMOKE WORKFLOW" in smoke
    assert "run_runtime_smoke" in smoke
    forbidden_manual_fallbacks = (
        "ui_grid(",
        "ui_toggle(",
        "ui_get_window_tree(",
        "output_checkpoint(",
        "ui_focus",
    )
    scenario_start = smoke.index("WPF ONE-CALL RUNTIME SMOKE WORKFLOW")
    scenario_end = smoke.index("async def test_avalonia_ui_fixture_compatibility", scenario_start)
    scenario_body = smoke[scenario_start:scenario_end]
    assert all(token not in scenario_body for token in forbidden_manual_fallbacks)
    assert 'terminal_status == "PASS"' in scenario_body
    assert 'getattr(backend, "process_id", None) != pid' in scenario_body
    assert "WPF one-call reports BLOCKED without FlaUI" in scenario_body
    assert "WPF one-call did not claim false PASS" not in scenario_body


def test_wpf_workflow_example_is_one_call_and_contains_required_evidence_sections() -> None:
    example_path = REPO_ROOT / "docs" / "examples" / "runtime-smoke-wpf-workflow-plan.json"
    example = example_path.read_text(encoding="utf-8")

    assert '"schema": "netcoredbg.runtime_smoke.v1"' in example
    assert '"op": "ui.grid.snapshot"' in example
    assert '"op": "ui.list.toggle_item_child"' in example
    assert '"op": "ui.invoke"' in example
    assert '"op": "ui.focus.assert"' in example
    assert '"restore_files"' in example
    assert '"stop_debug": "graceful"' in example
    assert '"debug_hygiene": true' in example
    assert "coordinate" not in example.lower()
    assert "ui_grid" not in example
    assert "ui_toggle" not in example

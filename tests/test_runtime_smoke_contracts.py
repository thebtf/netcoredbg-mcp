"""Shared runtime smoke contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from netcoredbg_mcp.session.state import (
    EvidenceRef,
    SmokeResultSummary,
    TerminalStatus,
)


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

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, before_probe, one_probe_plan, runner


class TextProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.text_results: deque[dict[str, Any]] = deque()
        self.read_results: deque[dict[str, Any]] = deque()
        self.state_results: deque[dict[str, Any]] = deque()
        self.selection_results: deque[dict[str, Any]] = deque()

    async def text_assert(
        self,
        *,
        selector: dict[str, Any],
        contains: str | None,
        equals: str | None,
        must_exist: bool,
    ) -> dict[str, Any]:
        self.calls.append(("ui.text.assert", dict(selector), contains, equals, must_exist))
        return self.text_results.popleft()

    async def text_read(
        self,
        *,
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("ui.text.read", dict(selector)))
        return self.read_results.popleft()

    async def text_get_state(
        self,
        *,
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("ui.text.get_state", dict(selector)))
        return self.state_results.popleft()

    async def text_assert_selection(
        self,
        *,
        selector: dict[str, Any],
        selection_start: int,
        selection_end: int,
    ) -> dict[str, Any]:
        self.calls.append(
            ("ui.text.assert_selection", dict(selector), selection_start, selection_end)
        )
        return self.selection_results.popleft()


@pytest.mark.asyncio
async def test_ui_text_probe_reads_text_and_checks_expected_after_value() -> None:
    session = TextProbeSession()
    session.text_results.extend(
        [
            {"status": "PASS", "text": "Off"},
            {"status": "PASS", "text": "On", "evidence_ref": "ui-text:mode-badge"},
        ]
    )

    result = await runner(
        session,
        {"ui.text.assert": session.text_assert},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "mode_badge",
                "selector": {"automation_id": "modeBadgeText"},
                "expected": "On",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] == "On"
    assert probe["expected"] == "On"
    assert probe["evidence_ref"] == "ui-text:mode-badge"


@pytest.mark.asyncio
async def test_ui_text_probe_blocks_when_execution_is_unavailable() -> None:
    session = TextProbeSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "mode_badge",
                "selector": {"automation_id": "modeBadgeText"},
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"


@pytest.mark.asyncio
async def test_ui_text_probe_read_mode_uses_read_adapter_without_expected() -> None:
    session = TextProbeSession()
    session.read_results.extend(
        [
            {
                "status": "PASS",
                "text": "Fixture cue zero",
                "source": "ValuePattern",
                "full_tree": {"must": "not leak"},
            },
            {
                "status": "PASS",
                "text": "Fixture cue one",
                "source": "ValuePattern",
                "full_tree": {"must": "not leak"},
            },
        ]
    )

    result = await runner(
        session,
        {"ui.text.read": session.text_read},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "cue_text",
                "action": "read",
                "selector": {"automation_id": "CueTextBox"},
            }
        )
    )

    before = before_probe(result)
    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert before["status"] == "PASS"
    assert before["value"] == "Fixture cue zero"
    assert probe["status"] == "PASS"
    assert probe["value"] == "Fixture cue one"
    assert probe["source"] == "ValuePattern"
    assert session.calls == [
        ("ui.text.read", {"automation_id": "CueTextBox"}),
        ("ui.invoke", {"automation_id": "ToggleSetting"}),
        ("ui.text.read", {"automation_id": "CueTextBox"}),
    ]
    assert "full_tree" not in str(before)
    assert "full_tree" not in str(probe)


@pytest.mark.asyncio
async def test_ui_text_probe_read_mode_preserves_blocked_status_without_expected() -> None:
    session = TextProbeSession()
    session.read_results.extend(
        [
            {"status": "BLOCKED", "reason": "bridge not connected"},
            {"status": "BLOCKED", "reason": "bridge not connected"},
        ]
    )

    result = await runner(
        session,
        {"ui.text.read": session.text_read},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "cue_text",
                "action": "read",
                "selector": {"automation_id": "CueTextBox"},
            }
        )
    )

    before = before_probe(result)
    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert before["status"] == "BLOCKED"
    assert before["reason"] == "bridge not connected"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "bridge not connected"


@pytest.mark.asyncio
async def test_ui_text_probe_get_state_uses_state_adapter_and_preserves_selection() -> None:
    session = TextProbeSession()
    session.state_results.extend(
        [
            {
                "status": "PASS",
                "text": "Fixture cue zero",
                "selection": {"start": 0, "end": 0, "length": 0, "selected_text": ""},
                "source": "TextPattern",
                "full_tree": {"must": "not leak"},
            },
            {
                "status": "PASS",
                "text": "Fixture cue one",
                "selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
                "caret_index": 10,
                "focus_within": True,
                "source": "TextPattern",
                "full_tree": {"must": "not leak"},
            },
        ]
    )

    result = await runner(
        session,
        {"ui.text.get_state": session.text_get_state},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "cue_text_state",
                "action": "get_state",
                "selector": {"automation_id": "CueTextBox"},
            }
        )
    )

    before = before_probe(result)
    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert before["value"] == "Fixture cue zero"
    assert before["selection"] == {"start": 0, "end": 0, "length": 0, "selected_text": ""}
    assert probe["status"] == "PASS"
    assert probe["value"] == "Fixture cue one"
    assert probe["selection"] == {
        "start": 3,
        "end": 10,
        "length": 7,
        "selected_text": "ture cu",
    }
    assert probe["caret_index"] == 10
    assert probe["focus_within"] is True
    assert probe["source"] == "TextPattern"
    assert session.calls == [
        ("ui.text.get_state", {"automation_id": "CueTextBox"}),
        ("ui.invoke", {"automation_id": "ToggleSetting"}),
        ("ui.text.get_state", {"automation_id": "CueTextBox"}),
    ]
    assert "full_tree" not in str(before)
    assert "full_tree" not in str(probe)


@pytest.mark.asyncio
async def test_ui_text_probe_assert_selection_uses_selection_adapter() -> None:
    session = TextProbeSession()
    session.selection_results.extend(
        [
            {
                "status": "PASS",
                "matched": True,
                "expected_selection": {"start": 3, "end": 10},
                "actual_selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
            },
            {
                "status": "PASS",
                "matched": True,
                "expected_selection": {"start": 3, "end": 10},
                "actual_selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
            },
        ]
    )

    result = await runner(
        session,
        {"ui.text.assert_selection": session.text_assert_selection},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "cue_text_selection",
                "action": "assert_selection",
                "selector": {"automation_id": "CueTextBox"},
                "selection": {"start": 3, "end": 10},
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["matched"] is True
    assert probe["expected_selection"] == {"start": 3, "end": 10}
    assert probe["actual_selection"] == {
        "start": 3,
        "end": 10,
        "length": 7,
        "selected_text": "ture cu",
    }
    assert session.calls == [
        ("ui.text.assert_selection", {"automation_id": "CueTextBox"}, 3, 10),
        ("ui.invoke", {"automation_id": "ToggleSetting"}),
        ("ui.text.assert_selection", {"automation_id": "CueTextBox"}, 3, 10),
    ]


@pytest.mark.asyncio
async def test_ui_text_probe_assert_selection_reports_mismatch_as_fail() -> None:
    session = TextProbeSession()
    session.selection_results.extend(
        [
            {
                "status": "PASS",
                "matched": True,
                "expected_selection": {"start": 3, "end": 10},
                "actual_selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
            },
            {
                "status": "FAIL",
                "matched": False,
                "reason": "selection mismatch",
                "expected_selection": {"start": 3, "end": 10},
                "actual_selection": {"start": 0, "end": 0, "length": 0, "selected_text": ""},
            },
        ]
    )

    result = await runner(
        session,
        {"ui.text.assert_selection": session.text_assert_selection},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "cue_text_selection",
                "action": "assert_selection",
                "selector": {"automation_id": "CueTextBox"},
                "selection": {"start": 3, "end": 10},
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "selection mismatch"
    assert probe["expected_selection"] == {"start": 3, "end": 10}
    assert probe["actual_selection"] == {
        "start": 0,
        "end": 0,
        "length": 0,
        "selected_text": "",
    }


@pytest.mark.asyncio
async def test_ui_text_probe_preserves_blocked_backend_diagnostics() -> None:
    session = TextProbeSession()
    session.text_results.extend(
        [
            {"status": "PASS", "text": "Pending"},
            {
                "status": "BLOCKED",
                "reason": "Element not found",
                "requested": {"selector": {"automation_id": "txtOutput"}},
                "accepted": {"fields": ["text", "value"]},
                "next_step": "Call ui_get_window_tree and verify the selector.",
                "backend_result": {"error": "Element not found"},
            },
        ]
    )

    result = await runner(
        session,
        {"ui.text.assert": session.text_assert},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.text",
                "name": "txt_output",
                "selector": {"automation_id": "txtOutput"},
                "expected": "Done",
            }
        )
    )

    probe = after_probe(result)
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "Element not found"
    assert probe["requested"] == {"selector": {"automation_id": "txtOutput"}}
    assert probe["accepted"] == {"fields": ["text", "value"]}
    assert probe["next_step"].startswith("Call ui_get_window_tree")
    assert probe["backend_result"] == {"error": "Element not found"}

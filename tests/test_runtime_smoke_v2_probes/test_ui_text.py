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

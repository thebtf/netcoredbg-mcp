from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class TextProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.text_results: deque[dict[str, Any]] = deque()

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


@pytest.mark.asyncio
async def test_ui_text_probe_reads_text_and_checks_expected_after_value() -> None:
    session = TextProbeSession()
    session.text_results.extend([
        {"status": "PASS", "text": "Off"},
        {"status": "PASS", "text": "On", "evidence_ref": "ui-text:mode-badge"},
    ])

    result = await runner(
        session,
        {"ui.text.assert": session.text_assert},
    ).run(one_probe_plan({
        "kind": "ui.text",
        "name": "mode_badge",
        "selector": {"automation_id": "modeBadgeText"},
        "expected": "On",
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] == "On"
    assert probe["expected"] == "On"
    assert probe["evidence_ref"] == "ui-text:mode-badge"


@pytest.mark.asyncio
async def test_ui_text_probe_blocks_when_execution_is_unavailable() -> None:
    session = TextProbeSession()

    result = await runner(session).run(one_probe_plan({
        "kind": "ui.text",
        "name": "mode_badge",
        "selector": {"automation_id": "modeBadgeText"},
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"

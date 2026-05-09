from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class UiPropertyProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.property_results: deque[dict[str, Any]] = deque()

    async def get_property(
        self,
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        self.calls.append(("ui.get_property", dict(selector), property_name))
        return self.property_results.popleft()


@pytest.mark.asyncio
async def test_ui_property_preserves_adapter_blocked_reason() -> None:
    session = UiPropertyProbeSession()
    session.property_results.extend([
        {"status": "PASS", "value": "before"},
        {
            "status": "BLOCKED",
            "reason": "backend bridge disconnected",
            "requested": {"adapter": "ui.get_property"},
            "accepted": {"adapter_names": ["ui.get_property"]},
            "next_step": "Reconnect UI bridge.",
        },
    ])

    result = await runner(
        session,
        {"ui.get_property": session.get_property},
    ).run(one_probe_plan({
        "kind": "ui.property",
        "name": "status_text",
        "selector": {"automation_id": "statusText"},
        "property": "Name",
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "backend bridge disconnected"
    assert probe["next_step"] == "Reconnect UI bridge."

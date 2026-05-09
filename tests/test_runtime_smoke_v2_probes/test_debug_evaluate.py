from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class DebugEvaluateProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.evaluate_results: deque[Any] = deque()

    async def evaluate_expression(self, expression: str) -> Any:
        self.calls.append(("debug.evaluate", expression))
        return self.evaluate_results.popleft()


@pytest.mark.asyncio
async def test_debug_evaluate_session_fallback_blocks_error_payload() -> None:
    session = DebugEvaluateProbeSession()
    session.evaluate_results.extend(
        [
            {"error": "frame not stopped"},
            {"error": "frame not stopped"},
        ]
    )

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "debug.evaluate",
                "name": "selected_value",
                "expression": "ViewModel.Mode",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "frame not stopped"

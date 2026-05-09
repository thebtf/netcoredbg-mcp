from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class CollectAllSession:
    def __init__(self, outcomes: dict[str, dict[str, Any]]) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("ui.invoke")
        return {"status": "PASS", "selector": selector}

    async def evaluate(self, expression: str) -> dict[str, Any]:
        self.calls.append(expression)
        return dict(self.outcomes[expression])


def _runner(session: CollectAllSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
        },
    )


def _plan(*expressions: str) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "aggregate",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "Toggle"},
                        },
                        "probes": [
                            {
                                "kind": "debug.evaluate",
                                "name": expression,
                                "expression": expression,
                            }
                            for expression in expressions
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_collect_all_blocked_plus_pass_aggregates_blocked() -> None:
    session = CollectAllSession(
        {
            "blocked": {"status": "BLOCKED", "reason": "no stop frame"},
            "passed": {"status": "PASS", "value": 1},
        }
    )

    result = await _runner(session).run(_plan("blocked", "passed"))

    assert result["status"] == "BLOCKED"
    assert result["cases"][0]["transitions"][0]["status"] == "BLOCKED"
    assert session.calls == ["blocked", "passed", "ui.invoke", "blocked", "passed"]


@pytest.mark.asyncio
async def test_collect_all_fail_plus_blocked_aggregates_fail() -> None:
    session = CollectAllSession(
        {
            "failed": {"status": "FAIL", "reason": "wrong value", "value": 0},
            "blocked": {"status": "BLOCKED", "reason": "no stop frame"},
        }
    )

    result = await _runner(session).run(_plan("failed", "blocked"))

    assert result["status"] == "FAIL"
    assert result["cases"][0]["transitions"][0]["status"] == "FAIL"
    assert session.calls == ["failed", "blocked", "ui.invoke", "failed", "blocked"]


@pytest.mark.asyncio
async def test_collect_all_passes_when_all_probes_pass() -> None:
    session = CollectAllSession(
        {
            "one": {"status": "PASS", "value": 1},
            "two": {"status": "PASS", "value": 2},
        }
    )

    result = await _runner(session).run(_plan("one", "two"))

    assert result["status"] == "PASS"
    assert result["cases"][0]["transitions"][0]["status"] == "PASS"
    assert session.calls == ["one", "two", "ui.invoke", "one", "two"]

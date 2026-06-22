from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.transition_executor import (
    _status_from_records,
)


class ConfidenceSmokeSession:
    def __init__(self, monitor_result: dict[str, Any] | None = None) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.monitor_result = monitor_result or {"status": "PASS"}
        self.monitor_calls: list[dict[str, Any]] = []

    def input_monitor_check(self, **kwargs: Any) -> dict[str, Any]:
        self.monitor_calls.append(dict(kwargs))
        return dict(self.monitor_result)


def _runner(
    session: ConfidenceSmokeSession,
    *,
    include_monitor: bool = True,
) -> RuntimeSmokeRunner:
    adapters = (
        {"runtime.input_monitor.check": session.input_monitor_check}
        if include_monitor
        else {}
    )
    return RuntimeSmokeRunner(session, service_adapters=adapters)


def _no_operator_plan(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = {
        "schema": "netcoredbg.runtime_smoke.v2",
        "input_policy": {"no_global_input": True},
        "run_confidence": {"no_operator": True},
        "cases": [
            {
                "id": "no_operator_case",
                "transitions": [
                    {
                        "id": "noop_transition",
                        "action": {"kind": "noop"},
                        "probes": [],
                    }
                ],
            }
        ],
    }
    if extra:
        plan.update(extra)
    return plan


def test_unknown_confidence_statuses_fail_closed_in_transition_aggregation() -> None:
    assert _status_from_records(
        [{"status": "PASS"}, {"status": "UNPROVEN"}]
    ) == "BLOCKED"


@pytest.mark.asyncio
async def test_no_operator_dirty_monitor_blocks_product_verdict() -> None:
    session = ConfidenceSmokeSession(
        {
            "status": "DIRTY",
            "source": "mouse",
            "window": "action",
            "summary": "external mouse movement observed",
        }
    )

    result = await _runner(session).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert result["run_confidence"]["product_verdict_allowed"] is False
    assert result["run_confidence"]["contamination"]["source"] == "mouse"
    assert result["run_confidence"]["contamination"]["window"] == "action"
    assert "restart" in result["run_confidence"]["restart_guidance"].lower()
    assert result["compact"]["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert session.monitor_calls
    assert session.monitor_calls[0]["input_policy"] == {"no_global_input": True}
    assert session.monitor_calls[0]["run_confidence"] == {"no_operator": True}


@pytest.mark.asyncio
async def test_no_operator_missing_monitor_is_blocked_unproven() -> None:
    session = ConfidenceSmokeSession()

    result = await _runner(session, include_monitor=False).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "UNPROVEN"
    assert result["run_confidence"]["basis"] == "monitor_unavailable"
    assert result["run_confidence"]["product_verdict_allowed"] is False
    assert "runtime.input_monitor.check" in result["run_confidence"]["restart_guidance"]
    assert result["compact"]["run_confidence"]["classification"] == "UNPROVEN"


@pytest.mark.asyncio
async def test_no_operator_clean_monitor_allows_product_failure() -> None:
    session = ConfidenceSmokeSession({"status": "PASS", "basis": "external_input_monitor"})

    result = await _runner(session).run(
        _no_operator_plan({"metrics_thresholds": {"action_latency_ms": {"max": 1}}})
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "metric threshold exceeded"
    assert result["run_confidence"]["classification"] == "CLEAN_PROVEN"
    assert result["run_confidence"]["product_verdict_allowed"] is True
    assert result["compact"]["run_confidence"]["classification"] == "CLEAN_PROVEN"

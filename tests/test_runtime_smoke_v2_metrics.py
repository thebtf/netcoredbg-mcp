from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2 import metrics as metrics_module


class ManualClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps_ms: list[int] = []

    def __call__(self) -> float:
        return self.current

    async def sleep_ms(self, idle_ms: int) -> None:
        self.sleeps_ms.append(idle_ms)
        self.current += idle_ms / 1000


class MetricSmokeSession:
    def __init__(self, clock: ManualClock) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.clock = clock
        self.calls: list[tuple[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {"status": "PASS", "found": True}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return {"status": "PASS"}

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        self.clock.current += 0.160
        return {"status": "PASS", "sent": keys}


def _runner(session: MetricSmokeSession, clock: ManualClock) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
        },
        clock=clock,
    )


def _plan(*, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "latency_case",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.key_sequence",
                            "selector": {"automation_id": "largeGridToggle"},
                            "keys": "{SPACE}",
                        },
                        "settle": {"idle_ms": 250},
                        "probes": [],
                    }
                ],
            }
        ],
    }
    if thresholds is not None:
        plan["metrics_thresholds"] = thresholds
    return plan


@pytest.mark.asyncio
async def test_v2_case_metrics_are_present_with_latency_and_memory_fields() -> None:
    clock = ManualClock()
    session = MetricSmokeSession(clock)

    result = await _runner(session, clock).run(_plan())

    metrics = result["cases"][0]["metrics"]
    assert metrics["action_latency_ms"] == 410
    assert "working_set_delta_mb" in metrics
    assert "private_bytes_delta_mb" in metrics
    assert metrics["partial"] in (False, True)


@pytest.mark.asyncio
async def test_v2_metric_threshold_breach_flips_case_to_fail() -> None:
    clock = ManualClock()
    session = MetricSmokeSession(clock)

    result = await _runner(session, clock).run(
        _plan(thresholds={"action_latency_ms": {"max": 250}})
    )

    case = result["cases"][0]
    assert result["status"] == "FAIL"
    assert case["status"] == "FAIL"
    assert case["failed_assertions"] == [
        {
            "kind": "metric_threshold",
            "metric": "action_latency_ms",
            "value": 410,
            "threshold": {"max": 250},
        }
    ]


def test_v2_metric_thresholds_ignore_non_numeric_min_max() -> None:
    failures = metrics_module.evaluate_metric_thresholds(
        {"action_latency_ms": 410},
        {"action_latency_ms": {"max": "250", "min": None}},
    )

    assert failures == []


@pytest.mark.asyncio
async def test_v2_metrics_without_thresholds_do_not_flip_case_status() -> None:
    clock = ManualClock()
    session = MetricSmokeSession(clock)

    result = await _runner(session, clock).run(_plan())

    case = result["cases"][0]
    assert result["status"] == "PASS"
    assert case["status"] == "PASS"
    assert case["metrics"]["action_latency_ms"] == 410
    assert case["failed_assertions"] == []


@pytest.mark.asyncio
async def test_v2_metrics_mark_memory_fields_blocked_when_psutil_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metrics_module, "_load_psutil", lambda: None)
    clock = ManualClock()
    session = MetricSmokeSession(clock)

    result = await _runner(session, clock).run(_plan())

    metrics = result["cases"][0]["metrics"]
    assert metrics["partial"] is True
    assert metrics["working_set_delta_mb"] is None
    assert metrics["private_bytes_delta_mb"] is None
    assert metrics["field_status"]["working_set_delta_mb"]["status"] == "BLOCKED"
    assert metrics["field_status"]["private_bytes_delta_mb"]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_v2_metrics_mark_private_bytes_blocked_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.samples = [
                SimpleNamespace(rss=100 * 1024 * 1024),
                SimpleNamespace(rss=112 * 1024 * 1024),
            ]

        def memory_info(self) -> Any:
            return self.samples.pop(0)

    class FakePsutil:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self.process_calls: list[int] = []

        def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - mirrors psutil API
            self.process_calls.append(pid)
            return self.process

    fake_psutil = FakePsutil()
    monkeypatch.setattr(metrics_module, "_load_psutil", lambda: fake_psutil)
    clock = ManualClock()
    session = MetricSmokeSession(clock)
    session.process_id = 4242

    result = await _runner(session, clock).run(_plan())

    metrics = result["cases"][0]["metrics"]
    assert fake_psutil.process_calls == [4242, 4242]
    assert metrics["partial"] is True
    assert metrics["working_set_delta_mb"] == 12.0
    assert metrics["private_bytes_delta_mb"] is None
    assert metrics["field_status"]["working_set_delta_mb"]["status"] == "PASS"
    assert metrics["field_status"]["private_bytes_delta_mb"] == {
        "status": "BLOCKED",
        "reason": "private bytes unavailable",
    }


@pytest.mark.asyncio
async def test_v2_metrics_do_not_sample_orchestrator_without_target_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePsutil:
        def __init__(self) -> None:
            self.process_calls: list[int] = []

        def Process(self, pid: int) -> Any:  # noqa: N802 - mirrors psutil API
            self.process_calls.append(pid)
            raise AssertionError("Process should not be sampled without target pid")

    fake_psutil = FakePsutil()
    monkeypatch.setattr(metrics_module, "_load_psutil", lambda: fake_psutil)
    clock = ManualClock()
    session = MetricSmokeSession(clock)

    result = await _runner(session, clock).run(_plan())

    metrics = result["cases"][0]["metrics"]
    assert fake_psutil.process_calls == []
    assert metrics["partial"] is True
    assert metrics["field_status"]["working_set_delta_mb"] == {
        "status": "BLOCKED",
        "reason": "target process id unavailable",
    }

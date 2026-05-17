from __future__ import annotations

import asyncio
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2 import RuntimeStateOracleRunner


class CleanupSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []
        self.restore_results: dict[str, dict[str, Any]] = {}
        self.process_registry_result: dict[str, Any] = {"status": "PASS", "count": 0}

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("ui.invoke", dict(selector)))
        return {"status": "PASS"}

    async def fixture_restore(self, *, path: str, baseline_file: str) -> dict[str, Any]:
        self.calls.append(("fixture.restore", path, baseline_file))
        return self.restore_results.get(path, {"status": "PASS"})

    async def remove_tracepoint(self, tracepoint_id: str) -> dict[str, Any]:
        self.calls.append(("debug.tracepoint.remove", tracepoint_id))
        return {"status": "PASS"}

    async def teardown_profile(self, profile: str) -> dict[str, Any]:
        self.calls.append(("isolated_profile.teardown", profile))
        return {"status": "PASS"}

    async def stop_debug(self, mode: str) -> dict[str, Any]:
        self.calls.append(("debug.stop", mode))
        return {"status": "PASS"}

    async def process_registry_count(self) -> dict[str, Any]:
        self.calls.append(("process.registry.count", None))
        return dict(self.process_registry_result)


def _runner(session: CleanupSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "fixture.restore": session.fixture_restore,
            "debug.tracepoint.remove": session.remove_tracepoint,
            "isolated_profile.teardown": session.teardown_profile,
            "debug.stop": session.stop_debug,
            "process.registry.count": session.process_registry_count,
        },
    )


def _v2_runner(session: CleanupSmokeSession) -> RuntimeStateOracleRunner:
    return RuntimeStateOracleRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "fixture.restore": session.fixture_restore,
            "debug.tracepoint.remove": session.remove_tracepoint,
            "isolated_profile.teardown": session.teardown_profile,
            "debug.stop": session.stop_debug,
            "process.registry.count": session.process_registry_count,
        },
    )


@pytest.mark.asyncio
async def test_v2_runner_converts_budget_parse_errors_to_invalid_setup() -> None:
    session = CleanupSmokeSession()

    result = await _v2_runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": "abc"},
            "cases": [
                {
                    "id": "case_a",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == ["budgets.max_actions must be an integer"]
    assert result["action_count"] == 0
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_cleanup_aggregates_plan_and_case_cleanup_without_stopping_next_case() -> None:
    session = CleanupSmokeSession()
    session.restore_results["case-a.txt"] = {
        "status": "FAIL",
        "reason": "restore denied",
    }

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cleanup": {
                "steps": [
                    {"kind": "isolated_profile.teardown", "profile": "isolated"},
                    {"kind": "debug.stop"},
                    {"kind": "process.registry.assert_empty"},
                ]
            },
            "cases": [
                {
                    "id": "case_a",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "probes": [],
                        }
                    ],
                    "cleanup": [
                        {
                            "kind": "fixture.restore",
                            "path": "case-a.txt",
                            "baseline_file": "baseline-a.txt",
                        },
                        {"kind": "debug.tracepoint.remove", "id": "tp-a"},
                    ],
                },
                {
                    "id": "case_b",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseB"},
                            },
                            "probes": [],
                        }
                    ],
                },
            ],
        }
    )

    assert [case["id"] for case in result["cases"]] == ["case_a", "case_b"]
    assert result["cases"][0]["cleanup"]["status"] == "FAIL"
    assert result["cases"][1]["status"] == "PASS"
    assert result["cleanup"]["status"] == "FAIL"
    assert result["cleanup"]["process_registry_after"] == 0
    assert result["cleanup"]["debug_stop"]["mode"] == "graceful"
    assert result["cleanup"]["tracepoints_removed"] == 1
    assert result["cleanup"]["failed_case_cleanups"] == [
        {
            "case_id": "case_a",
            "failures": [
                {
                    "kind": "fixture.restore",
                    "reason": "restore denied",
                    "result": {"status": "FAIL", "reason": "restore denied"},
                }
            ],
        }
    ]
    assert ("ui.invoke", {"automation_id": "caseB"}) in session.calls


@pytest.mark.asyncio
async def test_v2_cleanup_reports_invalid_process_registry_count() -> None:
    session = CleanupSmokeSession()
    session.process_registry_result = {"status": "PASS", "count": "not-a-number"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cleanup": {"steps": [{"kind": "process.registry.assert_empty"}]},
            "cases": [
                {
                    "id": "case_a",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "FAIL"
    assert result["cleanup"]["status"] == "FAIL"
    assert result["cleanup"]["failures"][0]["reason"] == "invalid process registry count"
    assert result["cleanup"]["failures"][0]["result"]["count"] == "not-a-number"


@pytest.mark.asyncio
async def test_v2_cleanup_stops_debug_before_asserting_registry_empty() -> None:
    class StopClearsRegistrySession(CleanupSmokeSession):
        def __init__(self) -> None:
            super().__init__()
            self.process_registry_result = {"status": "PASS", "count": 2}

        async def stop_debug(self, mode: str) -> dict[str, Any]:
            self.calls.append(("debug.stop", mode))
            self.process_registry_result = {"status": "PASS", "count": 0}
            return {"status": "PASS"}

    session = StopClearsRegistrySession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cleanup": {
                "steps": [
                    {"kind": "debug.stop"},
                    {"kind": "process.registry.assert_empty"},
                ]
            },
            "cases": [
                {
                    "id": "case_a",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0
    assert session.calls[-2:] == [
        ("debug.stop", "graceful"),
        ("process.registry.count", None),
    ]


@pytest.mark.asyncio
async def test_v2_elapsed_budget_timeout_returns_cleanup_evidence() -> None:
    class SlowActionSession(CleanupSmokeSession):
        def __init__(self) -> None:
            super().__init__()
            self.process_registry_result = {"status": "PASS", "count": 1}

        async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(("ui.invoke", dict(selector)))
            await asyncio.sleep(0.05)
            return {"status": "PASS"}

        async def stop_debug(self, mode: str) -> dict[str, Any]:
            self.calls.append(("debug.stop", mode))
            self.process_registry_result = {"status": "PASS", "count": 0}
            return {"status": "PASS"}

    session = SlowActionSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": 3, "max_elapsed_seconds": 0.01},
            "cleanup": {
                "steps": [
                    {"kind": "debug.stop"},
                    {"kind": "process.registry.assert_empty"},
                ]
            },
            "cases": [
                {
                    "id": "slow_case",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "IMPASSE"
    assert result["reason"] == "elapsed time budget exhausted"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0
    assert session.calls[-2:] == [
        ("debug.stop", "graceful"),
        ("process.registry.count", None),
    ]


@pytest.mark.asyncio
async def test_v2_elapsed_budget_does_not_cancel_or_repeat_case_cleanup() -> None:
    class SlowCaseCleanupSession(CleanupSmokeSession):
        async def fixture_restore(self, *, path: str, baseline_file: str) -> dict[str, Any]:
            self.calls.append(("fixture.restore", path, baseline_file))
            await asyncio.sleep(0.05)
            return {"status": "PASS"}

    session = SlowCaseCleanupSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": 3, "max_elapsed_seconds": 0.02},
            "cases": [
                {
                    "id": "slow_cleanup_case",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "settle": {"idle_ms": 0},
                            "probes": [],
                        }
                    ],
                    "cleanup": [
                        {
                            "kind": "fixture.restore",
                            "path": "case-a.txt",
                            "baseline_file": "baseline-a.txt",
                        },
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert result["cases"][0]["cleanup"]["status"] == "PASS"
    assert session.calls.count(("fixture.restore", "case-a.txt", "baseline-a.txt")) == 1


@pytest.mark.asyncio
async def test_v2_elapsed_budget_preserves_completed_action_evidence() -> None:
    session = CleanupSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": 3, "max_elapsed_seconds": 0.02},
            "cases": [
                {
                    "id": "slow_settle_case",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "caseA"},
                            },
                            "settle": {"idle_ms": 50},
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "IMPASSE"
    assert result["reason"] == "elapsed time budget exhausted"
    assert result["action_count"] == 1
    action = result["cases"][0]["actions"][0]
    assert action["status"] == "PASS"
    assert action["route"] == "invoke"
    assert action["selector"] == {"automation_id": "caseA"}
    assert result["cases"][0]["transitions"][0]["actions"] == [action]


@pytest.mark.asyncio
async def test_v2_action_budget_stops_before_next_transition_with_cleanup_evidence() -> None:
    session = CleanupSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": 1, "max_elapsed_seconds": 10},
            "cleanup": {
                "steps": [
                    {"kind": "debug.stop"},
                    {"kind": "process.registry.assert_empty"},
                ]
            },
            "cases": [
                {
                    "id": "bounded_case",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "first"},
                            },
                            "probes": [],
                        },
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "second"},
                            },
                            "probes": [],
                        },
                    ],
                }
            ],
        }
    )

    assert result["status"] == "IMPASSE"
    assert result["reason"] == "action budget exhausted"
    assert result["action_count"] == 1
    assert result["cases"][0]["actions"][0]["selector"] == {"automation_id": "first"}
    assert ("ui.invoke", {"automation_id": "second"}) not in session.calls
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0


@pytest.mark.asyncio
async def test_v2_action_budget_does_not_mask_failed_transition() -> None:
    class FailingInvokeSession(CleanupSmokeSession):
        async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(("ui.invoke", dict(selector)))
            return {"status": "FAIL", "reason": "button rejected"}

    session = FailingInvokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "budgets": {"max_actions": 1, "max_elapsed_seconds": 10},
            "cleanup": {
                "steps": [
                    {"kind": "debug.stop"},
                    {"kind": "process.registry.assert_empty"},
                ]
            },
            "cases": [
                {
                    "id": "failing_case",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "first"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "button rejected"
    assert result["action_count"] == 1
    assert result["cleanup"]["status"] == "PASS"

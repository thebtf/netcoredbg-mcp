from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class CleanupSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []
        self.restore_results: dict[str, dict[str, Any]] = {}

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
        return {"status": "PASS", "count": 0}


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


@pytest.mark.asyncio
async def test_v2_cleanup_aggregates_plan_and_case_cleanup_without_stopping_next_case() -> None:
    session = CleanupSmokeSession()
    session.restore_results["case-a.txt"] = {
        "status": "FAIL",
        "reason": "restore denied",
    }

    result = await _runner(session).run({
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
    })

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

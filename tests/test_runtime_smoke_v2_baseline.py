from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class BaselineSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self._now = 0.0
        self.calls: list[tuple[float, str, Any]] = []
        self.launch_result: dict[str, Any] = {"status": "PASS", "profile": "isolated"}

    def clock(self) -> float:
        self._now += 1.0
        return self._now

    async def fixture_restore(self, *, path: str, baseline_file: str) -> dict[str, Any]:
        self.calls.append((self.clock(), "fixture.restore", (path, baseline_file)))
        return {"status": "PASS", "path": path, "baseline_file": baseline_file}

    async def launch(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((self.clock(), "launch", dict(kwargs)))
        return dict(self.launch_result)

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((self.clock(), "ui.invoke", dict(selector)))
        return {"status": "PASS", "invoked": True}


def _runner(session: BaselineSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "fixture.restore": session.fixture_restore,
            "launch": session.launch,
            "ui.invoke": session.invoke,
        },
        clock=session.clock,
    )


def _baseline_plan() -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "baseline": {
            "steps": [
                {
                    "id": "restore-fixture",
                    "kind": "fixture.restore",
                    "path": "work/settings.json",
                    "baseline_file": "fixtures/settings.clean.json",
                },
                {
                    "id": "launch-isolated",
                    "kind": "isolated_profile.launch",
                    "launch": {"program": "SmokeTestApp.dll", "profile": "isolated"},
                },
                {
                    "id": "set-starting-control",
                    "kind": "control_set",
                    "action": {
                        "kind": "ui.invoke",
                        "selector": {"automation_id": "checkBoxSpellCheckInput"},
                    },
                },
            ]
        },
        "cases": [
            {
                "id": "spellcheck_case",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "caseToggle"},
                        },
                        "probes": [],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_v2_baseline_runs_before_first_case() -> None:
    session = BaselineSmokeSession()

    result = await _runner(session).run(_baseline_plan())

    assert result["status"] == "PASS"
    assert [call[1:] for call in session.calls] == [
        ("fixture.restore", ("work/settings.json", "fixtures/settings.clean.json")),
        ("launch", {"program": "SmokeTestApp.dll", "profile": "isolated"}),
        ("ui.invoke", {"automation_id": "checkBoxSpellCheckInput"}),
        ("ui.invoke", {"automation_id": "caseToggle"}),
    ]
    assert [call[0] for call in session.calls] == sorted(call[0] for call in session.calls)


@pytest.mark.asyncio
async def test_v2_baseline_failure_blocks_cases_and_runs_cleanup() -> None:
    session = BaselineSmokeSession()
    session.launch_result = {"status": "FAIL", "reason": "launch failed"}

    result = await _runner(session).run(_baseline_plan())

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "baseline setup failed"
    assert result["cases"] == []
    assert result["baseline"]["status"] == "BLOCKED"
    assert result["baseline"]["failed_step_id"] == "launch-isolated"
    assert result["baseline"]["cleanup"]["status"] == "PASS"
    assert result["baseline"]["cleanup"]["attempted"] == ["fixture.restore:work/settings.json"]
    assert ("ui.invoke", {"automation_id": "caseToggle"}) not in [
        call[1:] for call in session.calls
    ]


@pytest.mark.asyncio
async def test_v2_baseline_success_records_ordered_step_outcomes() -> None:
    session = BaselineSmokeSession()

    result = await _runner(session).run(_baseline_plan())

    assert result["baseline"]["status"] == "PASS"
    assert [step["id"] for step in result["baseline"]["steps"]] == [
        "restore-fixture",
        "launch-isolated",
        "set-starting-control",
    ]
    assert [step["status"] for step in result["baseline"]["steps"]] == [
        "PASS",
        "PASS",
        "PASS",
    ]
    assert result["cases"][0]["id"] == "spellcheck_case"

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "WpfSmokeApp"


class WpfStateOracleSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []
        self.debug_reads: defaultdict[str, int] = defaultdict(int)
        self.ui_reads: defaultdict[str, int] = defaultdict(int)

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("ui.invoke", dict(selector)))
        return {"status": "PASS", "selector": dict(selector)}

    async def evaluate(self, expression: str) -> dict[str, Any]:
        self.calls.append(("debug.evaluate", expression))
        index = self.debug_reads[expression]
        self.debug_reads[expression] += 1
        return {"status": "PASS", "value": f"{expression}:{index}"}

    async def get_property(
        self,
        *,
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        key = f"{selector.get('automation_id')}:{property_name}"
        self.calls.append(("ui.get_property", key))
        index = self.ui_reads[key]
        self.ui_reads[key] += 1
        return {
            "status": "PASS",
            "found": True,
            "value": f"{key}:{index}",
        }

    async def process_registry_count(self) -> dict[str, Any]:
        self.calls.append(("process.registry.count", None))
        return {"status": "PASS", "count": 0}


def _runner(session: WpfStateOracleSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
            "ui.get_property": session.get_property,
            "process.registry.count": session.process_registry_count,
        },
    )


def _case(index: int) -> dict[str, Any]:
    return {
        "id": f"wpf_ab_{index}",
        "transitions": [
            {
                "action": {
                    "kind": "ui.invoke",
                    "selector": {"automation_id": f"wpfToggle{index}"},
                },
                "probes": [
                    {
                        "kind": "debug.evaluate",
                        "name": f"setting_{index}",
                        "expression": f"WpfSmokeApp.Settings.Flag{index}",
                    },
                    {
                        "kind": "ui.property",
                        "name": f"visible_{index}",
                        "selector": {"automation_id": f"wpfStatus{index}"},
                        "property": "Name",
                    },
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_v2_wpf_state_oracle_runs_five_ab_cases_with_diffs() -> None:
    assert (FIXTURE_ROOT / "WpfSmokeApp.csproj").exists()
    session = WpfStateOracleSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "wpf fixture state oracle",
            "cases": [_case(index) for index in range(5)],
            "cleanup": {
                "steps": [{"kind": "process.registry.assert_empty"}],
            },
        }
    )

    assert result["status"] == "PASS"
    assert len(result["cases"]) == 5
    assert all(case["diff"] for case in result["cases"])
    assert any(
        any(path.startswith("debug.evaluate.") for path in case["before"])
        for case in result["cases"]
    )
    assert any(
        any(path.startswith("ui.property.") for path in case["before"]) for case in result["cases"]
    )

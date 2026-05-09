from __future__ import annotations

import sys
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from tests.smoke_test_manual import run_wpf_v2_state_oracle_runtime_smoke


class CriticalV2Session:
    def __init__(self, *, selector_missing: bool = False) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.selector_missing = selector_missing

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        if self.selector_missing:
            return {
                "status": "BLOCKED",
                "reason": "selector not found",
                "requested": {"selector": dict(selector)},
                "accepted": {"selector_keys": ["automation_id", "name"]},
                "next_step": "Inspect the fixture UI tree.",
            }
        return {"status": "PASS"}

    async def process_registry_count(self) -> dict[str, Any]:
        return {"status": "PASS", "count": 0}


def _runner(session: CriticalV2Session) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "process.registry.count": session.process_registry_count,
        },
    )


def _plan() -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "critical_happy",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "criticalToggle"},
                        },
                        "probes": [],
                    }
                ],
            }
        ],
        "cleanup": {
            "steps": [{"kind": "process.registry.assert_empty"}],
        },
    }


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_smoke_v2_critical_happy_path_has_cleanup_proof() -> None:
    result = await _runner(CriticalV2Session()).run(_plan())

    assert result["status"] == "PASS"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_smoke_v2_critical_selector_miss_blocks_with_cleanup_proof() -> None:
    result = await _runner(CriticalV2Session(selector_missing=True)).run(_plan())

    assert result["status"] == "BLOCKED"
    assert result["blocked"]["reason"] == "selector not found"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_smoke_v2_critical_direct_wpf_mcp_smoke() -> None:
    if sys.platform != "win32":
        pytest.skip("direct WPF runtime smoke requires Windows UI automation")

    evidence = await run_wpf_v2_state_oracle_runtime_smoke()

    assert evidence["status"] == "PASS", evidence
    assert evidence["happy"]["status"] == "PASS", evidence
    assert evidence["happy"]["cleanup"]["process_registry_after"] == 0, evidence
    assert evidence["blocked"]["status"] == "BLOCKED", evidence
    assert evidence["blocked"]["blocked"]["reason"] == "selector not found", evidence
    assert evidence["blocked"]["cleanup"]["process_registry_after"] == 0, evidence

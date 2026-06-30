from __future__ import annotations

import sys
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.actions.ui_drag import (
    REASON_NO_ROUTE_EVIDENCE,
)
from tests.smoke_test_manual import run_wpf_v2_state_oracle_runtime_smoke


class CriticalV2Session:
    def __init__(
        self,
        *,
        selector_missing: bool = False,
        drag_result: dict[str, Any] | None = None,
        viewport_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.selector_missing = selector_missing
        self.drag_result = drag_result
        self.viewport_results = list(viewport_results or [])

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

    async def drag(self, **_: Any) -> dict[str, Any]:
        if self.drag_result is not None:
            return dict(self.drag_result)
        return {
            "status": "PASS",
            "backend": "critical-fake",
            "route_evidence": {
                "move_points": [{"relative_to": "screen", "x": 12, "y": 14}],
                "final_pointer": {"relative_to": "screen", "x": 20, "y": 30},
            },
        }

    async def grid_viewport(self, **_: Any) -> dict[str, Any]:
        if self.viewport_results:
            return self.viewport_results.pop(0)
        return {
            "status": "PASS",
            "snapshot": {
                "first_visible_index": 0,
                "last_visible_index": 1,
                "visible_rows": [
                    {"index": 0, "identity": "Cue 001"},
                    {"index": 1, "identity": "Cue 002"},
                ],
                "row_count": 2,
            },
        }

    async def process_registry_count(self) -> dict[str, Any]:
        return {"status": "PASS", "count": 0}


def _runner(session: CriticalV2Session) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "ui.grid.viewport": session.grid_viewport,
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


def _drag_plan(*, include_viewport_probe: bool = False) -> dict[str, Any]:
    transition: dict[str, Any] = {
        "action": {
            "kind": "ui.drag",
            "source": {"point": {"x": 10, "y": 10}},
            "path": [{"relative_to": "screen", "x": 12, "y": 14}],
            "drop": {"relative_to": "screen", "x": 20, "y": 30},
        },
        "probes": [],
    }
    if include_viewport_probe:
        transition["probes"] = [
            {
                "kind": "ui.grid.viewport",
                "name": "critical_viewport",
                "phase": "both",
                "selector": {"automation_id": "CriticalGrid"},
                "identity": {"column": "Phrase"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {"identity_order_preserved": True},
            }
        ]
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "critical_drag",
                "transitions": [transition],
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
async def test_runtime_smoke_v2_critical_selector_miss_blocks_with_cleanup_proof() -> (
    None
):
    result = await _runner(CriticalV2Session(selector_missing=True)).run(_plan())

    assert result["status"] == "BLOCKED"
    assert result["blocked"]["reason"] == "selector not found"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_smoke_v2_critical_drag_blocks_without_route_evidence() -> None:
    result = await _runner(
        CriticalV2Session(
            drag_result={
                "status": "PASS",
                "backend": "diagnostic-shortcut",
            }
        )
    ).run(_drag_plan())

    assert result["status"] == "BLOCKED"
    assert result["blocked"]["reason"] == REASON_NO_ROUTE_EVIDENCE
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0


@pytest.mark.critical
@pytest.mark.asyncio
async def test_runtime_smoke_v2_critical_drag_blocks_without_viewport_evidence() -> (
    None
):
    missing_identity_snapshot = {
        "status": "PASS",
        "snapshot": {
            "first_visible_index": 0,
            "last_visible_index": 1,
            "visible_rows": [],
            "row_count": 2,
        },
    }

    result = await _runner(
        CriticalV2Session(
            viewport_results=[missing_identity_snapshot, missing_identity_snapshot],
        )
    ).run(_drag_plan(include_viewport_probe=True))

    assert result["status"] == "BLOCKED"
    assert result["blocked"]["reason"] == "visible row identity evidence unavailable"
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
    assert (
        evidence["blocked"]["blocked"]["reason"]
        == "selector result did not match exact automation_id"
    ), evidence
    assert evidence["blocked"]["cleanup"]["process_registry_after"] == 0, evidence

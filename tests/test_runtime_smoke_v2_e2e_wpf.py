from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from tests import smoke_test_manual

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


def test_manual_smoke_exposes_offscreen_row_target_drag_entrypoint() -> None:
    assert hasattr(smoke_test_manual, "test_wpf_v2_offscreen_row_target_drag_runtime_smoke")


def test_offscreen_row_target_drag_plan_uses_source_and_drop_ensure_visible() -> None:
    plan = smoke_test_manual._v2_offscreen_row_target_drag_plan(
        program="WpfSmokeApp.dll",
        build_project="tests/fixtures/WpfSmokeApp/WpfSmokeApp.csproj",
    )

    transition = plan["cases"][0]["transitions"][0]
    action = transition["action"]

    assert action["kind"] == "ui.drag"
    assert action["ensure_visible"] is True
    assert action["drop"]["ensure_visible"] is True
    assert action["source"]["row_identity"] == "Fixture cue two"
    assert action["drop"]["row_identity"] == "Fixture cue nineteen"
    assert action["drop"]["identity"] == {"column": "Phrase"}
    assert action["drop"]["rows"] == {"visible_only": True, "max": 8}
    assert action["drop"]["columns"] == ["Phrase"]
    assert action["expect"]["row_count_preserved"] is True
    assert action["expect"]["identity_set_preserved"] is True
    assert any(
        probe["kind"] == "ui.grid.viewport" and probe["name"] == "offscreen_target_viewport"
        for probe in transition["probes"]
    )


def test_manual_smoke_lists_offscreen_row_target_drag_scenario() -> None:
    if not smoke_test_manual.WPF_GUI_ENABLED:
        pytest.skip("WPF fixture build required for WPF manual-smoke scenario inventory")

    scenario_names = {name for name, _fn in smoke_test_manual.get_scenarios()}
    assert "WPF V2 Offscreen Row-Target Drag Runtime Smoke" in scenario_names


def test_parse_drag_reorder_status_reads_drop_time_diagnostics() -> None:
    status = smoke_test_manual._parse_drag_reorder_status(
        "WpfWorkflow DragReorder "
        "sourceIdentity=Fixture cue two targetIdentity=Fixture cue nineteen "
        "selectedPayloadMode=single selectedPayloadBefore=Fixture cue two "
        "selectedPayloadAfter=Fixture cue two edgeScrollDirection=down "
        "edgeFirstVisible=1 edgeLastVisible=21 dropPoint=354,63 "
        "dropOriginTarget=Fixture cue nineteen dropBoundsTarget=Fixture cue nineteen "
        "dropBoundsIndex=18 dropBoundsTop=44 dropBoundsBottom=63 "
        "orderFingerprint=Fixture cue one>Fixture cue three>Fixture cue nineteen"
    )

    assert status["source_identity"] == "Fixture cue two"
    assert status["target_identity"] == "Fixture cue nineteen"
    assert status["drop_point_x"] == 354
    assert status["drop_point_y"] == 63
    assert status["drop_origin_target"] == "Fixture cue nineteen"
    assert status["drop_bounds_target"] == "Fixture cue nineteen"
    assert status["drop_bounds_index"] == 18
    assert status["drop_bounds_top"] == 44
    assert status["drop_bounds_bottom"] == 63


def test_parse_drag_reorder_status_reads_blocked_drop_time_diagnostics() -> None:
    status = smoke_test_manual._parse_drag_reorder_status(
        "WpfWorkflow DragReorder blocked "
        "sourceIdentity=Fixture cue two targetIdentity=<none> "
        "dropPoint=354,63 dropOriginTarget=<none> dropBoundsTarget=Fixture cue eighteen "
        "dropBoundsIndex=17 dropBoundsTop=44 dropBoundsBottom=63"
    )

    assert status["source_identity"] == "Fixture cue two"
    assert status["target_identity"] == "<none>"
    assert status["drop_point_x"] == 354
    assert status["drop_point_y"] == 63
    assert status["drop_origin_target"] == "<none>"
    assert status["drop_bounds_target"] == "Fixture cue eighteen"
    assert status["drop_bounds_index"] == 17
    assert status["drop_bounds_top"] == 44
    assert status["drop_bounds_bottom"] == 63
    assert status["order"] == []

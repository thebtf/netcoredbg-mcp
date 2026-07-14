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
        any(path.startswith("ui.property.") for path in case["before"])
        for case in result["cases"]
    )


def test_manual_smoke_exposes_offscreen_row_target_drag_entrypoint() -> None:
    assert hasattr(
        smoke_test_manual, "test_wpf_v2_offscreen_row_target_drag_runtime_smoke"
    )


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
        probe["kind"] == "ui.grid.viewport"
        and probe["name"] == "offscreen_target_viewport"
        for probe in transition["probes"]
    )


def test_manual_smoke_lists_offscreen_row_target_drag_scenario() -> None:
    if not smoke_test_manual.WPF_GUI_ENABLED:
        pytest.skip(
            "WPF fixture build required for WPF manual-smoke scenario inventory"
        )

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


def test_manual_smoke_exposes_selector_scoped_hover_entrypoint() -> None:
    assert hasattr(smoke_test_manual, "run_wpf_v2_hover_runtime_smoke")
    assert hasattr(smoke_test_manual, "test_wpf_v2_hover_runtime_smoke")


def test_wpf_hover_preflight_foregrounds_before_focus_arms_measurement() -> None:
    import inspect

    source = inspect.getsource(smoke_test_manual.run_wpf_v2_hover_runtime_smoke)
    wrapper = source.split("async def set_hover_focus", 1)[1].split(
        'adapters["ui.set_focus"] = set_hover_focus',
        1,
    )[0]

    assert wrapper.index("await backend.bring_to_front()") < wrapper.index(
        "await base_set_focus(**args)"
    )


def test_wpf_hover_selector_matrix_covers_real_root_and_target_cardinality() -> None:
    cases = smoke_test_manual._wpf_hover_selector_matrix_cases()

    assert [case["id"] for case in cases] == [
        "root_zero",
        "root_many",
        "automation_id_zero",
        "automation_id_many",
        "xpath_zero",
        "xpath_many",
        "name_control_type_zero",
        "name_control_type_many",
        "automation_id_one",
        "xpath_one",
        "name_control_type_one",
        "precedence_automation_id_wins",
        "precedence_xpath_after_automation_id_miss",
    ]
    blocked = cases[:8]
    assert all(case["expect_status"] == "BLOCKED" for case in blocked)
    assert [case["expect_match_count"] for case in blocked] == [0, 2, 0, 2, 0, 2, 0, 2]
    assert all(case["expect_status"] == "PASS" for case in cases[8:])
    by_id = {case["id"]: case for case in cases}
    assert by_id["precedence_automation_id_wins"]["expect_criterion"] == "automationId"
    assert (
        by_id["precedence_xpath_after_automation_id_miss"]["expect_criterion"]
        == "xpath"
    )


def test_wpf_hover_selector_matrix_checks_each_blocked_call_for_pointer_movement() -> (
    None
):
    cases = smoke_test_manual._wpf_hover_selector_matrix_cases()
    results = []
    for case in cases:
        if case["expect_status"] == "BLOCKED":
            results.append(
                {
                    "id": case["id"],
                    "status": "BLOCKED",
                    "phase": case["expect_phase"],
                    "matchCount": case["expect_match_count"],
                    "pointerMutationState": "not_started",
                    "harnessCursorBefore": {"x": 10, "y": 20},
                    "harnessCursorAfter": {"x": 10, "y": 20},
                }
            )
        else:
            results.append(
                {
                    "id": case["id"],
                    "status": "PASS",
                    "matchCount": 1,
                    "resolvedSelector": {"criterion": case["expect_criterion"]},
                    "pointerMutationState": "moved",
                    "foregroundVerified": True,
                    "focusUnchanged": True,
                    "underPointer": True,
                    "hovered": True,
                    "click": False,
                    "button": "none",
                }
            )

    evidence = smoke_test_manual._wpf_hover_selector_matrix_evidence(
        results,
        cursor_before={"x": 10, "y": 20},
        cursor_after_blocked={"x": 11, "y": 20},
    )
    moved_results = [dict(result) for result in results]
    moved_results[0]["harnessCursorAfter"] = {"x": 11, "y": 20}
    moved = smoke_test_manual._wpf_hover_selector_matrix_evidence(
        moved_results,
        cursor_before={"x": 10, "y": 20},
        cursor_after_blocked={"x": 10, "y": 20},
    )

    assert evidence["status"] == "PASS"
    assert moved["status"] == "FAIL"
    assert (
        "root_zero pointer moved before selector uniqueness passed" in moved["failures"]
    )


def test_wpf_hover_plan_arms_after_focus_then_runs_four_measured_transitions() -> None:
    plan = smoke_test_manual._v2_hover_plan(
        program="WpfSmokeApp.dll",
        build_project="tests/fixtures/WpfSmokeApp/WpfSmokeApp.csproj",
    )

    transitions = plan["cases"][0]["transitions"]
    assert len(transitions) == 5
    assert transitions[0]["id"] == "arm_hover_measurement"
    assert transitions[0]["action"] == {
        "kind": "ui.input.ensure_target",
        "selector": {"automation_id": "hoverFocusSentinel", "root_id": "hoverRegion"},
        "require": {"focus": True},
    }

    measured = transitions[1:]
    assert [transition["id"] for transition in measured] == [
        "hover_trigger",
        "hover_flyout_surface",
        "hover_outside",
        "wait_for_hover_close",
    ]
    assert [transition["action"]["kind"] for transition in measured] == [
        "ui.hover",
        "ui.hover",
        "ui.hover",
        "wait",
    ]
    assert [
        transition["action"].get("selector", {}).get("automation_id")
        for transition in measured[:3]
    ] == ["hoverTrigger", "hoverFlyoutSurface", "hoverOutsideSentinel"]
    assert all(
        transition["action"]["selector"]["root_id"] == "hoverRegion"
        for transition in measured[:3]
    )
    assert all(
        transition["action"]["timeout_ms"] == 5000 for transition in measured[:3]
    )
    assert "idle_ms" not in measured[2]
    assert measured[2]["settle"] == {"idle_ms": 100}
    assert measured[3]["action"] == {"kind": "wait", "idle_ms": 900}
    assert plan["cleanup"]["steps"] == [
        {"kind": "debug.stop"},
        {"kind": "process.registry.assert_empty"},
    ]


def test_wpf_hover_live_evidence_accepts_complete_measured_contract() -> None:
    import json

    def status_text(state: str, visible: bool) -> str:
        return json.dumps(
            {
                "state": state,
                "closeDelayMs": 500,
                "surfaceVisible": visible,
                "previewMouseLeftButtonDownCount": 0,
                "previewMouseLeftButtonUpCount": 0,
                "clickCount": 0,
                "focusChangeCount": 0,
                "measurementArmed": True,
            },
            separators=(",", ":"),
        )

    def hover_action(automation_id: str) -> dict[str, Any]:
        focus = {
            "automationId": "hoverFocusSentinel",
            "name": "Arm",
            "controlType": "Button",
        }
        return {
            "status": "PASS",
            "resolvedSelector": {
                "criterion": "automationId",
                "automationId": automation_id,
            },
            "target": {"automationId": automation_id, "controlType": "Button"},
            "matchCount": 1,
            "targetRootHwnd": 101,
            "targetProcessId": 42,
            "foregroundHwndBefore": 101,
            "foregroundHwndAfter": 101,
            "foregroundVerified": True,
            "focusBefore": focus,
            "focusAfter": dict(focus),
            "focusUnchanged": True,
            "targetRect": {"x": 10, "y": 20, "width": 100, "height": 40},
            "requestedPoint": {"x": 60, "y": 40},
            "actualPointer": {"x": 60, "y": 40},
            "hitElement": {"automationId": automation_id, "controlType": "Button"},
            "hitRelation": "self",
            "underPointer": True,
            "hovered": True,
            "click": False,
            "button": "none",
            "timeoutMs": 5000,
            "elapsedMs": 20,
            "pointerMutationState": "moved",
            "runner_input": {
                "source": "runner_injected",
                "kind": "ui.hover",
                "window": "action",
                "route": "hover",
            },
        }

    transition_specs = [
        ("arm_hover_measurement", "hover_armed", "closed", False, [{"status": "PASS"}]),
        (
            "hover_trigger",
            "hover_trigger_status",
            "open_trigger",
            True,
            [hover_action("hoverTrigger")],
        ),
        (
            "hover_flyout_surface",
            "hover_flyout_status",
            "open_flyout",
            True,
            [hover_action("hoverFlyoutSurface")],
        ),
        (
            "hover_outside",
            "hover_pending_status",
            "close_pending",
            True,
            [hover_action("hoverOutsideSentinel")],
        ),
        (
            "wait_for_hover_close",
            "hover_closed_status",
            "closed",
            False,
            [{"status": "PASS"}],
        ),
    ]
    transitions = [
        {
            "id": transition_id,
            "status": "PASS",
            "actions": actions,
            "after": {f"ui.text.{probe_name}": status_text(state, visible)},
        }
        for transition_id, probe_name, state, visible, actions in transition_specs
    ]
    result = smoke_test_manual._wpf_hover_smoke_evidence(
        {
            "status": "PASS",
            "action_count": 5,
            "cases": [{"transitions": transitions}],
            "cleanup": {"status": "PASS", "process_registry_after": 0},
        }
    )

    assert result["status"] == "PASS"
    assert result["failures"] == []


def test_manual_smoke_lists_wpf_selector_scoped_hover_scenario() -> None:
    scenario_names = {name for name, _fn in smoke_test_manual.get_scenarios()}
    assert "WPF V2 Selector-Scoped Hover Runtime Smoke" in scenario_names

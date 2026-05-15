from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_schema import validate_plan

EXAMPLE_PATH = Path("docs/examples/runtime-smoke-v2-drag-drop-grid.json")
README_PATH = Path("README.md")
PLAYBOOK_PATH = Path("docs/PRODUCTION-TESTING-PLAYBOOK.md")


class DocsExampleSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.drag_requests: list[dict[str, Any]] = []
        self.viewport_requests: list[dict[str, Any]] = []
        self.drag_results: deque[dict[str, Any]] = deque(
            [
                {
                    "status": "PASS",
                    "backend": "fake-flaui",
                    "route_evidence": {
                        "move_points": [
                            {"relative_to": "source", "x": 0.5, "y": 0.5},
                            {"relative_to": "viewport", "x": 0.5, "y": 0.92},
                            {"relative_to": "viewport", "x": 0.5, "y": 0.98},
                        ],
                        "hold_points": [
                            {"relative_to": "viewport", "x": 0.5, "y": 0.92, "hold_ms": 750},
                            {"relative_to": "viewport", "x": 0.5, "y": 0.98, "hold_ms": 1000},
                        ],
                        "final_pointer": {"relative_to": "viewport", "x": 0.5, "y": 0.82},
                    },
                    "selected_payload": {
                        "before": ["ROW-010", "ROW-011"],
                        "after": ["ROW-010", "ROW-011"],
                    },
                },
                {
                    "status": "PASS",
                    "backend": "fake-flaui",
                    "route_evidence": {
                        "move_points": [
                            {"relative_to": "source", "x": 0.5, "y": 0.5},
                            {"relative_to": "viewport", "x": 0.98, "y": 0.5},
                        ],
                        "final_pointer": {"relative_to": "viewport", "x": 0.98, "y": 0.5},
                    },
                    "no_op": {"expected": True, "reason": "outside_drop_zone"},
                    "cleanup": {
                        "modifier_cleanup": {"released": []},
                        "pointer_cleanup": {"left_button_released": True},
                    },
                },
            ]
        )
        self.viewport_results: deque[dict[str, Any]] = deque(
            [
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=8,
                        last=17,
                        visible=["ROW-008", "ROW-009", "ROW-010", "ROW-011", "ROW-012"],
                        selected=["ROW-010", "ROW-011"],
                    ),
                },
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=14,
                        last=23,
                        visible=["ROW-008", "ROW-009", "ROW-011", "ROW-012", "ROW-010"],
                        selected=["ROW-010", "ROW-011"],
                    ),
                },
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=14,
                        last=23,
                        visible=["ROW-014", "ROW-015", "ROW-016", "ROW-017", "ROW-018"],
                    ),
                },
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=14,
                        last=23,
                        visible=["ROW-014", "ROW-015", "ROW-016", "ROW-017", "ROW-018"],
                    ),
                },
            ]
        )

    async def drag(self, **request: Any) -> dict[str, Any]:
        self.drag_requests.append(request)
        return self.drag_results.popleft()

    async def grid_viewport(self, **request: Any) -> dict[str, Any]:
        self.viewport_requests.append(request)
        return self.viewport_results.popleft()


def _load_example() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _viewport_snapshot(
    *,
    first: int,
    last: int,
    visible: list[str],
    selected: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "first_visible_index": first,
        "last_visible_index": last,
        "visible_rows": [
            {"index": first + offset, "identity": identity}
            for offset, identity in enumerate(visible)
        ],
        "selected_rows": [
            {"index": first + offset, "identity": identity}
            for offset, identity in enumerate(selected or [])
        ],
        "row_count": 100,
        "identity_strategy": {"kind": "configured_column", "column": "StableRowId"},
    }


def _actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        transition["action"]
        for case in plan.get("cases", [])
        for transition in case.get("transitions", [])
        if isinstance(transition.get("action"), dict)
    ]


def _probes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        probe
        for case in plan.get("cases", [])
        for transition in case.get("transitions", [])
        for probe in transition.get("probes", [])
        if isinstance(probe, dict)
    ]


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def assert_drag_drop_docs_contract(plan: dict[str, Any]) -> None:
    assert plan["schema"] == "netcoredbg.runtime_smoke.v2"
    assert validate_plan(plan) == []

    drag_actions = [action for action in _actions(plan) if action.get("kind") == "ui.drag"]
    assert drag_actions
    assert any(action.get("source", {}).get("row_identity") for action in drag_actions)
    assert all(action.get("identity", {}).get("column") for action in drag_actions)
    assert any(
        action.get("expect", {}).get("selected_payload_preserved") is True
        for action in drag_actions
    )
    assert any(action.get("expect", {}).get("no_op") is True for action in drag_actions)
    assert any(
        waypoint.get("relative_to") == "viewport" and int(waypoint.get("hold_ms", 0)) > 0
        for action in drag_actions
        for waypoint in action.get("path", [])
        if isinstance(waypoint, dict)
    )

    viewport_probes = [probe for probe in _probes(plan) if probe.get("kind") == "ui.grid.viewport"]
    assert viewport_probes
    assert any(probe.get("phase") == "both" for probe in viewport_probes)
    assert all(probe.get("identity") for probe in viewport_probes)
    assert any(
        probe.get("expect", {}).get("identity_order_preserved") in {True, False}
        for probe in viewport_probes
    )
    assert any(
        probe.get("expect", {}).get("row_count_preserved") is True
        for probe in viewport_probes
    )

    notes = " ".join(
        str(note)
        for case in plan.get("cases", [])
        for note in case.get("notes", [])
    )
    assert "BLOCKED" in notes
    assert "route_evidence" in notes
    assert "ui.grid.viewport" in notes


def test_drag_drop_grid_example_declares_documented_protocol_contract() -> None:
    assert_drag_drop_docs_contract(_load_example())


def test_drag_drop_grid_example_rejects_missing_row_identity_checks() -> None:
    plan = copy.deepcopy(_load_example())
    for action in _actions(plan):
        action.get("source", {}).pop("row_identity", None)
        action.pop("identity", None)
        action.get("expect", {}).pop("selected_payload_preserved", None)
    for probe in _probes(plan):
        probe.pop("identity", None)
        probe.get("expect", {}).pop("identity_order_preserved", None)
        probe.get("expect", {}).pop("row_count_preserved", None)

    with pytest.raises(AssertionError):
        assert_drag_drop_docs_contract(plan)


@pytest.mark.asyncio
async def test_drag_drop_grid_example_runs_through_v2_parser_with_fake_ui_evidence() -> None:
    session = DocsExampleSmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.drag": session.drag,
            "ui.grid.viewport": session.grid_viewport,
        },
    ).run(_load_example())

    assert result["status"] == "PASS"
    assert "ui.drag" in result["accepted_action_kinds"]
    assert "ui.grid.viewport" in result["accepted_probe_kinds"]
    assert result["action_count"] == 2
    assert len(session.drag_requests) == 2
    assert len(session.viewport_requests) == 4
    assert session.drag_requests[0]["source"]["row_identity"] == "ROW-010"
    assert session.drag_requests[0]["path"] != session.drag_requests[1]["path"]


def test_readme_and_playbook_document_customer_mode_drag_drop_gate() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
    example_link = "docs/examples/runtime-smoke-v2-drag-drop-grid.json"
    winforms_boundary = (
        "WinForms `dragList` primitive smoke is not a substitute for WPF DataGrid "
        "CR-001 acceptance"
    )

    assert example_link in readme
    assert example_link in playbook
    assert winforms_boundary in _collapsed(readme)
    assert winforms_boundary in _collapsed(playbook)
    assert "PRODUCT_WORKS" in playbook
    assert "PARTIALLY_WORKS" in playbook
    assert "BROKEN" in playbook
    assert "BLOCK_RELEASE" in playbook

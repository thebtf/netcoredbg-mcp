from __future__ import annotations

import copy
import json
import re
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_schema import (
    validate_diagnostic_schema_example,
    validate_plan,
)
from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases

EXAMPLE_PATH = Path("docs/examples/runtime-smoke-v2-drag-drop-grid.json")
SELECTOR_SAFETY_EXAMPLE_PATH = Path(
    "docs/examples/runtime-smoke-v2-selector-safety.json"
)
NOVASCRIPT_ACTION_ORACLE_APP_DIAGNOSTICS_EXAMPLE_PATH = Path(
    "docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json"
)
APP_DIAGNOSTICS_WAIT_JSON_EXAMPLE_PATH = Path(
    "docs/examples/runtime-smoke-app-diagnostics-wait-json.json"
)
APP_DIAGNOSTICS_POLL_EXAMPLE_PATH = Path(
    "docs/examples/runtime-smoke-app-diagnostics-poll.json"
)
README_PATH = Path("README.md")
README_RU_PATH = Path("README.ru.md")
PLAYBOOK_PATH = Path("docs/PRODUCTION-TESTING-PLAYBOOK.md")
RELEASE_PROTOCOL_PATH = Path("docs/RELEASE-PROTOCOL.md")
AGENTS_PATH = Path("AGENTS.md")


class DocsExampleSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.drag_requests: list[dict[str, Any]] = []
        self.ensure_visible_requests: list[dict[str, Any]] = []
        self.viewport_requests: list[dict[str, Any]] = []
        self.operation_order: list[str] = []
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
                            {
                                "relative_to": "viewport",
                                "x": 0.5,
                                "y": 0.92,
                                "hold_ms": 750,
                            },
                            {
                                "relative_to": "viewport",
                                "x": 0.5,
                                "y": 0.98,
                                "hold_ms": 1000,
                            },
                        ],
                        "final_pointer": {
                            "relative_to": "viewport",
                            "x": 0.5,
                            "y": 0.82,
                        },
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
                        "final_pointer": {
                            "relative_to": "viewport",
                            "x": 0.98,
                            "y": 0.5,
                        },
                    },
                    "no_op": {"expected": True, "reason": "outside_drop_zone"},
                    "cleanup": {
                        "modifier_cleanup": {"released": []},
                        "pointer_cleanup": {"left_button_released": True},
                    },
                },
                {
                    "status": "PASS",
                    "backend": "fake-flaui",
                    "drop_ensure_visible_result": {
                        "status": "PASS",
                        "already_visible": False,
                        "resolved_row": {
                            "identity": "ROW-060",
                            "index": 60,
                        },
                    },
                    "route_evidence": {
                        "move_points": [
                            {"relative_to": "source", "x": 0.5, "y": 0.5},
                            {"relative_to": "viewport", "x": 0.5, "y": 0.9},
                        ],
                        "hold_points": [
                            {
                                "relative_to": "viewport",
                                "x": 0.5,
                                "y": 0.9,
                                "hold_ms": 750,
                            },
                        ],
                        "final_pointer": {
                            "relative_to": "row",
                            "row_identity": "ROW-060",
                            "position": "center",
                        },
                    },
                    "selected_payload": {
                        "before": ["ROW-010"],
                        "after": ["ROW-010"],
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
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=8,
                        last=17,
                        visible=["ROW-008", "ROW-009", "ROW-010", "ROW-011", "ROW-012"],
                        selected=["ROW-010"],
                    ),
                },
                {
                    "status": "PASS",
                    "snapshot": _viewport_snapshot(
                        first=56,
                        last=65,
                        visible=["ROW-056", "ROW-057", "ROW-058", "ROW-059", "ROW-060"],
                        selected=["ROW-010"],
                    ),
                },
            ]
        )

    async def drag(self, **request: Any) -> dict[str, Any]:
        self.operation_order.append("ui.drag")
        self.drag_requests.append(request)
        return self.drag_results.popleft()

    async def grid_ensure_visible(self, **request: Any) -> dict[str, Any]:
        self.operation_order.append("ui.grid.ensure_visible")
        self.ensure_visible_requests.append(request)
        return {
            "status": "PASS",
            "already_visible": False,
            "resolved_row": {
                "identity": request.get("row", {}).get("identity"),
                "index": 10,
            },
        }

    async def grid_viewport(self, **request: Any) -> dict[str, Any]:
        self.operation_order.append("ui.grid.viewport")
        self.viewport_requests.append(request)
        return self.viewport_results.popleft()


class SelectorSafetySmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.launch_requests: list[dict[str, Any]] = []
        self.invoke_requests: list[dict[str, Any]] = []
        self.property_requests: list[dict[str, Any]] = []

    async def launch(self, **request: Any) -> dict[str, Any]:
        self.launch_requests.append(request)
        return {"status": "PASS", "profile": "selector-safety"}

    async def invoke(self, **request: Any) -> dict[str, Any]:
        self.invoke_requests.append(request)
        return {
            "status": "BLOCKED",
            "reason": "selector result did not match exact automation_id",
            "requested": {
                "selector": request.get("selector"),
            },
            "accepted": {"selector_policy": "exact automation_id match"},
            "next_step": "Inspect the scoped tree and adjust the selector.",
        }

    async def get_property(self, **request: Any) -> dict[str, Any]:
        self.property_requests.append(request)
        return {"status": "PASS", "value": "Selector side effects: 0"}

    async def debug_stop(self, **_request: Any) -> dict[str, Any]:
        return {"status": "PASS"}

    async def process_registry_count(self) -> dict[str, Any]:
        return {"status": "PASS", "count": 0}


def _load_example() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _load_selector_safety_example() -> dict[str, Any]:
    return json.loads(SELECTOR_SAFETY_EXAMPLE_PATH.read_text(encoding="utf-8"))


def _load_novascript_action_oracle_app_diagnostics_example() -> dict[str, Any]:
    return json.loads(
        NOVASCRIPT_ACTION_ORACLE_APP_DIAGNOSTICS_EXAMPLE_PATH.read_text(
            encoding="utf-8"
        )
    )


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

    actions = _actions(plan)
    drag_actions = [action for action in actions if action.get("kind") == "ui.drag"]
    assert drag_actions
    assert not any(action.get("kind") == "ui.grid.ensure_visible" for action in actions)
    assert any(action.get("source", {}).get("row_identity") for action in drag_actions)
    assert all(action.get("identity", {}).get("column") for action in drag_actions)
    first_row_drag_index, first_row_drag = next(
        (index, action)
        for index, action in enumerate(actions)
        if action.get("kind") == "ui.drag"
        and action.get("source", {}).get("row_identity")
    )
    first_drag_selector = first_row_drag.get("source", {}).get("selector")
    first_drag_identity = first_row_drag.get("source", {}).get("row_identity")
    first_drag_identity_column = first_row_drag.get("identity", {}).get("column")
    assert first_row_drag_index == 0
    assert first_drag_selector
    assert first_drag_identity
    assert first_drag_identity_column
    assert first_row_drag.get("ensure_visible") is True
    assert first_row_drag.get("rows", {}).get("visible_only") is True
    assert first_drag_identity_column in first_row_drag.get("columns", [])
    assert isinstance(first_row_drag.get("max_scrolls"), int)
    assert first_row_drag["max_scrolls"] > 0
    assert isinstance(first_row_drag.get("scroll_settle_ms"), int)
    assert first_row_drag["scroll_settle_ms"] > 0
    assert any(
        action.get("expect", {}).get("selected_payload_preserved") is True
        for action in drag_actions
    )
    assert any(action.get("expect", {}).get("no_op") is True for action in drag_actions)
    assert any(
        waypoint.get("relative_to") == "viewport"
        and int(waypoint.get("hold_ms", 0)) > 0
        for action in drag_actions
        for waypoint in action.get("path", [])
        if isinstance(waypoint, dict)
    )
    offscreen_drop_actions = [
        action
        for action in drag_actions
        if action.get("drop", {}).get("ensure_visible") is True
    ]
    assert offscreen_drop_actions
    for action in offscreen_drop_actions:
        assert action.get("ensure_visible") is True
        drop = action["drop"]
        assert drop.get("selector") == action["source"]["selector"]
        assert drop.get("row_identity")
        assert drop.get("identity", {}).get("column") == action.get("identity", {}).get(
            "column"
        )
        assert drop.get("rows", {}).get("visible_only") is True
        assert drop.get("rows", {}).get("max") == action.get("rows", {}).get("max")
        assert drop.get("columns") == action.get("columns")
        assert isinstance(drop.get("max_scrolls"), int)
        assert drop["max_scrolls"] > first_row_drag["max_scrolls"]
        assert isinstance(drop.get("scroll_settle_ms"), int)
        assert drop["scroll_settle_ms"] > 0

    viewport_probes = [
        probe for probe in _probes(plan) if probe.get("kind") == "ui.grid.viewport"
    ]
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
        str(note) for case in plan.get("cases", []) for note in case.get("notes", [])
    )
    assert "BLOCKED" in notes
    assert "route_evidence" in notes
    assert "ui.grid.viewport" in notes
    assert "offscreen" in notes
    assert "row-based drop endpoint" in notes
    assert "drop.ensure_visible=true" in notes
    assert "raw viewport guessing" in notes
    assert "CR-075" in notes
    assert "broad #270 remains open" in notes


def test_drag_drop_grid_example_declares_documented_protocol_contract() -> None:
    assert_drag_drop_docs_contract(_load_example())


def test_selector_safety_example_declares_blocked_no_mutation_contract() -> None:
    plan = _load_selector_safety_example()

    assert plan["schema"] == "netcoredbg.runtime_smoke.v2"
    assert validate_plan(plan) == []

    actions = _actions(plan)
    assert len(actions) == 1
    action = actions[0]
    assert action["kind"] == "ui.invoke"
    assert action["selector"] == {
        "automation_id": "playButton",
        "control_type": "Button",
        "root_id": "selectorSafetyPanel",
    }
    assert action["expect"]["status"] == "BLOCKED"
    assert action["expect"]["no_mutation"] is True

    probes = _probes(plan)
    assert len(probes) == 1
    assert probes[0]["kind"] == "ui.property"
    assert probes[0]["phase"] == "both"
    assert probes[0]["selector"]["automation_id"] == "selectorSafetyStatus"
    assert probes[0]["expected"] == "Selector side effects: 0"

    notes = " ".join(
        str(note) for case in plan.get("cases", []) for note in case.get("notes", [])
    )
    assert "BLOCKED" in notes
    assert "No-mutation proof" in notes


@pytest.mark.asyncio
async def test_selector_safety_example_runs_through_v2_parser_with_blocked_evidence() -> (
    None
):
    session = SelectorSafetySmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={
            "launch": session.launch,
            "ui.invoke": session.invoke,
            "ui.get_property": session.get_property,
            "debug.stop": session.debug_stop,
            "process.registry.count": session.process_registry_count,
        },
    ).run(_load_selector_safety_example())

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "selector result did not match exact automation_id"
    assert result["action_count"] == 1
    assert result["baseline"]["status"] == "PASS"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["process_registry_after"] == 0
    transition = result["cases"][0]["transitions"][0]
    assert (
        transition["before"]["ui.property.selector_sentinel_after"]
        == "Selector side effects: 0"
    )
    assert (
        transition["after"]["ui.property.selector_sentinel_after"]
        == "Selector side effects: 0"
    )
    assert "ui.property.selector_sentinel_after" not in transition["diff"]
    assert session.launch_requests
    assert len(session.property_requests) == 2
    assert session.invoke_requests == [
        {
            "selector": {
                "automation_id": "playButton",
                "control_type": "Button",
                "root_id": "selectorSafetyPanel",
            }
        }
    ]


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


def test_drag_drop_grid_example_rejects_missing_inline_ensure_visible_preflight() -> (
    None
):
    plan = copy.deepcopy(_load_example())
    first_action = plan["cases"][0]["transitions"][0]["action"]
    first_action.pop("ensure_visible", None)
    first_action.pop("max_scrolls", None)
    first_action.pop("scroll_settle_ms", None)

    with pytest.raises(AssertionError):
        assert_drag_drop_docs_contract(plan)


def test_drag_drop_grid_example_rejects_missing_offscreen_drop_note() -> None:
    plan = copy.deepcopy(_load_example())
    for case in plan["cases"]:
        case["notes"] = [
            note
            for note in case.get("notes", [])
            if "drop.ensure_visible" not in str(note)
        ]

    with pytest.raises(AssertionError):
        assert_drag_drop_docs_contract(plan)


def test_drag_drop_grid_example_rejects_missing_offscreen_row_target_drop_contract() -> (
    None
):
    plan = copy.deepcopy(_load_example())
    for action in _actions(plan):
        drop = action.get("drop")
        if isinstance(drop, dict):
            drop.pop("ensure_visible", None)
            drop.pop("row_identity", None)
            drop.pop("selector", None)

    with pytest.raises(AssertionError):
        assert_drag_drop_docs_contract(plan)


@pytest.mark.asyncio
async def test_drag_drop_grid_example_runs_through_v2_parser_with_fake_ui_evidence() -> (
    None
):
    session = DocsExampleSmokeSession()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.drag": session.drag,
            "ui.grid.ensure_visible": session.grid_ensure_visible,
            "ui.grid.viewport": session.grid_viewport,
        },
    ).run(_load_example())

    assert result["status"] == "PASS"
    assert "ui.grid.ensure_visible" in result["accepted_action_kinds"]
    assert "ui.drag" in result["accepted_action_kinds"]
    assert "ui.grid.viewport" in result["accepted_probe_kinds"]
    assert result["action_count"] == 3
    assert len(session.ensure_visible_requests) == 2
    assert len(session.drag_requests) == 3
    assert len(session.viewport_requests) == 6
    assert session.operation_order.index(
        "ui.grid.ensure_visible"
    ) < session.operation_order.index("ui.drag")
    assert session.ensure_visible_requests[0]["selector"] == {
        "automation_id": "DataGridUnderTest",
        "control_type": "DataGrid",
    }
    assert session.ensure_visible_requests[0]["row"] == {"identity": "ROW-010"}
    assert session.ensure_visible_requests[0]["identity"] == {"column": "StableRowId"}
    assert session.ensure_visible_requests[0]["rows"] == {
        "visible_only": True,
        "max": 20,
    }
    assert session.ensure_visible_requests[0]["columns"] == ["StableRowId"]
    assert session.ensure_visible_requests[1]["row"] == {"identity": "ROW-010"}
    assert session.ensure_visible_requests[1]["identity"] == {"column": "StableRowId"}
    assert session.ensure_visible_requests[1]["rows"] == {
        "visible_only": True,
        "max": 20,
    }
    assert session.ensure_visible_requests[1]["columns"] == ["StableRowId"]
    assert session.drag_requests[0]["source"]["row_identity"] == "ROW-010"
    assert session.drag_requests[0]["path"] != session.drag_requests[1]["path"]
    offscreen_drop = session.drag_requests[2]["drop"]
    assert offscreen_drop == {
        "selector": {
            "automation_id": "DataGridUnderTest",
            "control_type": "DataGrid",
        },
        "row_identity": "ROW-060",
        "identity": {"column": "StableRowId"},
        "rows": {"visible_only": True, "max": 20},
        "columns": ["StableRowId"],
        "ensure_visible": True,
        "max_scrolls": 24,
        "scroll_settle_ms": 25,
        "position": "center",
    }
    offscreen_action_result = result["cases"][2]["transitions"][0]["actions"][0]
    assert offscreen_action_result["drop_ensure_visible_result"]["status"] == "PASS"
    assert offscreen_action_result["drop_ensure_visible_result"]["resolved_row"] == {
        "identity": "ROW-060",
        "index": 60,
    }
    assert offscreen_action_result["route_evidence"]["final_pointer"] == {
        "relative_to": "row",
        "row_identity": "ROW-060",
        "position": "center",
    }


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
    assert "offscreen row-target" in playbook
    assert "drop.ensure_visible=true" in playbook
    assert "row-based" in playbook
    assert "bounded CR-075 customer-mode proof contract" in playbook
    assert "#270" in playbook
    assert "Release-Candidate Consumer Environment" in playbook
    assert "uv venv --python" in playbook
    assert "uv pip install --python $ConsumerPython" in playbook
    assert "source-tree `uv run` commands are supporting checks only" in playbook
    assert "Installed CLI Consumer Smoke" in playbook
    assert "Installed MCP Client Exchange" in playbook
    assert "installed release-candidate server" in playbook
    assert (
        "fail closed before side effects if target-side realization hides the drag"
        in playbook
    )


def test_readmes_document_non_mutating_source_checkout_mcp_launch() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    readme_ru = README_RU_PATH.read_text(encoding="utf-8")

    for document in (readme, readme_ru):
        assert "\nnetcoredbg-mcp --project-from-cwd\n" in document
        assert re.search(
            r"uv\s+sync\s+--locked\s+--project\s+\S+.*?"
            r"cd\s+\S*my-dotnet-project\s+"
            r"uv\s+run\s+--no-sync\s+--project\s+\S+\s+"
            r"netcoredbg-mcp\s+--project-from-cwd",
            document,
            re.DOTALL,
        )
        assert re.search(
            r'"run",\s*"--no-sync",\s*"--project",\s*"[^"]+",\s*'
            r'"netcoredbg-mcp",\s*"--project-from-cwd"',
            document,
            re.DOTALL,
        )
        assert not re.search(r"\buv\s+run\s+--project\b", document)


def test_release_policy_uses_one_consumer_first_autonomy_contract() -> None:
    agents = AGENTS_PATH.read_text(encoding="utf-8")
    protocol = RELEASE_PROTOCOL_PATH.read_text(encoding="utf-8")
    canonical_gate = "primary UXDD consumer-mode release gate"

    assert canonical_gate.lower() in agents.lower()
    assert protocol.lower().count(canonical_gate.lower()) >= 5
    assert "Missing release intent, MAJOR/breaking change" not in protocol

    dependent_slice_gate = "no dependent slice in the same integration wave remains active"
    assert dependent_slice_gate in agents
    assert dependent_slice_gate in protocol

    release_steps = _collapsed(agents).lower()
    assert release_steps.index(
        "run the primary uxdd consumer-mode release gate"
    ) < release_steps.index("run the remaining local pre-pr protocol gates")


def test_readme_documents_runner_controlled_input_provenance_model() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    readme_ru = README_RU_PATH.read_text(encoding="utf-8")
    english = _collapsed(readme)
    russian = _collapsed(readme_ru)

    for document in (english, russian):
        for term in (
            "runner_injected",
            "foreign_injected",
            "physical",
            "ui.drag",
            "CLEAN_PROVEN",
            "DIRTY_UNPROVEN",
        ):
            assert term in document
        assert "RUNNER_GLOBAL_INPUT_AMBIGUOUS" not in document
        assert "runner_emulated_input" not in document

    assert "product verdict" in english
    assert "product verdict" in russian
    assert "full isolation is proven" not in english
    assert "полная изоляция доказана" not in russian


def test_readme_and_playbook_document_diagnostic_schema_gate() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
    diagnostic_examples = {
        "docs/examples/runtime-smoke-oracle-pack.json",
        "docs/examples/runtime-smoke-app-diagnostics.json",
        "docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json",
        "docs/examples/runtime-smoke-semantic-probe.json",
        "docs/examples/runtime-smoke-tracepoint-guardrail.json",
    }
    required_terms = {
        "netcoredbg.runtime_smoke.diagnostics.v1",
        "PASS",
        "BLOCKED",
        "FAIL",
        "max_text_length",
        "max_list_items",
        "max_json_bytes",
        "raw_tree",
        "window_tree",
        "ui_tree",
        "screenshot_base64",
        "access_token",
        "api_key",
        "password",
        "secret",
        "backend_result",
        "exception",
        "raw_output",
        "stack",
        "freshness",
        "expected_process_name",
        "expected_modules",
        "loaded_sources",
        "symbolStatus",
        "live-target PDB/process proof",
        "allowed_when",
        "blocked_when",
        "unsafe_when",
        "debug.tracepoint.remove",
    }
    winforms_boundary = (
        "WinForms `dragList` primitive smoke is not a substitute for WPF DataGrid "
        "CR-001 acceptance"
    )

    for document in (readme, playbook):
        for example_path in diagnostic_examples:
            assert example_path in document
        for term in required_terms:
            assert term in document
    collapsed_playbook = _collapsed(playbook)
    assert collapsed_playbook.index(
        _collapsed(winforms_boundary)
    ) < collapsed_playbook.index("### 8. Supporting Runtime-Smoke Diagnostic Schema Contract")


def test_readme_and_playbook_document_novascript_action_oracle_app_diagnostics_gate() -> (
    None
):
    readme = README_PATH.read_text(encoding="utf-8")
    playbook = PLAYBOOK_PATH.read_text(encoding="utf-8")
    example_path = (
        "docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json"
    )
    replay_packet_path = (
        "docs/reproduction-scenarios/"
        "novascript-action-oracle-app-diagnostics-replay-2026-06-21.md"
    )
    required_terms = {
        "NovaScript Action-Oracle App-Diagnostics Consumer Gate",
        replay_packet_path,
        "& $ConsumerCli --version",
        "netcoredbg-mcp <TARGET_VERSION>",
        "installed release-candidate entry point",
        "0.20.5",
        "<NOVASCRIPT_PROCESS_NAME>",
        "<NOVASCRIPT_PRIMARY_MODULE>",
        "run_runtime_smoke",
        "runtime_smoke_start",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
        "runtime_smoke_stop",
        "app_diagnostics",
        "novascript-action-oracle",
        "action_oracle_diagnostics",
        "freshness",
        "cleanup",
        "PRODUCT_WORKS",
        "PARTIALLY_WORKS",
        "BROKEN",
    }

    assert example_path in readme
    assert example_path in playbook
    assert replay_packet_path in playbook
    assert (
        "NovaScript consumers validating the current action-oracle app-diagnostics path"
        in readme
    )
    for term in required_terms:
        assert term in playbook
    assert "does not replace the CR-003 DataGrid drag/drop replay gate" in _collapsed(
        playbook
    )


def test_novascript_action_oracle_app_diagnostics_example_is_consumer_ready() -> None:
    plan = _load_novascript_action_oracle_app_diagnostics_example()

    assert "v0.20.5" in plan["name"]
    assert "netcoredbg-mcp 0.20.5" in plan["description"]
    assert "v0.20.0" not in plan["name"]
    assert "v0.20.0" not in plan["description"]
    assert "v0.20.4" not in plan["name"]
    assert "v0.20.4" not in plan["description"]
    assert "0.19.0" not in plan["name"]
    assert "0.19.0" not in plan["description"]
    assert validate_plan(plan) == []
    diagnostic_launch = plan["diagnostics"]["app_diagnostics"]["diagnostic_launch"]
    assert diagnostic_launch["evidence"] == {
        "directory": ".agent/runtime-smoke/app-diagnostics",
        "path": ".agent/runtime-smoke/app-diagnostics/novascript-action-oracle.json",
    }
    assert plan["baseline"]["steps"][0]["kind"] == "isolated_profile.launch"
    launch = plan["baseline"]["steps"][0]["launch"]
    assert launch["pre_build"] is True
    assert launch["env"]["NOVASCRIPT_UI_TEST_MODE"] == "1"
    assert launch["env"]["NOVASCRIPT_UI_TEST_AUTO_OPEN_DOCUMENT"] == "1"
    assert launch["env"]["NOVASCRIPT_UI_TEST_DISABLE_RESTORE"] == "1"

    generated, errors = expand_generated_cases(plan)

    assert errors == []
    assert len(generated) == 1
    case = generated[0]
    assert case["id"] == "action_oracle_diagnostics"
    transition = case["transitions"][0]
    assert transition["action"]["kind"] == "ui.invoke"
    assert transition["settle"] == {"idle_ms": 500}
    probes = transition["probes"]
    assert len(probes) == 1
    probe = probes[0]
    assert probe["kind"] == "app_diagnostics"
    assert probe["phase"] == "after"
    assert probe["name"] == "novascript_action_oracle"
    assert "wait_json" not in probe
    assert "poll" not in probe
    assert probe["schema"] == "netcoredbg.runtime_smoke.diagnostics.v1"
    assert probe["app"] == {
        "name": "NovaScript",
        "process_name": "<NOVASCRIPT_PROCESS_NAME>",
        "expected_modules": ["<NOVASCRIPT_PRIMARY_MODULE>"],
        "require_active_process": True,
    }
    assert probe["artifacts"] == {
        "expected": [
            ".agent/runtime-smoke/app-diagnostics/novascript-action-oracle.json"
        ]
    }
    assert validate_diagnostic_schema_example(probe, kind="app_diagnostics") == []


def test_runtime_smoke_examples_remain_schema_compatible() -> None:
    examples = [
        Path("docs/examples/runtime-smoke-v2-drag-drop-grid.json"),
        Path("docs/examples/runtime-smoke-v2-selector-safety.json"),
        Path("docs/examples/runtime-smoke-v2-handwritten.json"),
        Path("docs/examples/runtime-smoke-v2-matrix-toggle.json"),
        Path("docs/examples/runtime-smoke-v2-state-only-file-json-matrix.json"),
        Path(
            "docs/examples/runtime-smoke-novascript-action-oracle-app-diagnostics.json"
        ),
        Path("docs/examples/runtime-smoke-wpf-workflow-plan.json"),
    ]

    for path in examples:
        plan = json.loads(path.read_text(encoding="utf-8"))
        assert validate_plan(plan) == []


def test_diagnostic_examples_remain_schema_compatible() -> None:
    examples = {
        "oracle_pack": Path("docs/examples/runtime-smoke-oracle-pack.json"),
        "app_diagnostics": Path("docs/examples/runtime-smoke-app-diagnostics.json"),
        "semantic_probe": Path("docs/examples/runtime-smoke-semantic-probe.json"),
        "tracepoint_guardrail": Path(
            "docs/examples/runtime-smoke-tracepoint-guardrail.json"
        ),
    }
    payloads: dict[str, dict[str, Any]] = {}

    for kind, path in examples.items():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payloads[kind] = payload
        assert validate_diagnostic_schema_example(payload, kind=kind) == []

    limits = {"max_text_length": 240, "max_list_items": 8, "max_json_bytes": 32768}
    assert payloads["oracle_pack"]["limits"] == limits
    assert payloads["app_diagnostics"]["limits"] == limits
    assert {"PASS", "BLOCKED", "FAIL"}.issubset(
        {
            payloads["oracle_pack"]["status"],
            payloads["app_diagnostics"]["status"],
            payloads["semantic_probe"]["status"],
            payloads["semantic_probe"]["backend_result"]["status"],
            payloads["tracepoint_guardrail"]["status"],
        }
    )
    assert (
        "debug.tracepoint.remove"
        in payloads["tracepoint_guardrail"]["cleanup"]["operations"]
    )


def test_app_diagnostics_wait_json_example_remains_schema_compatible() -> None:
    payload = json.loads(
        APP_DIAGNOSTICS_WAIT_JSON_EXAMPLE_PATH.read_text(encoding="utf-8")
    )

    assert validate_diagnostic_schema_example(payload, kind="app_diagnostics") == []
    assert payload["wait_json"]["path"] == ".agent/runtime-smoke/app-diagnostics.json"
    assert payload["wait_json"]["condition"] == {
        "jsonpath": "$.status",
        "expected": "PASS",
    }
    assert payload["wait_json"]["timeout_ms"] == 5000
    assert payload["wait_json"]["poll_interval_ms"] == 100
    assert payload["observations"] == []


def test_app_diagnostics_poll_example_remains_schema_compatible() -> None:
    payload = json.loads(APP_DIAGNOSTICS_POLL_EXAMPLE_PATH.read_text(encoding="utf-8"))

    assert validate_diagnostic_schema_example(payload, kind="app_diagnostics") == []
    assert payload["poll"]["path"] == ".agent/runtime-smoke/app-diagnostics"
    assert payload["poll"]["pattern"] == "app-diagnostics-*.json"
    assert payload["poll"]["since"] == {
        "mtime_ns": 0,
        "name": "app-diagnostics-0000.json",
    }
    assert payload["poll"]["timeout_ms"] == 5000
    assert payload["poll"]["poll_interval_ms"] == 100
    assert "wait_json" not in payload
    assert payload["observations"] == []

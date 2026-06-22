"""Runtime smoke plan schema diagnostics tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_schema import validate_plan
from netcoredbg_mcp.session.state import DebugState


class SchemaSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=[],
            output_sequence=0,
            output_trimmed_before=0,
            process_id=None,
            process_name=None,
            modules=[],
            loaded_sources={},
        )
        self.launch_calls = 0
        self.adapter_calls: list[tuple[str, dict[str, Any]]] = []

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        return {"status": "PASS", "reason": "launched"}

    async def grid_get_state(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.get_state", request))
        return {"status": "PASS", "visible_rows": [], "selected_rows": []}

    async def grid_ensure_visible(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.ensure_visible", request))
        return {
            "status": "PASS",
            "already_visible": False,
            "resolved_row": dict(request.get("row") or {}),
        }

    async def grid_assert_range(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.assert_range", request))
        return {
            "status": "PASS",
            "asserted_range": {
                "start_index": request.get("start_index"),
                "end_index": request.get("end_index"),
            },
        }

    async def grid_select_row(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.select_row", request))
        return {"status": "PASS", "selected_row": dict(request.get("row") or {})}

    async def grid_click_row(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.click_row", request))
        return {"status": "PASS", "clicked": True, "row": dict(request.get("row") or {})}

    async def grid_right_click_row(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.right_click_row", request))
        return {
            "status": "PASS",
            "clicked": True,
            "right_clicked": True,
            "row": dict(request.get("row") or {}),
        }

    async def grid_double_click_row(self, **request: Any) -> dict[str, Any]:
        self.adapter_calls.append(("ui.grid.double_click_row", request))
        return {
            "status": "PASS",
            "clicked": True,
            "double_clicked": True,
            "row": dict(request.get("row") or {}),
        }


@pytest.mark.asyncio
async def test_invalid_plan_returns_self_describing_schema_help_before_launch() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "launch": {"program": "unused.exe"},
            "evidence": "not-a-list",
            "steps": [{"id": "assert-output", "op": "debug.output_assert_since"}],
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert "evidence must be a list" in result["validation_errors"]
    assert (
        "steps[0].checkpoint is required for op debug.output_assert_since"
        in result["validation_errors"]
    )
    assert session.launch_calls == 0

    assert result["accepted_schema_values"] == [
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]
    assert "steps" in result["accepted_top_level_keys"]
    assert "debug.output_checkpoint" in result["accepted_operation_names"]
    assert "debug.output_assert_since" in result["accepted_operation_names"]
    assert result["operation_aliases"]["debug.output_checkpoint"] == "output_checkpoint"
    assert result["operation_required_fields"]["debug.output_assert_since"] == ["checkpoint"]


@pytest.mark.asyncio
async def test_op_style_debug_output_checkpoint_preserves_completed_step_name() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [{"id": "before", "op": "debug.output_checkpoint", "name": "start"}],
        }
    )

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["phase"] == "step"
    assert result["completed_steps"][0]["name"] == "output_checkpoint"
    assert result["completed_steps"][0]["result"]["checkpoint"] == "start"


@pytest.mark.asyncio
async def test_legacy_name_args_output_checkpoint_remains_valid() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run(
        {
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        }
    )

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["phase"] == "action"
    assert result["completed_steps"][0]["name"] == "output_checkpoint"
    assert result["completed_steps"][0]["result"]["checkpoint"] == "start"


def test_cleanup_restore_entries_require_explicit_baseline_source() -> None:
    assert validate_plan(
        {
            "cleanup": {"restore_files": [{"path": "fixture.txt"}]},
        }
    ) == ["cleanup.restore_files[0] requires exactly one of baseline_text or baseline_file"]

    assert validate_plan(
        {
            "cleanup": {
                "restore_files": [
                    {
                        "path": "fixture.txt",
                        "baseline_text": "inline",
                        "baseline_file": "baseline.txt",
                    }
                ]
            },
        }
    ) == ["cleanup.restore_files[0] requires exactly one of baseline_text or baseline_file"]


def test_fixture_restore_step_uses_same_restore_validation() -> None:
    assert validate_plan(
        {
            "steps": [{"op": "fixture.restore", "path": "fixture.txt"}],
        }
    ) == ["steps[0] requires exactly one of baseline_text or baseline_file"]


def test_validate_plan_rejects_unexpected_top_level_keys() -> None:
    errors = validate_plan(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "stepps": [],
        }
    )

    assert errors == [
        "unexpected top-level key: stepps; expected one of: "
        "schema, name, description, preflight, launch, freshness, steps, actions, "
        "assertions, evidence, cleanup, teardown, budgets, "
        "stop_on_first_failed_assertion"
    ]


def test_validate_v2_plan_accepts_no_global_input_policy() -> None:
    plan = {
        "schema": "netcoredbg.runtime_smoke.v2",
        "input_policy": {"no_global_input": True},
        "cases": [
            {
                "id": "isolated_noop",
                "transitions": [{"action": {"kind": "noop"}, "probes": []}],
            }
        ],
    }

    assert validate_plan(plan) == []


def test_validate_v2_plan_accepts_run_confidence_policy() -> None:
    plan = {
        "schema": "netcoredbg.runtime_smoke.v2",
        "input_policy": {"no_global_input": True},
        "run_confidence": {"no_operator": True},
        "cases": [
            {
                "id": "isolated_noop",
                "transitions": [{"action": {"kind": "noop"}, "probes": []}],
            }
        ],
    }

    assert validate_plan(plan) == []


def test_validate_v2_plan_rejects_malformed_run_confidence_policy() -> None:
    errors = validate_plan(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "run_confidence": {"no_operator": "yes"},
            "cases": [
                {
                    "id": "isolated_noop",
                    "transitions": [{"action": {"kind": "noop"}, "probes": []}],
                }
            ],
        }
    )

    assert errors == ["run_confidence.no_operator must be a boolean"]


def test_validate_v2_plan_rejects_unknown_run_confidence_key() -> None:
    errors = validate_plan(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "run_confidence": {"no_operator": True, "foreground_free": True},
            "cases": [
                {
                    "id": "isolated_noop",
                    "transitions": [{"action": {"kind": "noop"}, "probes": []}],
                }
            ],
        }
    )

    assert errors == [
        "run_confidence.foreground_free is not accepted; expected one of: no_operator"
    ]


def test_validate_v2_plan_rejects_malformed_no_global_input_policy() -> None:
    errors = validate_plan(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "input_policy": {"no_global_input": "yes"},
            "cases": [
                {
                    "id": "isolated_noop",
                    "transitions": [{"action": {"kind": "noop"}, "probes": []}],
                }
            ],
        }
    )

    assert errors == ["input_policy.no_global_input must be a boolean"]


def test_validate_plan_rejects_nested_operation_argument_type_errors() -> None:
    errors = validate_plan(
        {
            "steps": [
                {"op": "ui.invoke", "selector": []},
                {
                    "op": "ui.grid.assert_rows",
                    "selector": {"automation_id": "dataGrid"},
                    "rows": {"index": 0},
                },
                {
                    "op": "ui.list.toggle_item_child",
                    "selector": {"automation_id": "CharactersListBox"},
                    "item": "ALICE",
                    "child": [],
                    "target_state": 1,
                },
            ],
        }
    )

    assert errors == [
        "steps[0].selector must be an object for op ui.invoke",
        "steps[1].rows must be a list for op ui.grid.assert_rows",
        "steps[2].item must be an object for op ui.list.toggle_item_child",
        "steps[2].child must be an object for op ui.list.toggle_item_child",
        "steps[2].target_state must be a string for op ui.list.toggle_item_child",
    ]


def test_runtime_smoke_schema_accepts_ui_text_read_operation() -> None:
    assert (
        validate_plan(
            {
                "steps": [
                    {
                        "op": "ui.text.read",
                        "selector": {"automation_id": "CueTextBox"},
                    }
                ]
            }
        )
        == []
    )


def test_runtime_smoke_schema_accepts_ui_text_get_state_operation() -> None:
    assert (
        validate_plan(
            {
                "steps": [
                    {
                        "op": "ui.text.get_state",
                        "selector": {"automation_id": "CueTextBox"},
                    }
                ]
            }
        )
        == []
    )


@pytest.mark.asyncio
async def test_legacy_runtime_smoke_grid_ensure_visible_reaches_adapter() -> None:
    session = SchemaSmokeSession()
    plan = {
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [
            {
                "op": "ui.grid.ensure_visible",
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"identity": "Cue 042"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True},
                "columns": ["PhraseId"],
                "max_scrolls": 11,
                "scroll_settle_ms": 30,
            }
        ],
    }

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={"ui.grid.ensure_visible": session.grid_ensure_visible},
    ).run(plan)

    assert validate_plan(plan) == []
    assert result["status"] == "PASS"
    assert "validation_errors" not in result
    assert session.adapter_calls == [
        (
            "ui.grid.ensure_visible",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"identity": "Cue 042"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True},
                "columns": ["PhraseId"],
                "max_scrolls": 11,
                "scroll_settle_ms": 30,
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_runtime_smoke_grid_assert_range_reaches_adapter() -> None:
    session = SchemaSmokeSession()
    plan = {
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [
            {
                "op": "ui.grid.assert_range",
                "selector": {"automation_id": "CueDataGrid"},
                "start_index": 2,
                "end_index": 5,
            }
        ],
    }

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={"ui.grid.assert_range": session.grid_assert_range},
    ).run(plan)

    assert validate_plan(plan) == []
    assert result["status"] == "PASS"
    assert "validation_errors" not in result
    assert session.adapter_calls == [
        (
            "ui.grid.assert_range",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "start_index": 2,
                "end_index": 5,
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_runtime_smoke_grid_state_actions_reach_adapters() -> None:
    session = SchemaSmokeSession()
    plan = {
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [
            {
                "op": "ui.grid.get_state",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True},
                "columns": ["PhraseId"],
            },
            {
                "op": "ui.grid.select_row",
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"identity": "Cue 042"},
                "identity": {"column": "PhraseId"},
            },
            {
                "op": "ui.grid.click_row",
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 3},
                "column": "Phrase",
            },
            {
                "op": "ui.grid.right_click_row",
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 4},
                "column": "Phrase",
            },
            {
                "op": "ui.grid.double_click_row",
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 5},
                "column": "Phrase",
            },
        ],
    }

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.grid.get_state": session.grid_get_state,
            "ui.grid.select_row": session.grid_select_row,
            "ui.grid.click_row": session.grid_click_row,
            "ui.grid.right_click_row": session.grid_right_click_row,
            "ui.grid.double_click_row": session.grid_double_click_row,
        },
    ).run(plan)

    assert validate_plan(plan) == []
    assert result["status"] == "PASS"
    assert "validation_errors" not in result
    assert session.adapter_calls == [
        (
            "ui.grid.get_state",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True},
                "columns": ["PhraseId"],
            },
        ),
        (
            "ui.grid.select_row",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"identity": "Cue 042"},
                "identity": {"column": "PhraseId"},
            },
        ),
        (
            "ui.grid.click_row",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 3},
                "column": "Phrase",
            },
        ),
        (
            "ui.grid.right_click_row",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 4},
                "column": "Phrase",
            },
        ),
        (
            "ui.grid.double_click_row",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row": {"index": 5},
                "column": "Phrase",
            },
        ),
    ]


def test_legacy_runtime_smoke_grid_state_actions_validate_arguments() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.grid.get_state",
                    "selector": {"automation_id": "CueDataGrid"},
                    "identity": [],
                    "rows": [],
                    "columns": ["PhraseId", 7],
                },
                {
                    "op": "ui.grid.select_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": "Cue 042",
                },
                {
                    "op": "ui.grid.click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 3},
                    "column": 9,
                },
                {
                    "op": "ui.grid.right_click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 4},
                    "identity": [],
                    "column": 10,
                },
                {
                    "op": "ui.grid.double_click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 5},
                    "identity": [],
                    "column": 11,
                },
            ],
        }
    ) == [
        "steps[0].rows must be an object for op ui.grid.get_state",
        "steps[0].columns must be a list of strings for op ui.grid.get_state",
        "steps[0].identity must be an object for op ui.grid.get_state",
        "steps[1].row must be an object for op ui.grid.select_row",
        "steps[2].column must be a string for op ui.grid.click_row",
        "steps[3].identity must be an object for op ui.grid.right_click_row",
        "steps[3].column must be a string for op ui.grid.right_click_row",
        "steps[4].identity must be an object for op ui.grid.double_click_row",
        "steps[4].column must be a string for op ui.grid.double_click_row",
    ]


def test_legacy_runtime_smoke_grid_row_actions_validate_ensure_visible_arguments() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.grid.select_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"identity": "Cue 042"},
                    "ensure_visible": "yes",
                    "max_scrolls": "far",
                    "scroll_settle_ms": False,
                },
                {
                    "op": "ui.grid.click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 3},
                    "ensure_visible": 1,
                    "max_scrolls": [],
                    "scroll_settle_ms": "slow",
                },
                {
                    "op": "ui.grid.right_click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 4},
                    "ensure_visible": 1,
                    "max_scrolls": [],
                    "scroll_settle_ms": "slow",
                },
                {
                    "op": "ui.grid.double_click_row",
                    "selector": {"automation_id": "CueDataGrid"},
                    "row": {"index": 5},
                    "ensure_visible": 1,
                    "max_scrolls": [],
                    "scroll_settle_ms": "slow",
                },
            ],
        }
    ) == [
        "steps[0].ensure_visible must be a boolean for op ui.grid.select_row",
        "steps[0].max_scrolls must be an integer for op ui.grid.select_row",
        "steps[0].scroll_settle_ms must be an integer for op ui.grid.select_row",
        "steps[1].ensure_visible must be a boolean for op ui.grid.click_row",
        "steps[1].max_scrolls must be an integer for op ui.grid.click_row",
        "steps[1].scroll_settle_ms must be an integer for op ui.grid.click_row",
        "steps[2].ensure_visible must be a boolean for op ui.grid.right_click_row",
        "steps[2].max_scrolls must be an integer for op ui.grid.right_click_row",
        "steps[2].scroll_settle_ms must be an integer for op ui.grid.right_click_row",
        "steps[3].ensure_visible must be a boolean for op ui.grid.double_click_row",
        "steps[3].max_scrolls must be an integer for op ui.grid.double_click_row",
        "steps[3].scroll_settle_ms must be an integer for op ui.grid.double_click_row",
    ]


def test_legacy_runtime_smoke_grid_ensure_visible_validates_arguments() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.grid.ensure_visible",
                    "selector": [],
                    "row": "Cue 042",
                    "identity": [],
                    "rows": [],
                    "columns": ["PhraseId", 7],
                    "max_scrolls": "far",
                    "scroll_settle_ms": False,
                },
            ],
        }
    ) == [
        "steps[0].selector must be an object for op ui.grid.ensure_visible",
        "steps[0].rows must be an object for op ui.grid.ensure_visible",
        "steps[0].columns must be a list of strings for op ui.grid.ensure_visible",
        "steps[0].identity must be an object for op ui.grid.ensure_visible",
        "steps[0].row must be an object for op ui.grid.ensure_visible",
        "steps[0].max_scrolls must be an integer for op ui.grid.ensure_visible",
        "steps[0].scroll_settle_ms must be an integer for op ui.grid.ensure_visible",
    ]


def test_runtime_smoke_schema_accepts_ui_text_assert_selection_operation() -> None:
    assert (
        validate_plan(
            {
                "steps": [
                    {
                        "op": "ui.text.assert_selection",
                        "selector": {"automation_id": "CueTextBox"},
                        "selection_start": 3,
                        "selection_end": 10,
                    }
                ]
            }
        )
        == []
    )


def test_runtime_smoke_schema_accepts_ui_text_set_text_operation() -> None:
    assert (
        validate_plan(
            {
                "steps": [
                    {
                        "op": "ui.text.set_text",
                        "selector": {"automation_id": "CueTextBox"},
                        "text": "Replaced text",
                    }
                ]
            }
        )
        == []
    )


def test_runtime_smoke_schema_requires_ui_text_set_text_text() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.text.set_text",
                    "selector": {"automation_id": "CueTextBox"},
                }
            ]
        }
    ) == ["steps[0].text is required for op ui.text.set_text"]


def test_runtime_smoke_schema_rejects_non_string_ui_text_set_text_text() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.text.set_text",
                    "selector": {"automation_id": "CueTextBox"},
                    "text": 42,
                }
            ]
        }
    ) == ["steps[0].text must be a string for op ui.text.set_text"]


def test_runtime_smoke_schema_requires_ui_text_assert_selection_range() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.text.assert_selection",
                    "selector": {"automation_id": "CueTextBox"},
                }
            ]
        }
    ) == [
        (
            "steps[0].selection_start and selection_end are required "
            "for op ui.text.assert_selection"
        )
    ]


def test_runtime_smoke_schema_rejects_bool_ui_text_assert_selection_range() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.text.assert_selection",
                    "selector": {"automation_id": "CueTextBox"},
                    "selection_start": True,
                    "selection_end": False,
                }
            ]
        }
    ) == [
        "steps[0].selection_start must be an integer for op ui.text.assert_selection",
        "steps[0].selection_end must be an integer for op ui.text.assert_selection",
    ]


def test_runtime_smoke_schema_accepts_ui_get_property_operation() -> None:
    assert (
        validate_plan(
            {
                "steps": [
                    {
                        "op": "ui.get_property",
                        "selector": {"automation_id": "CueTextBox"},
                        "property": "Name",
                    },
                    {
                        "op": "ui.get_property",
                        "selector": {"automation_id": "CueTextBox"},
                        "property_name": "Value",
                    }
                ]
            }
        )
        == []
    )


def test_runtime_smoke_schema_requires_ui_get_property_property_argument() -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.get_property",
                    "selector": {"automation_id": "CueTextBox"},
                }
            ]
        }
    ) == [
        "steps[0].property or property_name is required for op ui.get_property"
    ]


@pytest.mark.parametrize("field_name", ["property", "property_name"])
def test_runtime_smoke_schema_rejects_non_string_ui_get_property_property(
    field_name: str,
) -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.get_property",
                    "selector": {"automation_id": "CueTextBox"},
                    field_name: 42,
                }
            ]
        }
    ) == [
        f"steps[0].{field_name} must be a string for op ui.get_property"
    ]


@pytest.mark.parametrize("field_name", ["property", "property_name"])
def test_runtime_smoke_schema_rejects_blank_ui_get_property_property(
    field_name: str,
) -> None:
    assert validate_plan(
        {
            "steps": [
                {
                    "op": "ui.get_property",
                    "selector": {"automation_id": "CueTextBox"},
                    field_name: "   ",
                }
            ]
        }
    ) == [
        f"steps[0].{field_name} must be a non-empty string for op ui.get_property"
    ]


@pytest.mark.parametrize("value", [None, True, False, 1.5, "1"])
def test_validate_plan_rejects_non_integral_max_actions(value: Any) -> None:
    assert validate_plan({"budgets": {"max_actions": value}}) == [
        "budgets.max_actions must be an integer"
    ]


@pytest.mark.parametrize("value", [0, -1])
def test_validate_plan_rejects_non_positive_max_actions(value: int) -> None:
    assert validate_plan({"budgets": {"max_actions": value}}) == [
        "budgets.max_actions must be at least 1"
    ]


@pytest.mark.parametrize("value", [None, True, False, "1", []])
def test_validate_plan_rejects_non_numeric_max_elapsed_seconds(value: Any) -> None:
    assert validate_plan({"budgets": {"max_elapsed_seconds": value}}) == [
        "budgets.max_elapsed_seconds must be a number"
    ]


@pytest.mark.parametrize(
    "value",
    [0, -1, pytest.param(10**10000, id="huge-int"), float("inf"), float("nan")],
)
def test_validate_plan_rejects_non_positive_or_non_finite_max_elapsed_seconds(
    value: float,
) -> None:
    assert validate_plan({"budgets": {"max_elapsed_seconds": value}}) == [
        "budgets.max_elapsed_seconds must be positive"
    ]

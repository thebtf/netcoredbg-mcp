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

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        return {"status": "PASS", "reason": "launched"}


@pytest.mark.asyncio
async def test_invalid_plan_returns_self_describing_schema_help_before_launch() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run({
        "schema": "netcoredbg.runtime_smoke.v1",
        "launch": {"program": "unused.exe"},
        "evidence": "not-a-list",
        "steps": [{"id": "assert-output", "op": "debug.output_assert_since"}],
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert "evidence must be a list" in result["validation_errors"]
    assert (
        "steps[0].checkpoint is required for op debug.output_assert_since"
        in result["validation_errors"]
    )
    assert session.launch_calls == 0

    assert result["accepted_schema_values"] == ["netcoredbg.runtime_smoke.v1"]
    assert "steps" in result["accepted_top_level_keys"]
    assert "debug.output_checkpoint" in result["accepted_operation_names"]
    assert "debug.output_assert_since" in result["accepted_operation_names"]
    assert result["operation_aliases"]["debug.output_checkpoint"] == "output_checkpoint"
    assert result["operation_required_fields"]["debug.output_assert_since"] == [
        "checkpoint"
    ]


@pytest.mark.asyncio
async def test_op_style_debug_output_checkpoint_preserves_completed_step_name() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run({
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [{"id": "before", "op": "debug.output_checkpoint", "name": "start"}],
    })

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["phase"] == "step"
    assert result["completed_steps"][0]["name"] == "output_checkpoint"
    assert result["completed_steps"][0]["result"]["checkpoint"] == "start"


@pytest.mark.asyncio
async def test_legacy_name_args_output_checkpoint_remains_valid() -> None:
    session = SchemaSmokeSession()
    result = await RuntimeSmokeRunner(session).run({
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
    })

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["phase"] == "action"
    assert result["completed_steps"][0]["name"] == "output_checkpoint"
    assert result["completed_steps"][0]["result"]["checkpoint"] == "start"


def test_cleanup_restore_entries_require_explicit_baseline_source() -> None:
    assert validate_plan({
        "cleanup": {"restore_files": [{"path": "fixture.txt"}]},
    }) == [
        "cleanup.restore_files[0] requires exactly one of baseline_text or baseline_file"
    ]

    assert validate_plan({
        "cleanup": {
            "restore_files": [
                {
                    "path": "fixture.txt",
                    "baseline_text": "inline",
                    "baseline_file": "baseline.txt",
                }
            ]
        },
    }) == [
        "cleanup.restore_files[0] requires exactly one of baseline_text or baseline_file"
    ]


def test_fixture_restore_step_uses_same_restore_validation() -> None:
    assert validate_plan({
        "steps": [{"op": "fixture.restore", "path": "fixture.txt"}],
    }) == [
        "steps[0] requires exactly one of baseline_text or baseline_file"
    ]


def test_validate_plan_rejects_unexpected_top_level_keys() -> None:
    errors = validate_plan({
        "schema": "netcoredbg.runtime_smoke.v1",
        "stepps": [],
    })

    assert errors == [
        "unexpected top-level key: stepps; expected one of: "
        "schema, name, description, preflight, launch, freshness, steps, actions, "
        "assertions, evidence, cleanup, teardown, budgets, "
        "stop_on_first_failed_assertion"
    ]


def test_validate_plan_rejects_nested_operation_argument_type_errors() -> None:
    errors = validate_plan({
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
    })

    assert errors == [
        "steps[0].selector must be an object for op ui.invoke",
        "steps[1].rows must be a list for op ui.grid.assert_rows",
        "steps[2].item must be an object for op ui.list.toggle_item_child",
        "steps[2].child must be an object for op ui.list.toggle_item_child",
        "steps[2].target_state must be a string for op ui.list.toggle_item_child",
    ]

"""Bounded runtime smoke runner contract tests."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
from netcoredbg_mcp.session.state import DebugState, OutputEntry
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class FakeRuntimeSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            process_id=1234,
            process_name="Smoke",
            modules=[],
            loaded_sources={},
        )
        self.hygiene_calls = 0
        self.instrumentation_clears: list[str] = []
        self.key_sequence_calls = 0
        self.grid_calls = 0
        self.workflow_calls: list[str] = []
        self.failing_action_calls = 0
        self.stop_calls = 0
        self.allowed_root: Path | None = None
        self.validation_failure: str | None = None
        self.validated_paths: list[tuple[str, bool]] = []

    async def hygiene_preflight(self, **_: Any) -> dict[str, Any]:
        self.hygiene_calls += 1
        return {"status": "PASS", "reason": "hygiene passed"}

    async def clear_instrumentation_group(self, name: str) -> dict[str, Any]:
        self.instrumentation_clears.append(name)
        if name == "leak":
            return {
                "status": "FAIL",
                "reason": "instrumentation group cleanup leaked state",
                "leaks": [{"kind": "breakpoint", "line": 42}],
            }
        self.runtime_smoke.instrumentation_groups.pop(name, None)
        return {"status": "PASS", "reason": "instrumentation group cleared"}

    async def scoped_key_sequence(self, **_: Any) -> dict[str, Any]:
        self.key_sequence_calls += 1
        return {"status": "BLOCKED", "reason": "ui backend unsupported"}

    async def grid_action(self, **_: Any) -> dict[str, Any]:
        self.grid_calls += 1
        return {"status": "BLOCKED", "reason": "grid backend unsupported"}

    async def grid_snapshot(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.grid.snapshot")
        return {"status": "PASS", "visible_rows": [{"index": 0, "cells": {"Phrase": "one"}}]}

    async def list_invoke_item(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.list.invoke_item")
        return {"status": "PASS", "invoked": True}

    async def list_toggle_item_child(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.list.toggle_item_child")
        return {"status": "PASS", "toggled": True, "new_state": "On"}

    async def focus_assert(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.focus.assert")
        return {"status": "PASS", "focused": True}

    async def text_assert(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.text.assert")
        return {"status": "PASS", "matched": True}

    async def invoke(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.invoke")
        return {"status": "PASS", "invoked": True}

    async def failing_action(self, **_: Any) -> dict[str, Any]:
        self.failing_action_calls += 1
        raise RuntimeError("adapter exploded")

    async def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        self.state.state = DebugState.IDLE
        return {"success": True, "state": "idle"}

    def validate_path(self, path: str, must_exist: bool = False) -> str:
        self.validated_paths.append((path, must_exist))
        if self.validation_failure:
            raise ValueError(self.validation_failure)
        candidate = Path(path).resolve()
        if self.allowed_root is not None:
            root = self.allowed_root.resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError("Path outside project root") from exc
        if must_exist and not candidate.exists():
            raise ValueError(f"Path does not exist: {path}")
        return str(candidate)

    async def append_output(self, text: str, category: str = "stdout") -> dict[str, Any]:
        self.state.output_sequence += 1
        self.state.output_buffer.append(
            OutputEntry(
                text,
                category=category,
                sequence=self.state.output_sequence,
            )
        )
        return {
            "status": "PASS",
            "reason": "output appended",
            "text": text,
            "text_length": len(text),
        }


def _runner(session: FakeRuntimeSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "debug_hygiene_preflight": session.hygiene_preflight,
            "instrumentation_group_clear": session.clear_instrumentation_group,
            "ui_key_sequence": session.scoped_key_sequence,
            "ui_grid": session.grid_action,
            "append_output": session.append_output,
            "failing_action": session.failing_action,
            "ui.grid.snapshot": session.grid_snapshot,
            "ui.list.invoke_item": session.list_invoke_item,
            "ui.list.toggle_item_child": session.list_toggle_item_child,
            "ui.focus.assert": session.focus_assert,
            "ui.text.assert": session.text_assert,
            "ui.invoke": session.invoke,
        },
    )


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    pass


@pytest.mark.asyncio
async def test_runner_passes_with_preflight_output_assertion_and_teardown() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}
    session.state.output_buffer.append(OutputEntry("boot\n"))

    result = await _runner(session).run({
        "name": "happy",
        "budgets": {"max_actions": 5, "max_elapsed_seconds": 10},
        "preflight": {"name": "debug_hygiene_preflight"},
        "actions": [
            {"name": "output_checkpoint", "args": {"name": "start"}},
            {"name": "append_output", "args": {"text": "ready\n"}},
        ],
        "assertions": [
            {
                "name": "output_assert_since",
                "args": {"checkpoint": "start", "required": ["ready"]},
            },
        ],
        "teardown": {"instrumentation_groups": ["flow"]},
    })

    assert result["status"] == "PASS"
    assert result["action_count"] == 4
    assert result["failed_assertions"] == []
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["attempted"] == [
        "instrumentation_group_clear:flow",
        "runtime_smoke_reset",
    ]
    assert session.instrumentation_clears == ["flow"]
    assert result["compact"]["status"] == "PASS"
    assert result["compact"]["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_stops_on_first_failed_assertion_and_still_tears_down() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}

    result = await _runner(session).run({
        "name": "failed-assertion",
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        "assertions": [
            {
                "name": "output_assert_since",
                "args": {"checkpoint": "start", "required": ["missing"]},
            },
            {"name": "append_output", "args": {"text": "must not run\n"}},
        ],
        "teardown": {"instrumentation_groups": ["flow"]},
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "assertion failed"
    assert result["action_count"] == 2
    assert len(result["failed_assertions"]) == 1
    assert result["cleanup"]["status"] == "PASS"
    assert session.instrumentation_clears == ["flow"]


@pytest.mark.asyncio
async def test_runner_action_budget_exhaustion_returns_impasse_with_completed_steps() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run({
        "name": "budget",
        "budgets": {"max_actions": 1, "max_elapsed_seconds": 10},
        "actions": [
            {"name": "output_checkpoint", "args": {"name": "start"}},
            {"name": "append_output", "args": {"text": "ready\n"}},
        ],
    })

    assert result["status"] == "IMPASSE"
    assert result["reason"] == "action budget exhausted"
    assert result["action_count"] == 1
    assert [step["name"] for step in result["completed_steps"]] == ["output_checkpoint"]
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_unsupported_backend_action_returns_blocked_and_teardown() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}

    result = await _runner(session).run({
        "name": "unsupported-ui",
        "actions": [
            {
                "name": "ui_key_sequence",
                "args": {
                    "selector": {"automation_id": "Grid"},
                    "modifiers": ["Shift"],
                    "keys": ["Down"],
                },
            }
        ],
        "teardown": {"instrumentation_groups": ["flow"]},
    })

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ui backend unsupported"
    assert session.key_sequence_calls == 1
    assert session.instrumentation_clears == ["flow"]
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_treats_freshness_warning_as_non_terminal_failure() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run({
        "name": "freshness-warn",
        "actions": [
            {
                "name": "verify_debug_freshness",
                "args": {"expected_workspace": "C:/repo"},
            },
        ],
    })

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["status"] == "PASS"
    assert result["completed_steps"][0]["result"]["status"] == "WARN"


@pytest.mark.asyncio
async def test_runner_cleanup_failure_changes_success_to_fail_with_residue_evidence() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["leak"] = {"breakpoints": [1]}

    result = await _runner(session).run({
        "name": "cleanup-leak",
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        "teardown": {"instrumentation_groups": ["leak"]},
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "teardown failed"
    assert result["cleanup"]["status"] == "FAIL"
    assert (
        result["cleanup"]["failures"][0]["reason"]
        == "instrumentation group cleanup leaked state"
    )
    residue_failure = next(
        item
        for item in result["cleanup"]["failures"]
        if item["operation"] == "runtime_smoke_residue"
    )
    assert residue_failure["remaining_runtime_smoke_state"]["instrumentation_groups"] == [
        "leak"
    ]
    assert result["cleanup"]["remaining_runtime_smoke_state"]["instrumentation_groups"] == []


@pytest.mark.asyncio
async def test_runner_invalid_plan_still_attempts_cleanup() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.register_cleanup(
        "release-modifiers",
        lambda: (_ for _ in ()).throw(RuntimeError("release failed")),
    )

    result = await _runner(session).run({"name": "invalid", "actions": "not-a-list"})

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == ["actions must be a list"]
    assert result["cleanup"]["status"] == "FAIL"
    assert result["cleanup"]["reset_failures"] == [
        {"name": "release-modifiers", "error": "release failed"}
    ]


@pytest.mark.asyncio
async def test_runner_reports_operation_exception_and_still_tears_down() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}

    result = await _runner(session).run({
        "name": "operation-error",
        "actions": [{"name": "failing_action"}],
        "teardown": {"instrumentation_groups": ["flow"]},
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "runtime smoke operation raised exception"
    assert result["completed_steps"][0]["result"]["exception"] == {
        "type": "RuntimeError",
        "message": "adapter exploded",
    }
    assert session.failing_action_calls == 1
    assert session.instrumentation_clears == ["flow"]
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_rejects_invalid_budget_values_without_raising() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run({
        "name": "bad-budgets",
        "budgets": {"max_actions": "many", "max_elapsed_seconds": []},
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == [
        "budgets.max_actions must be an integer",
        "budgets.max_elapsed_seconds must be a number",
    ]
    assert result["completed_steps"] == []
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_compact_export_truncates_large_step_evidence() -> None:
    session = FakeRuntimeSmokeSession()
    long_text = "x" * 600

    result = await _runner(session).run({
        "name": "large-evidence",
        "actions": [
            {"name": "append_output", "args": {"text": long_text}},
            {"name": "output_checkpoint", "args": {"name": "after_large"}},
        ],
    })

    step = result["completed_steps"][0]
    compact_step = result["compact"]["completed_steps"][0]
    assert step["result"]["text_length"] == 600
    assert "text" not in compact_step["result"]
    assert compact_step["result"]["omitted_fields"] == ["text"]


@pytest.mark.asyncio
async def test_runner_executes_op_style_ui_and_output_steps_in_one_plan() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run({
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [
            {"op": "ui.grid.snapshot", "selector": {"automation_id": "CueGrid"}},
            {
                "op": "ui.list.invoke_item",
                "selector": {"automation_id": "CharactersListBox"},
                "item": {"name": "ALICE"},
            },
            {
                "op": "ui.list.toggle_item_child",
                "selector": {"automation_id": "CharactersListBox"},
                "item": {"name": "ALICE"},
                "child": {"automation_id": "CharGender"},
            },
            {"op": "ui.focus.assert", "selector": {"automation_id": "CueGrid"}},
            {"op": "ui.text.assert", "selector": {"name": "female"}},
            {"op": "ui.invoke", "selector": {"automation_id": "menuItemUndo"}},
            {"op": "debug.output_checkpoint", "name": "before"},
            {
                "op": "debug.output_assert_since",
                "checkpoint": "before",
                "forbidden": ["boom"],
            },
        ],
    })

    assert result["status"] == "PASS"
    assert [step["name"] for step in result["completed_steps"]] == [
        "ui.grid.snapshot",
        "ui.list.invoke_item",
        "ui.list.toggle_item_child",
        "ui.focus.assert",
        "ui.text.assert",
        "ui.invoke",
        "output_checkpoint",
        "output_assert_since",
    ]
    assert session.workflow_calls == [
        "ui.grid.snapshot",
        "ui.list.invoke_item",
        "ui.list.toggle_item_child",
        "ui.focus.assert",
        "ui.text.assert",
        "ui.invoke",
    ]


@pytest.mark.asyncio
async def test_runner_accepted_ui_op_without_backend_returns_blocked_not_schema_error() -> None:
    session = FakeRuntimeSmokeSession()

    result = await RuntimeSmokeRunner(session).run({
        "schema": "netcoredbg.runtime_smoke.v1",
        "steps": [{"op": "ui.grid.snapshot", "selector": {"automation_id": "CueGrid"}}],
    })

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "unsupported runtime smoke operation"
    assert "validation_errors" not in result
    assert result["completed_steps"][0]["name"] == "ui.grid.snapshot"


@pytest.mark.asyncio
async def test_ui_operation_adapters_reject_non_object_selector() -> None:
    session = FakeRuntimeSmokeSession()

    async def backend_provider() -> object:
        class FakeBackend:
            async def invoke_element(self, **_: Any) -> dict[str, Any]:
                return {"status": "PASS"}

        return FakeBackend()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters=ui_operation_adapters(backend_provider),
    ).run({
        "actions": [{"name": "ui.invoke", "args": {"selector": []}}],
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "runtime smoke operation raised exception"
    assert result["completed_steps"][0]["result"]["exception"] == {
        "type": "TypeError",
        "message": "selector must be an object when provided",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("pass", "PASS"),
        ("fail", "FAIL"),
        ("blocked", "BLOCKED"),
        ("impasse", "IMPASSE"),
    ],
)
async def test_runner_restores_files_on_every_terminal_status(
    tmp_path: Path,
    case: str,
    expected_status: str,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / f"{case}.txt"
    fixture.write_text("mutated", encoding="utf-8")
    actions: list[dict[str, Any]]
    assertions: list[dict[str, Any]] = []
    budgets: dict[str, Any] = {"max_actions": 10, "max_elapsed_seconds": 10}

    if case == "pass":
        actions = [{"name": "output_checkpoint", "args": {"name": "start"}}]
    elif case == "fail":
        actions = [{"name": "output_checkpoint", "args": {"name": "start"}}]
        assertions = [
            {
                "name": "output_assert_since",
                "args": {"checkpoint": "start", "required": ["missing"]},
            }
        ]
    elif case == "blocked":
        actions = [{"name": "ui_key_sequence", "args": {"keys": ["Down"]}}]
    else:
        actions = [
            {"name": "output_checkpoint", "args": {"name": "start"}},
            {"name": "append_output", "args": {"text": "must not run\n"}},
        ]
        budgets = {"max_actions": 1, "max_elapsed_seconds": 10}

    result = await _runner(session).run({
        "name": f"restore-{case}",
        "budgets": budgets,
        "actions": actions,
        "assertions": assertions,
        "cleanup": {
            "restore_files": [
                {"path": str(fixture), "baseline_text": f"baseline-{case}"}
            ]
        },
    })

    assert result["status"] == expected_status
    assert fixture.read_text(encoding="utf-8") == f"baseline-{case}"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["restored_files"] == [
        {
            "status": "PASS",
            "path": str(fixture.resolve()),
            "source": "baseline_text",
            "char_count": len(f"baseline-{case}"),
            "byte_count": len(f"baseline-{case}".encode()),
        }
    ]
    assert any(item.startswith("restore_file:") for item in result["cleanup"]["attempted"])


@pytest.mark.asyncio
async def test_runner_restores_from_validated_baseline_file(tmp_path: Path) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    baseline = tmp_path / "baseline.txt"
    fixture.write_text("mutated", encoding="utf-8")
    baseline.write_text("restored from file", encoding="utf-8")

    result = await _runner(session).run({
        "name": "restore-baseline-file",
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        "cleanup": {
            "restore_files": [
                {"path": str(fixture), "baseline_file": str(baseline)}
            ]
        },
    })

    assert result["status"] == "PASS"
    assert fixture.read_text(encoding="utf-8") == "restored from file"
    assert result["cleanup"]["restored_files"][0]["source"] == "baseline_file"
    assert result["cleanup"]["restored_files"][0]["baseline_file"] == str(baseline.resolve())
    assert "restored from file" not in str(result["compact"]["cleanup"])


@pytest.mark.asyncio
async def test_runner_cleanup_failure_changes_success_to_fail_for_restore_error(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    directory_target = tmp_path / "fixture-dir"
    directory_target.mkdir()

    result = await _runner(session).run({
        "name": "restore-fails",
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        "cleanup": {
            "restore_files": [
                {"path": str(directory_target), "baseline_text": "baseline"}
            ]
        },
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "teardown failed"
    assert result["cleanup"]["status"] == "FAIL"
    assert result["cleanup"]["failures"][0]["operation"] == "fixture.restore"
    assert result["cleanup"]["failures"][0]["path"] == str(directory_target.resolve())


@pytest.mark.asyncio
async def test_runner_records_graceful_debug_stop_when_requested() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run({
        "name": "stop-debug",
        "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        "cleanup": {"stop_debug": "graceful"},
    })

    assert result["status"] == "PASS"
    assert session.stop_calls == 1
    assert "stop_debug:graceful" in result["cleanup"]["attempted"]
    assert result["cleanup"]["debug_stop"] == {
        "status": "PASS",
        "mode": "graceful",
        "result": {"success": True, "state": "idle"},
    }


@pytest.mark.asyncio
async def test_runner_rejects_restore_path_outside_project_before_steps(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    session.allowed_root = allowed
    outside = tmp_path / "outside.txt"
    outside.write_text("mutated", encoding="utf-8")

    result = await _runner(session).run({
        "name": "unsafe-restore",
        "actions": [{"name": "append_output", "args": {"text": "must not run\n"}}],
        "cleanup": {
            "restore_files": [
                {"path": str(outside), "baseline_text": "baseline"}
            ]
        },
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["action_count"] == 0
    assert result["completed_steps"] == []
    assert outside.read_text(encoding="utf-8") == "mutated"
    assert "restore_file:" not in result["cleanup"]["attempted"]
    assert any("cleanup.restore_files[0].path" in error for error in result["validation_errors"])


@pytest.mark.asyncio
async def test_runner_rejects_restore_without_explicit_baseline_before_steps(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("mutated", encoding="utf-8")

    result = await _runner(session).run({
        "name": "missing-baseline",
        "actions": [{"name": "append_output", "args": {"text": "must not run\n"}}],
        "cleanup": {"restore_files": [{"path": str(fixture)}]},
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["action_count"] == 0
    assert fixture.read_text(encoding="utf-8") == "mutated"
    assert result["validation_errors"] == [
        "cleanup.restore_files[0] requires exactly one of baseline_text or baseline_file"
    ]


@pytest.mark.asyncio
async def test_runner_skips_plan_owned_cleanup_when_restore_schema_is_invalid(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("mutated", encoding="utf-8")

    result = await _runner(session).run({
        "name": "invalid-cleanup",
        "cleanup": {
            "restore_files": [{"path": str(fixture)}],
            "stop_debug": "graceful",
            "debug_hygiene": True,
        },
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert session.stop_calls == 0
    assert session.hygiene_calls == 0
    assert result["cleanup"]["attempted"] == ["runtime_smoke_reset"]


@pytest.mark.asyncio
async def test_runtime_smoke_tools_register_freshness_and_runner(capturing_mcp) -> None:
    mcp = capturing_mcp
    session = FakeRuntimeSmokeSession()
    register_runtime_smoke_tools(
        mcp=mcp,
        session=session,
        check_session_access=lambda ctx: None,
        resolve_project_root=_noop_resolve_project_root,
    )

    assert "verify_debug_freshness" in mcp.tools
    assert "run_runtime_smoke" in mcp.tools

    freshness = await mcp.tools["verify_debug_freshness"](
        ctx=None,
        expected_process_id=1234,
        expected_process_name="Smoke",
    )
    invalid = await mcp.tools["run_runtime_smoke"](
        ctx=None,
        plan={"name": "invalid", "actions": "not-a-list"},
    )
    non_object = await mcp.tools["run_runtime_smoke"](
        ctx=None,
        plan=["not-a-dict"],
    )
    failed = await mcp.tools["run_runtime_smoke"](
        ctx=None,
        plan={
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "assertions": [
                {
                    "name": "output_assert_since",
                    "args": {"checkpoint": "start", "required": ["missing"]},
                },
            ],
        },
    )

    assert freshness["data"]["status"] == "PASS"
    assert invalid["data"]["status"] == "FAIL"
    assert invalid["data"]["reason"] == "invalid plan schema"
    assert "accepted_schema_values" in invalid["data"]
    assert "accepted_top_level_keys" in invalid["data"]
    assert "accepted_operation_names" in invalid["data"]
    assert "operation_required_fields" in invalid["data"]
    assert invalid["data"]["cleanup"]["status"] == "PASS"
    assert non_object["data"]["status"] == "FAIL"
    assert non_object["data"]["reason"] == "invalid plan schema"
    assert non_object["data"]["validation_errors"] == ["plan must be an object"]
    assert non_object["data"]["accepted_schema_values"] == [
        "netcoredbg.runtime_smoke.v1"
    ]
    assert "debug.output_checkpoint" in non_object["data"]["accepted_operation_names"]
    assert non_object["data"]["completed_steps"] == []
    assert failed["data"]["status"] == "FAIL"
    assert failed["data"]["cleanup"]["status"] == "PASS"

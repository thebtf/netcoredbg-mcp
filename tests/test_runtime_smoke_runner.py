"""Bounded runtime smoke runner contract tests."""

from __future__ import annotations

import asyncio
import ctypes
import os
import stat
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from netcoredbg_mcp.session import runtime_smoke_operations as smoke_ops
from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
from netcoredbg_mcp.session.state import DebugState, OutputEntry
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


def _set_windows_file_attributes(path: Path, attributes: int) -> None:
    if os.name != "nt":
        return
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    kernel32.SetFileAttributesW.restype = wintypes.BOOL
    if not kernel32.SetFileAttributesW(str(path), attributes):
        error = ctypes.GetLastError()
        raise OSError(error, f"SetFileAttributesW failed for {path}")


async def _no_ui_backend() -> None:
    return None


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
        self.launch_calls = 0
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

    async def grid_get_state(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.grid.get_state")
        return {"status": "PASS", "visible_rows": [], "selected_rows": []}

    async def grid_select_row(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.grid.select_row")
        return {"status": "PASS", "selected_row": {"index": 0}}

    async def grid_click_row(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.grid.click_row")
        return {"status": "PASS", "clicked": True, "row": {"index": 0}}

    async def ui_ensure_connected(self, **_: Any) -> dict[str, Any]:
        self.workflow_calls.append("ui.ensure_connected")
        return {"status": "PASS", "reason": "ui backend connected"}

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

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        self.state.state = DebugState.RUNNING
        return {"success": True}

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
            "ui.ensure_connected": session.ui_ensure_connected,
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

    result = await _runner(session).run(
        {
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
        }
    )

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

    result = await _runner(session).run(
        {
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
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "assertion failed"
    assert result["action_count"] == 2
    assert len(result["failed_assertions"]) == 1
    assert result["cleanup"]["status"] == "PASS"
    assert session.instrumentation_clears == ["flow"]


@pytest.mark.asyncio
async def test_runner_action_budget_exhaustion_returns_impasse_with_completed_steps() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
            "name": "budget",
            "budgets": {"max_actions": 1, "max_elapsed_seconds": 10},
            "actions": [
                {"name": "output_checkpoint", "args": {"name": "start"}},
                {"name": "append_output", "args": {"text": "ready\n"}},
            ],
        }
    )

    assert result["status"] == "IMPASSE"
    assert result["reason"] == "action budget exhausted"
    assert result["action_count"] == 1
    assert [step["name"] for step in result["completed_steps"]] == ["output_checkpoint"]
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_unsupported_backend_action_returns_blocked_and_teardown() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}

    result = await _runner(session).run(
        {
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
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ui backend unsupported"
    assert session.key_sequence_calls == 1
    assert session.instrumentation_clears == ["flow"]
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_runner_treats_freshness_warning_as_non_terminal_failure() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
            "name": "freshness-warn",
            "actions": [
                {
                    "name": "verify_debug_freshness",
                    "args": {"expected_workspace": "C:/repo"},
                },
            ],
        }
    )

    assert result["status"] == "PASS"
    assert result["completed_steps"][0]["status"] == "PASS"
    assert result["completed_steps"][0]["result"]["status"] == "WARN"


@pytest.mark.asyncio
async def test_runner_cleanup_failure_changes_success_to_fail_with_residue_evidence() -> None:
    session = FakeRuntimeSmokeSession()
    session.runtime_smoke.instrumentation_groups["leak"] = {"breakpoints": [1]}

    result = await _runner(session).run(
        {
            "name": "cleanup-leak",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "teardown": {"instrumentation_groups": ["leak"]},
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "teardown failed"
    assert result["cleanup"]["status"] == "FAIL"
    assert (
        result["cleanup"]["failures"][0]["reason"] == "instrumentation group cleanup leaked state"
    )
    residue_failure = next(
        item
        for item in result["cleanup"]["failures"]
        if item["operation"] == "runtime_smoke_residue"
    )
    assert residue_failure["remaining_runtime_smoke_state"]["instrumentation_groups"] == ["leak"]
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

    result = await _runner(session).run(
        {
            "name": "operation-error",
            "actions": [{"name": "failing_action"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        }
    )

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

    result = await _runner(session).run(
        {
            "name": "bad-budgets",
            "budgets": {"max_actions": "many", "max_elapsed_seconds": []},
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == [
        "budgets.max_actions must be an integer",
        "budgets.max_elapsed_seconds must be a number",
    ]
    assert result["completed_steps"] == []
    assert result["cleanup"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_v2_runner_rejects_invalid_budget_values_before_runner_parse() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "bad-v2-budgets",
            "budgets": {"max_actions": 1.5, "max_elapsed_seconds": "10"},
            "cases": [
                {
                    "id": "case-a",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "button"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == [
        "budgets.max_actions must be an integer",
        "budgets.max_elapsed_seconds must be a number",
    ]
    assert result["action_count"] == 0


@pytest.mark.asyncio
async def test_runner_compact_export_truncates_large_step_evidence() -> None:
    session = FakeRuntimeSmokeSession()
    long_text = "x" * 600

    result = await _runner(session).run(
        {
            "name": "large-evidence",
            "actions": [
                {"name": "append_output", "args": {"text": long_text}},
                {"name": "output_checkpoint", "args": {"name": "after_large"}},
            ],
        }
    )

    step = result["completed_steps"][0]
    compact_step = result["compact"]["completed_steps"][0]
    assert step["result"]["text_length"] == 600
    assert "text" not in compact_step["result"]
    assert compact_step["result"]["omitted_fields"] == ["text"]


@pytest.mark.asyncio
async def test_runner_executes_op_style_ui_and_output_steps_in_one_plan() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
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
        }
    )

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
async def test_runner_eagerly_connects_ui_after_launch_before_freshness() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "launch": {"program": "Smoke.dll"},
            "freshness": {"expected_process_name": "Smoke"},
            "steps": [
                {"op": "ui.grid.snapshot", "selector": {"automation_id": "CueGrid"}},
            ],
        }
    )

    assert result["status"] == "PASS"
    assert [step["name"] for step in result["completed_steps"]] == [
        "launch",
        "ui.ensure_connected",
        "verify_debug_freshness",
        "ui.grid.snapshot",
    ]
    assert session.workflow_calls == ["ui.ensure_connected", "ui.grid.snapshot"]


@pytest.mark.asyncio
async def test_runner_accepted_ui_op_without_backend_returns_blocked_not_schema_error() -> None:
    session = FakeRuntimeSmokeSession()

    result = await RuntimeSmokeRunner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [{"op": "ui.grid.snapshot", "selector": {"automation_id": "CueGrid"}}],
        }
    )

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
    ).run(
        {
            "actions": [{"name": "ui.invoke", "args": {"selector": []}}],
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "runtime smoke operation raised exception"
    assert result["completed_steps"][0]["result"]["exception"] == {
        "type": "TypeError",
        "message": "selector must be an object when provided",
    }


@pytest.mark.asyncio
async def test_ui_invoke_uses_fallback_key_sequence_when_primary_missing() -> None:
    session = FakeRuntimeSmokeSession()

    class FakeBackend:
        def __init__(self) -> None:
            self.key_sequence_calls: list[tuple[dict[str, Any], list[str], list[str]]] = []

        async def invoke_element(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("Element not found. Search: automationId='menuItemUndo'")

        async def scoped_key_sequence(
            self,
            selector: dict[str, Any],
            modifiers: list[str],
            keys: list[str],
        ) -> dict[str, Any]:
            self.key_sequence_calls.append((dict(selector), list(modifiers), list(keys)))
            return {
                "status": "PASS",
                "sent_count": len(keys),
                "final_held_modifiers": [],
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await RuntimeSmokeRunner(
        session,
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [
                {
                    "op": "ui.invoke",
                    "selector": {"automation_id": "menuItemUndo"},
                    "fallback_key_sequence": {
                        "automation_id": "CueDataGrid",
                        "modifiers": ["ctrl"],
                        "keys": ["z"],
                    },
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    step_result = result["completed_steps"][0]["result"]
    assert step_result["method"] == "fallback_key_sequence"
    assert step_result["invoked"] is True
    assert "menuItemUndo" in step_result["primary_error"]
    assert backend.key_sequence_calls == [({"automation_id": "CueDataGrid"}, ["ctrl"], ["z"])]


@pytest.mark.asyncio
async def test_ui_invoke_preserves_non_selector_backend_exception() -> None:
    session = FakeRuntimeSmokeSession()

    class FakeBackend:
        async def invoke_element(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("bridge transport unavailable")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await RuntimeSmokeRunner(
        session,
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [
                {
                    "op": "ui.invoke",
                    "selector": {"automation_id": "menuItemUndo"},
                }
            ],
        }
    )

    step_result = result["completed_steps"][0]["result"]
    assert result["status"] == "BLOCKED"
    assert step_result["reason"] == "bridge transport unavailable"
    assert step_result["requested"]["selector"] == {"automation_id": "menuItemUndo"}


@pytest.mark.asyncio
async def test_ui_operation_adapters_click_uses_invoke_element_with_bounded_evidence() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.invoke_calls: list[dict[str, Any]] = []
            self.click_at_calls: list[tuple[int, int]] = []

        async def invoke_element(self, **kwargs: Any) -> dict[str, Any]:
            self.invoke_calls.append(dict(kwargs))
            return {
                "status": "PASS",
                "invoked": True,
                "method": "InvokePattern",
                "automationId": "ApplyButton",
                "name": "Apply",
                "controlType": "Button",
                "full_tree": {"must": "not leak"},
            }

        async def click_at(self, x: int, y: int) -> None:
            self.click_at_calls.append((x, y))

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.click"](
        selector={"automation_id": "ApplyButton"},
    )

    assert result == {
        "status": "PASS",
        "clicked": True,
        "method": "InvokePattern",
        "selector": {"automation_id": "ApplyButton"},
        "automationId": "ApplyButton",
        "name": "Apply",
        "controlType": "Button",
        "result": {
            "status": "PASS",
            "invoked": True,
            "method": "InvokePattern",
            "automationId": "ApplyButton",
            "name": "Apply",
            "controlType": "Button",
        },
        "settled_ms": 500,
    }
    assert backend.invoke_calls == [
        {
            "automation_id": "ApplyButton",
            "name": None,
            "control_type": None,
            "root_id": None,
            "xpath": None,
        }
    ]
    assert backend.click_at_calls == []


@pytest.mark.asyncio
async def test_ui_operation_adapters_click_blocks_without_activation_evidence() -> None:
    class FakeBackend:
        async def invoke_element(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "status": "PASS",
                "invoked": False,
                "method": "InvokePattern",
                "automationId": "ApplyButton",
                "full_tree": {"must": "not leak"},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.click"](
        selector={"automation_id": "ApplyButton"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "click activation evidence failed"
    assert result["requested"] == {"clicked": False}
    assert result["accepted"] == {"clicked": True, "invoked": True}
    assert result["selector"] == {"automation_id": "ApplyButton"}
    assert "full_tree" not in repr(result)


@pytest.mark.asyncio
async def test_ui_operation_adapters_click_blocks_raw_backend_failure() -> None:
    class FakeBackend:
        async def invoke_element(self, **kwargs: Any) -> str:
            return "bridge transport unavailable"

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.click"](
        selector={"automation_id": "ApplyButton"},
    )

    assert result == {
        "status": "FAIL",
        "reason": "ui.click returned non-object result",
        "result": "bridge transport unavailable",
        "selector": {"automation_id": "ApplyButton"},
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_right_click_uses_bounded_target_rect() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.find_calls: list[dict[str, Any]] = []
            self.right_click_at_calls: list[tuple[int, int]] = []

        async def find_element(self, **kwargs: Any) -> dict[str, Any]:
            self.find_calls.append(dict(kwargs))
            return {
                "status": "PASS",
                "found": True,
                "automationId": "CueGrid",
                "controlType": "DataGrid",
                "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
                "full_tree": {"must": "not leak"},
            }

        async def right_click_at(self, x: int, y: int) -> None:
            self.right_click_at_calls.append((x, y))

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.right_click"](
        selector={"automation_id": "CueGrid"},
    )

    assert result == {
        "status": "PASS",
        "clicked": True,
        "right_clicked": True,
        "click_kind": "right",
        "method": "right_click_at",
        "selector": {"automation_id": "CueGrid"},
        "position": {"x": 60, "y": 50},
        "automationId": "CueGrid",
        "controlType": "DataGrid",
        "result": {
            "status": "PASS",
            "found": True,
            "automationId": "CueGrid",
            "controlType": "DataGrid",
            "rect": {"x": 10, "y": 20, "width": 100, "height": 60},
        },
        "settled_ms": 500,
    }
    assert backend.find_calls == [
        {
            "automation_id": "CueGrid",
            "name": None,
            "control_type": None,
            "root_id": None,
            "xpath": None,
        }
    ]
    assert backend.right_click_at_calls == [(60, 50)]
    assert "full_tree" not in repr(result)


@pytest.mark.asyncio
async def test_ui_operation_adapters_double_click_uses_bounded_target_rect() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.find_calls: list[dict[str, Any]] = []
            self.double_click_at_calls: list[tuple[int, int]] = []

        async def find_element(self, **kwargs: Any) -> dict[str, Any]:
            self.find_calls.append(dict(kwargs))
            return {
                "status": "PASS",
                "found": True,
                "automationId": "OpenRecentItem",
                "controlType": "ListItem",
                "rect": {"left": 20, "top": 30, "right": 80, "bottom": 90},
                "raw_tree": {"must": "not leak"},
            }

        async def double_click_at(self, x: int, y: int) -> None:
            self.double_click_at_calls.append((x, y))

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.double_click"](
        selector={"automation_id": "OpenRecentItem"},
    )

    assert result == {
        "status": "PASS",
        "clicked": True,
        "double_clicked": True,
        "click_kind": "double",
        "method": "double_click_at",
        "selector": {"automation_id": "OpenRecentItem"},
        "position": {"x": 50, "y": 60},
        "automationId": "OpenRecentItem",
        "controlType": "ListItem",
        "result": {
            "status": "PASS",
            "found": True,
            "automationId": "OpenRecentItem",
            "controlType": "ListItem",
            "rect": {"left": 20, "top": 30, "right": 80, "bottom": 90},
        },
        "settled_ms": 500,
    }
    assert backend.find_calls == [
        {
            "automation_id": "OpenRecentItem",
            "name": None,
            "control_type": None,
            "root_id": None,
            "xpath": None,
        }
    ]
    assert backend.double_click_at_calls == [(50, 60)]
    assert "raw_tree" not in repr(result)


@pytest.mark.asyncio
async def test_ui_operation_adapters_right_click_blocks_raw_backend_failure() -> None:
    class FakeBackend:
        async def find_element(self, **kwargs: Any) -> str:
            return "bridge transport unavailable"

        async def right_click_at(self, x: int, y: int) -> None:
            raise AssertionError("right_click_at should not run without target evidence")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.right_click"](
        selector={"automation_id": "CueGrid"},
    )

    assert result == {
        "status": "FAIL",
        "reason": "ui.right_click returned non-object result",
        "result": "bridge transport unavailable",
        "selector": {"automation_id": "CueGrid"},
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_click_verified_uses_production_click_adapter() -> None:
    class FakeClient:
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            self.calls = calls

        async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(("client_call", method, dict(payload)))
            if method == "set_focus":
                return {
                    "status": "PASS",
                    "focused": True,
                    "focus_within": True,
                    "method": "UIA.Focus",
                }
            return {"status": "FAIL", "reason": f"unexpected method {method}"}

    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.client = FakeClient(self.calls)

        async def find_element(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("find_element", dict(kwargs)))
            return {
                "status": "PASS",
                "found": True,
                "visible": True,
                "enabled": True,
                "automationId": "ApplyButton",
                "controlType": "Button",
                "IsSelected": True,
                "full_tree": {"must": "not leak"},
            }

        async def invoke_element(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("invoke_element", dict(kwargs)))
            return {
                "status": "PASS",
                "invoked": True,
                "method": "InvokePattern",
                "automationId": "ApplyButton",
                "name": "Apply",
                "controlType": "Button",
                "full_tree": {"must": "not leak"},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await RuntimeSmokeRunner(
        FakeRuntimeSmokeSession(),
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "verified production click",
            "cases": [
                {
                    "id": "click_apply_button",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.click_verified",
                                "selector": {"automation_id": "ApplyButton"},
                                "postcondition": {
                                    "op": "ui.get_property",
                                    "selector": {"automation_id": "ApplyButton"},
                                    "property": "IsSelected",
                                    "equals": True,
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    action = result["cases"][0]["actions"][0]
    assert action["route"] == "click_verified"
    assert action["target"]["verified"] is True
    assert action["click"]["clicked"] is True
    assert action["postcondition"]["verified"] is True
    assert action["postcondition"]["actual"] is True
    assert "full_tree" not in repr(action)
    selector_kwargs = {
        "automation_id": "ApplyButton",
        "name": None,
        "control_type": None,
        "root_id": None,
        "xpath": None,
    }
    assert backend.calls == [
        ("find_element", selector_kwargs),
        ("client_call", "set_focus", {"automationId": "ApplyButton"}),
        ("invoke_element", selector_kwargs),
        ("find_element", selector_kwargs),
    ]


@pytest.mark.asyncio
async def test_ui_operation_adapters_click_verified_blocks_false_activation() -> None:
    class FakeClient:
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            self.calls = calls

        async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(("client_call", method, dict(payload)))
            if method == "set_focus":
                return {
                    "status": "PASS",
                    "focused": True,
                    "focus_within": True,
                    "method": "UIA.Focus",
                }
            return {"status": "FAIL", "reason": f"unexpected method {method}"}

    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.client = FakeClient(self.calls)

        async def find_element(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("find_element", dict(kwargs)))
            return {
                "status": "PASS",
                "found": True,
                "visible": True,
                "enabled": True,
                "automationId": "ApplyButton",
                "controlType": "Button",
                "IsSelected": True,
            }

        async def invoke_element(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("invoke_element", dict(kwargs)))
            return {
                "status": "PASS",
                "invoked": False,
                "method": "InvokePattern",
                "automationId": "ApplyButton",
                "name": "Apply",
                "controlType": "Button",
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await RuntimeSmokeRunner(
        FakeRuntimeSmokeSession(),
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "verified production click",
            "cases": [
                {
                    "id": "click_apply_button",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.click_verified",
                                "selector": {"automation_id": "ApplyButton"},
                                "postcondition": {
                                    "op": "ui.get_property",
                                    "selector": {"automation_id": "ApplyButton"},
                                    "property": "IsSelected",
                                    "equals": True,
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "BLOCKED"
    action = result["cases"][0]["actions"][0]
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "click activation evidence failed"
    selector_kwargs = {
        "automation_id": "ApplyButton",
        "name": None,
        "control_type": None,
        "root_id": None,
        "xpath": None,
    }
    assert backend.calls == [
        ("find_element", selector_kwargs),
        ("client_call", "set_focus", {"automationId": "ApplyButton"}),
        ("invoke_element", selector_kwargs),
    ]


@pytest.mark.asyncio
async def test_ui_get_property_propagates_backend_failure_status() -> None:
    class FakeBackend:
        async def extract_text(self, **_: Any) -> dict[str, Any]:
            return {"status": "BLOCKED", "reason": "backend not connected"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.get_property"](
        selector={"automation_id": "statusText"},
        property_name="Text",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "backend not connected"


@pytest.mark.asyncio
async def test_ui_text_read_adapter_extracts_bounded_text_without_assertion_args() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.extract_text_calls: list[dict[str, Any]] = []

        async def extract_text(self, **kwargs: Any) -> dict[str, Any]:
            self.extract_text_calls.append(dict(kwargs))
            return {
                "status": "PASS",
                "text": "Fixture cue one",
                "source": "ValuePattern",
                "full_tree": {"must": "not leak"},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.text.read"](
        selector={"automation_id": "CueTextBox"},
    )

    assert result == {
        "status": "PASS",
        "text": "Fixture cue one",
        "source": "ValuePattern",
        "selector": {"automation_id": "CueTextBox"},
    }
    assert backend.extract_text_calls == [
        {
            "automation_id": "CueTextBox",
            "name": None,
            "control_type": None,
            "root_id": None,
            "xpath": None,
        }
    ]


@pytest.mark.asyncio
async def test_ui_text_get_state_adapter_reads_bounded_textbox_state() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.state_calls: list[dict[str, Any]] = []

        async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
            self.state_calls.append(dict(selector))
            return {
                "status": "PASS",
                "text": "Fixture cue one",
                "value": "Fixture cue one",
                "selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
                "caret_index": 10,
                "focus_within": True,
                "enabled": True,
                "visible": True,
                "source": "TextPattern",
                "full_tree": {"must": "not leak"},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.text.get_state"](
        selector={"automation_id": "CueTextBox"},
    )

    assert result == {
        "status": "PASS",
        "text": "Fixture cue one",
        "value": "Fixture cue one",
        "selection": {
            "start": 3,
            "end": 10,
            "length": 7,
            "selected_text": "ture cu",
        },
        "caret_index": 10,
        "focus_within": True,
        "enabled": True,
        "visible": True,
        "source": "TextPattern",
        "selector": {"automation_id": "CueTextBox"},
    }
    assert backend.state_calls == [{"automation_id": "CueTextBox"}]


@pytest.mark.asyncio
async def test_ui_text_assert_selection_adapter_reports_expected_and_actual_ranges() -> None:
    class FakeBackend:
        async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "PASS",
                "text": "Fixture cue one",
                "selection": {
                    "start": 3,
                    "end": 10,
                    "length": 7,
                    "selected_text": "ture cu",
                },
                "source": "TextPattern",
                "full_tree": {"must": "not leak"},
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.text.assert_selection"](
        selector={"automation_id": "CueTextBox"},
        selection_start=3,
        selection_end=10,
    )

    assert result == {
        "status": "PASS",
        "matched": True,
        "expected_selection": {"start": 3, "end": 10},
        "actual_selection": {
            "start": 3,
            "end": 10,
            "length": 7,
            "selected_text": "ture cu",
        },
        "selector": {"automation_id": "CueTextBox"},
    }


@pytest.mark.asyncio
async def test_ui_text_assert_selection_adapter_fails_with_observed_range() -> None:
    class FakeBackend:
        async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "PASS",
                "text": "Fixture cue one",
                "selection": {
                    "start": 0,
                    "end": 0,
                    "length": 0,
                    "selected_text": "",
                },
                "source": "TextPattern",
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.text.assert_selection"](
        selector={"automation_id": "CueTextBox"},
        selection_start=3,
        selection_end=10,
    )

    assert result == {
        "status": "FAIL",
        "matched": False,
        "reason": "selection mismatch",
        "expected_selection": {"start": 3, "end": 10},
        "actual_selection": {
            "start": 0,
            "end": 0,
            "length": 0,
            "selected_text": "",
        },
        "selector": {"automation_id": "CueTextBox"},
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_text_set_text_uses_safe_type_replace() -> None:
    class FakeClient:
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            self._calls = calls

        async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
            self._calls.append(("client_call", method, dict(payload)))
            if method == "set_focus":
                return {"status": "PASS", "focused": True}
            return {"status": "FAIL", "reason": f"unexpected method {method}"}

    class FakeBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.client = FakeClient(self.calls)

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                (
                    "find_element",
                    {
                        "automation_id": automation_id,
                        "name": name,
                        "control_type": control_type,
                        "root_id": root_id,
                        "xpath": xpath,
                    },
                )
            )
            return {"status": "PASS", "found": True}

        def send_keys(self, keys: str) -> dict[str, Any]:
            self.calls.append(("send_keys", keys))
            return {"status": "PASS", "keys": keys}

        async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(("textbox_state", dict(selector)))
            return {
                "status": "PASS",
                "text": "Fixture cue one",
                "selection": {"start": 0, "end": 15, "length": 15},
                "focus_within": True,
                "source": "TextPattern",
                "full_tree": {"must": "not leak"},
            }

        async def extract_text(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                (
                    "extract_text",
                    {
                        "automation_id": automation_id,
                        "name": name,
                        "control_type": control_type,
                        "root_id": root_id,
                        "xpath": xpath,
                    },
                )
            )
            return {"status": "PASS", "text": "Replaced text", "full_tree": {"must": "not leak"}}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.text.set_text"](
        selector={"automation_id": "CueTextBox"},
        text="Replaced text",
    )

    assert result["status"] == "PASS"
    assert result["route"] == "text_type_replace_selection"
    assert result["verified"] is True
    assert result["text"] == "Replaced text"
    assert result["precondition"]["selected"] is True
    assert "full_tree" not in str(result)
    assert backend.calls == [
        (
            "find_element",
            {
                "automation_id": "CueTextBox",
                "name": None,
                "control_type": None,
                "root_id": None,
                "xpath": None,
            },
        ),
        (
            "client_call",
            "set_focus",
            {"automationId": "CueTextBox"},
        ),
        ("send_keys", "^a"),
        ("textbox_state", {"automation_id": "CueTextBox"}),
        ("send_keys", "Replaced text"),
        (
            "extract_text",
            {
                "automation_id": "CueTextBox",
                "name": None,
                "control_type": None,
                "root_id": None,
                "xpath": None,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_ui_get_property_name_reads_element_property_not_text() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.extract_text_calls = 0
            self.find_element_calls = 0

        async def extract_text(self, **_: Any) -> dict[str, Any]:
            self.extract_text_calls += 1
            return {"status": "PASS", "text": "visible caption"}

        async def find_element(self, **_: Any) -> dict[str, Any]:
            self.find_element_calls += 1
            return {"status": "PASS", "name": "accessible name", "text": "visible caption"}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.get_property"](
        selector={"automation_id": "statusText"},
        property_name="name",
    )

    assert result["status"] == "PASS"
    assert result["value"] == "accessible name"
    assert backend.extract_text_calls == 0
    assert backend.find_element_calls == 1


@pytest.mark.asyncio
async def test_ui_get_property_preserves_selector_backend_errors() -> None:
    class FakeBackend:
        async def find_element(self, **_: Any) -> dict[str, Any]:
            return {"status": "BLOCKED", "reason": "selector schema unsupported"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.get_property"](
        selector={"automation_id": "statusText"},
        property_name="name",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "selector schema unsupported"
    assert "requested" not in result


@pytest.mark.asyncio
async def test_ui_get_property_blocks_backend_find_exceptions() -> None:
    class FakeBackend:
        async def find_element(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("bridge transport unavailable")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.get_property"](
        selector={"automation_id": "statusText"},
        property_name="name",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "bridge transport unavailable"
    assert result["requested"] == {"adapter": "ui.get_property"}


@pytest.mark.asyncio
async def test_ui_find_element_blocks_backend_exceptions() -> None:
    class FakeBackend:
        async def find_element(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("bridge transport unavailable")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.find_element"](
        selector={"automation_id": "statusText"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "bridge transport unavailable"
    assert result["requested"] == {"adapter": "ui.find_element"}


@pytest.mark.asyncio
async def test_ui_send_keys_focused_preserves_backend_failure_status() -> None:
    class FakeBackend:
        async def send_keys(self, keys: str) -> dict[str, Any]:
            return {
                "status": "BLOCKED",
                "reason": "focused key input rejected",
                "keys": keys,
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.send_keys_focused"](
        keys="{SPACE}",
    )

    assert result == {
        "status": "BLOCKED",
        "reason": "focused key input rejected",
        "keys": "{SPACE}",
    }


@pytest.mark.asyncio
async def test_ui_set_focus_blocks_bridge_call_errors() -> None:
    class FakeClient:
        async def call(self, _method: str, _payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("bridge disconnected")

    class FakeBackend:
        def __init__(self) -> None:
            self.client = FakeClient()

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.set_focus"](
        selector={"automation_id": "statusText"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "bridge disconnected"
    assert result["requested"] == {"adapter": "ui.set_focus"}


@pytest.mark.asyncio
async def test_session_operation_adapters_preserve_non_pass_statuses() -> None:
    class FailingSession(FakeRuntimeSmokeSession):
        async def launch(self, **_: Any) -> dict[str, Any]:
            self.launch_calls += 1
            return {"status": "BLOCKED", "reason": "program missing"}

        async def evaluate(self, expression: str) -> dict[str, Any]:
            return {
                "status": "FAIL",
                "reason": "expression failed",
                "expression": expression,
            }

        async def stop(self) -> dict[str, Any]:
            self.stop_calls += 1
            return {"status": "BLOCKED", "reason": "debuggee is busy"}

    class FakeBackend:
        pass

    session = FailingSession()

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    adapters = ui_operation_adapters(backend_provider, session=session)

    launch = await adapters["launch"](program="missing.dll")
    evaluate = await adapters["debug.evaluate"](expression="Settings.Mode")
    stop = await adapters["debug.stop"](mode="graceful")

    assert launch == {"status": "BLOCKED", "reason": "program missing"}
    assert evaluate == {
        "status": "FAIL",
        "reason": "expression failed",
        "expression": "Settings.Mode",
    }
    assert stop == {"status": "BLOCKED", "reason": "debuggee is busy"}


@pytest.mark.asyncio
async def test_process_registry_count_blocks_registry_errors() -> None:
    class FailingRegistry:
        def reap_stale(self) -> None:
            raise RuntimeError("registry unavailable")

        def status(self) -> list[dict[str, Any]]:
            return [{"alive": True}]

    class RegistrySession(FakeRuntimeSmokeSession):
        def __init__(self) -> None:
            super().__init__()
            self.process_registry = FailingRegistry()

    class FakeBackend:
        pass

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    adapters = ui_operation_adapters(backend_provider, session=RegistrySession())

    result = await adapters["process.registry.count"]()

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "registry unavailable"
    assert result["requested"] == {"adapter": "process.registry.count"}


@pytest.mark.asyncio
async def test_fixture_restore_returns_structured_failure_for_io_errors(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    baseline_dir = tmp_path / "baseline-dir"
    baseline_dir.mkdir()
    target = tmp_path / "settings.json"

    adapters = ui_operation_adapters(_no_ui_backend, session=session)
    result = await adapters["fixture.restore"](
        path=str(target),
        baseline_file=str(baseline_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["requested"]["adapter"] == "fixture.restore"
    assert "fixture baseline read failed" in result["reason"]


@pytest.mark.asyncio
async def test_fixture_restore_returns_structured_failure_for_write_errors(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    target_dir = tmp_path / "target-dir"
    target_dir.mkdir()

    adapters = ui_operation_adapters(_no_ui_backend, session=session)
    result = await adapters["fixture.restore"](
        path=str(target_dir),
        baseline_text="baseline",
    )

    assert result["status"] == "BLOCKED"
    assert result["requested"]["adapter"] == "fixture.restore"
    assert "fixture restore write failed" in result["reason"]


@pytest.mark.asyncio
async def test_state_changing_ui_operations_return_settle_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeRuntimeSmokeSession()
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    class FakeBackend:
        async def list_toggle_item_child(
            self,
            selector: dict[str, Any],
            item: dict[str, Any],
            child: dict[str, Any],
            target_state: str | None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selector": selector,
                "item": item,
                "child": child,
                "target_state": target_state,
                "toggled": True,
                "new_state": target_state,
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    monkeypatch.setattr(smoke_ops.asyncio, "sleep", fake_sleep)

    result = await RuntimeSmokeRunner(
        session,
        service_adapters=smoke_ops.ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [
                {
                    "op": "ui.list.toggle_item_child",
                    "selector": {"automation_id": "CharactersListBox"},
                    "item": {"name": "ALICE"},
                    "child": {"automation_id": "CharGender"},
                    "target_state": "Off",
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    step_result = result["completed_steps"][0]["result"]
    assert step_result["settled_ms"] == int(
        smoke_ops.STATE_CHANGE_SETTLE_SECONDS * 1000,
    )
    assert slept == [smoke_ops.STATE_CHANGE_SETTLE_SECONDS]


@pytest.mark.asyncio
async def test_ui_operation_adapters_forward_grid_columns_and_backend_text_status() -> None:
    session = FakeRuntimeSmokeSession()

    class FakeBackend:
        def __init__(self) -> None:
            self.grid_columns: list[str] = []

        async def grid_assert_rows(
            self,
            selector: dict[str, Any],
            rows: list[dict[str, Any]],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.grid_columns = list(columns or [])
            return {"status": "PASS", "selector": selector, "rows": rows}

        async def extract_text(self, **_: Any) -> dict[str, Any]:
            return {"status": "BLOCKED", "reason": "text backend unavailable"}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await RuntimeSmokeRunner(
        session,
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v1",
            "steps": [
                {
                    "op": "ui.grid.assert_rows",
                    "selector": {"automation_id": "CueGrid"},
                    "columns": ["Start", "Phrase"],
                    "rows": [{"index": 0, "contains": {"Phrase": "Fixture cue one"}}],
                },
                {
                    "op": "ui.text.assert",
                    "selector": {"automation_id": "status"},
                    "contains": "ready",
                },
            ],
        }
    )

    assert backend.grid_columns == ["Start", "Phrase"]
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "text backend unavailable"
    assert result["completed_steps"][1]["result"]["status"] == "BLOCKED"
    assert result["completed_steps"][1]["result"]["matched"] is False


@pytest.mark.asyncio
async def test_ui_text_assert_selector_miss_returns_actionable_blocked() -> None:
    class FakeBackend:
        async def extract_text(self, **_: Any) -> dict[str, Any]:
            return {
                "status": "FAIL",
                "found": False,
                "reason": "Element not found",
                "tree": {"children": [{"automation_id": "txtOutput"}]},
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await RuntimeSmokeRunner(
        FakeRuntimeSmokeSession(),
        service_adapters=ui_operation_adapters(backend_provider),
    ).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "missing_text",
                    "transitions": [
                        {
                            "probes": [
                                {
                                    "kind": "ui.text",
                                    "name": "missing_output",
                                    "phase": "after",
                                    "selector": {"automation_id": "missingTxtOutput"},
                                    "expected": "Done",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["blocked"]["reason"] == "selector not found"
    assert result["blocked"]["requested"] == {
        "selector": {"automation_id": "missingTxtOutput"}
    }
    assert result["blocked"]["accepted"]["selector_keys"]
    assert result["blocked"]["next_step"]
    assert result["blocked"]["backend_result"] == {
        "status": "FAIL",
        "found": False,
        "reason": "Element not found",
    }


@pytest.mark.asyncio
async def test_ui_text_assert_backend_exception_reports_bridge_diagnostics() -> None:
    class FakeBackend:
        async def extract_text(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("bridge pipe closed")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.text.assert"](
        selector={"automation_id": "statusText"},
        contains="ready",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "bridge pipe closed"
    assert result["requested"] == {"selector": {"automation_id": "statusText"}}
    assert result["accepted"] == {"backend": "connected UI backend supporting ui.text.assert"}
    assert result["next_step"] == "Inspect UI backend or bridge transport diagnostics."
    assert result["result"] == {"status": "BLOCKED", "reason": "bridge pipe closed"}


@pytest.mark.asyncio
async def test_ui_operation_adapters_route_point_drag_to_backend() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int, int, list[str]]] = []

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            modifiers = list(hold_modifiers or [])
            self.drag_calls.append((from_x, from_y, to_x, to_y, speed_ms, modifiers))
            return {
                "status": "PASS",
                "dragged": True,
                "x1": from_x,
                "y1": from_y,
                "x2": to_x,
                "y2": to_y,
                "duration_ms": speed_ms,
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "point", "point": {"relative_to": "screen", "x": 10, "y": 20}},
        path=[{"relative_to": "screen", "x": 25, "y": 40}],
        drop={"relative_to": "screen", "x": 50, "y": 80},
        modifiers=["ctrl"],
        duration_ms=350,
    )

    assert backend.drag_calls == [(10, 20, 50, 80, 350, ["ctrl"])]
    assert result["status"] == "PASS"
    assert result["backend"] == "FakeBackend"
    assert result["route_evidence"]["source"]["kind"] == "point"
    assert result["route_evidence"]["start"] == {"x": 10, "y": 20}
    assert result["route_evidence"]["drop"] == {"x": 50, "y": 80}
    assert "move_points" not in result["route_evidence"]
    assert "hold_points" not in result["route_evidence"]
    assert "final_pointer" not in result["route_evidence"]


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_blocks_route_resolution_exceptions() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            raise RuntimeError("grid unavailable")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "screen", "x": 80, "y": 90},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "drag route resolution raised exception"
    assert result["exception"] == {
        "type": "RuntimeError",
        "message": "grid unavailable",
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_blocks_selector_lookup_fail_status() -> None:
    class FakeBackend:
        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "BLOCKED",
                "found": True,
                "reason": "ambiguous selector",
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "selector", "selector": {"automation_id": "dragHandle"}},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "screen", "x": 80, "y": 90},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ambiguous selector"


@pytest.mark.asyncio
async def test_ui_operation_adapters_path_drag_blocks_without_backend_route_proof() -> None:
    class FakeBackend:
        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "point", "point": {"relative_to": "screen", "x": 10, "y": 20}},
        path=[
            {"relative_to": "screen", "x": 10, "y": 20},
            {"relative_to": "screen", "x": 20, "y": 30, "hold_ms": 50},
            {"relative_to": "screen", "x": 30, "y": 40},
        ],
        drop={"relative_to": "screen", "x": 30, "y": 40},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "path-aware drag backend did not return route evidence"


@pytest.mark.asyncio
async def test_ui_operation_adapters_resolve_visible_row_drag_sources_to_backend() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int, int, list[str]]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 4,
                "visible_rows": [
                    {
                        "index": 0,
                        "bounds": {"x": 10, "y": 20, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue one"},
                    },
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                    {
                        "index": 2,
                        "bounds": {"x": 10, "y": 100, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue three"},
                    },
                    {
                        "index": 3,
                        "bounds": {"x": 10, "y": 140, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue four"},
                    },
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            modifiers = list(hold_modifiers or [])
            self.drag_calls.append((from_x, from_y, to_x, to_y, speed_ms, modifiers))
            return {
                "status": "PASS",
                "dragged": True,
                "path_points": [
                    {"x": from_x, "y": from_y},
                    {"x": from_x, "y": to_y},
                    {"x": to_x, "y": to_y},
                ],
                "final_pointer": {"x": to_x, "y": to_y},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {"relative_to": "drop", "x": 0.5, "y": 0.5},
        ],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 3},
        duration_ms=450,
    )

    assert backend.drag_calls == [(70, 75, 70, 155, 450, [])]
    assert result["status"] == "PASS"
    assert result["route_evidence"]["source_bounds"] == {
        "x": 10,
        "y": 60,
        "width": 120,
        "height": 30,
    }
    assert result["route_evidence"]["target_bounds"] == {
        "x": 10,
        "y": 140,
        "width": 120,
        "height": 30,
    }
    assert result["route_evidence"]["source_identity"] == "Cue two"
    assert result["route_evidence"]["target_identity"] == "Cue four"
    assert result["route_evidence"]["move_points"] == [
        {"x": 70, "y": 75},
        {"x": 70, "y": 155},
        {"x": 70, "y": 155},
    ]
    assert result["route_evidence"]["final_pointer"] == {"x": 70, "y": 155}

    top_row_result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 0},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {"relative_to": "drop", "x": 0.5, "y": 0.5},
        ],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 2},
        duration_ms=300,
    )

    assert top_row_result["status"] == "PASS"
    assert backend.drag_calls[-1] == (70, 35, 70, 115, 300, [])
    assert top_row_result["route_evidence"]["source_identity"] == "Cue one"


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_preserves_row_identity_source_offset() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 4,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                    {
                        "index": 3,
                        "bounds": {"x": 10, "y": 140, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue four"},
                    },
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_calls.append((from_x, from_y, to_x, to_y))
            return {"status": "PASS", "dragged": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={
            "kind": "row_identity",
            "selector": {"automation_id": "dataGrid"},
            "row_identity": "Cue two",
        },
        path=[
            {"relative_to": "source", "x": -0.01, "y": 0.5},
            {"relative_to": "drop", "x": 0.5, "y": 0.5},
        ],
        drop={"selector": {"automation_id": "dataGrid"}, "row_identity": "Cue four"},
        identity={"column": "Phrase"},
    )

    assert result["status"] == "PASS"
    assert backend.drag_calls == [(10, 75, 70, 155)]
    assert result["route_evidence"]["source_point"] == {"x": 10, "y": 75}
    assert result["route_evidence"]["target_point"] == {"x": 70, "y": 155}


@pytest.mark.asyncio
async def test_ui_operation_adapters_resolve_viewport_drop_from_source_selector() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    }
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "found": True,
                "automation_id": automation_id,
                "rect": {"x": 0, "y": 0, "width": 200, "height": 300},
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_calls.append((from_x, from_y, to_x, to_y))
            return {"status": "PASS", "dragged": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "viewport", "x": 0.5, "y": 0.8},
    )

    assert result["status"] == "PASS"
    assert backend.drag_calls == [(70, 75, 100, 240)]
    assert result["route_evidence"]["target_bounds"] == {
        "x": 0,
        "y": 0,
        "width": 200,
        "height": 300,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_resolve_viewport_drop_from_grid_snapshot_fallback() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_path_calls: list[list[dict[str, Any]]] = []
            self.find_element_calls: list[dict[str, Any]] = []
            self.grid_snapshot_calls: list[dict[str, Any]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.grid_snapshot_calls.append(dict(selector))
            return {
                "status": "PASS",
                "row_count": 1000,
                "grid_bounds": {"x": 20, "y": 40, "width": 300, "height": 500},
                "visible_rows": [
                    {
                        "index": 8,
                        "bounds": {"x": 30, "y": 120, "width": 250, "height": 30},
                        "cells": {"Phrase": "ROW-008-UNIQUE-PHRASE"},
                    },
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            self.find_element_calls.append(
                {
                    "automation_id": automation_id,
                    "name": name,
                    "control_type": control_type,
                    "root_id": root_id,
                    "xpath": xpath,
                }
            )
            return {"status": "PASS", "found": False}

        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_path_calls.append(points)
            return {
                "status": "PASS",
                "path_points": points,
                "hold_points": [point for point in points if point.get("hold_ms")],
                "final_pointer": points[-1],
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    selector = {"automation_id": "CueDataGrid", "control_type": "DataGrid"}
    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={
            "kind": "row_identity",
            "selector": selector,
            "row_identity": "ROW-008-UNIQUE-PHRASE",
        },
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {
                "relative_to": "viewport",
                "selector": selector,
                "x": 0.5,
                "y": 0.96,
                "hold_ms": 350,
            },
        ],
        drop={
            "relative_to": "viewport",
            "selector": selector,
            "x": 0.5,
            "y": 0.86,
        },
        identity={"column": "Phrase"},
    )

    assert result["status"] == "PASS"
    assert backend.find_element_calls
    assert backend.grid_snapshot_calls == [selector, selector, selector]
    assert backend.drag_path_calls == [
        [
            {"x": 155, "y": 135},
            {"x": 170, "y": 520, "hold_ms": 350},
            {"x": 170, "y": 470},
        ]
    ]
    assert result["route_evidence"]["target_bounds"] == {
        "x": 20,
        "y": 40,
        "width": 300,
        "height": 500,
    }
    assert result["route_evidence"]["target_identity"] == "CueDataGrid"


@pytest.mark.asyncio
async def test_ui_operation_adapters_do_not_use_grid_snapshot_for_selector_failures() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.grid_snapshot_calls: list[dict[str, Any]] = []

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "BLOCKED",
                "found": True,
                "reason": "ambiguous selector",
            }

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.grid_snapshot_calls.append(dict(selector))
            return {
                "status": "PASS",
                "grid_bounds": {"x": 20, "y": 40, "width": 300, "height": 500},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    selector = {"automation_id": "CueDataGrid", "control_type": "DataGrid"}
    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "point", "point": {"relative_to": "screen", "x": 10, "y": 20}},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "viewport", "selector": selector, "x": 0.5, "y": 0.86},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ambiguous selector"
    assert backend.grid_snapshot_calls == []


@pytest.mark.asyncio
async def test_ui_operation_adapters_resolve_viewport_drop_from_nested_row_bounds() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int]] = []

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "found": False}

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 1000,
                "visible_rows": [
                    {
                        "index": 8,
                        "grid_bounds": {"x": 20, "y": 40, "width": 300, "height": 40},
                        "cells": {"Phrase": "ROW-008"},
                    },
                    {
                        "index": 9,
                        "viewport_bounds": {
                            "x": 20,
                            "y": 80,
                            "width": 300,
                            "height": 40,
                        },
                        "cells": {"Phrase": "ROW-009"},
                    },
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_calls.append((from_x, from_y, to_x, to_y))
            return {"status": "PASS", "dragged": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    selector = {"automation_id": "CueDataGrid", "control_type": "DataGrid"}
    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "point", "point": {"relative_to": "screen", "x": 10, "y": 20}},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "viewport", "selector": selector, "x": 0.5, "y": 0.5},
    )

    assert result["status"] == "PASS"
    assert backend.drag_calls == [(10, 20, 170, 80)]
    assert result["route_evidence"]["target_bounds"] == {
        "x": 20,
        "y": 40,
        "width": 300,
        "height": 80,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_prefers_backend_row_index_for_drag_resolution() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_calls: list[tuple[int, int, int, int]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 24,
                "visible_rows": [
                    {
                        "index": 0,
                        "row_index": 18,
                        "bounds": {"x": 10, "y": 20, "width": 120, "height": 30},
                        "cells": {"PhraseId": "Cue 018"},
                    },
                    {
                        "index": 1,
                        "row_index": 19,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"PhraseId": "Cue 019"},
                    },
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_calls.append((from_x, from_y, to_x, to_y))
            return {"status": "PASS", "dragged": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 19},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 18},
    )

    assert result["status"] == "PASS"
    assert backend.drag_calls == [(70, 75, 70, 35)]
    assert result["route_evidence"]["source_identity"] == "Cue 019"
    assert result["route_evidence"]["target_identity"] == "Cue 018"


@pytest.mark.asyncio
async def test_ui_operation_adapters_resolves_cached_element_drag_source() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.element_cache = {
                "dragHandle": {
                    "rect": {"x": 30, "y": 40, "width": 20, "height": 10},
                    "name": "Drag Handle",
                }
            }
            self.drag_calls: list[tuple[int, int, int, int]] = []

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            self.drag_calls.append((from_x, from_y, to_x, to_y))
            return {"status": "PASS", "dragged": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"cached_element": "dragHandle"},
        path=[{"relative_to": "screen", "x": 60, "y": 70}],
        drop={"relative_to": "screen", "x": 80, "y": 90},
    )

    assert result["status"] == "PASS"
    assert backend.drag_calls == [(40, 45, 80, 90)]
    assert result["route_evidence"]["source_identity"] == "Drag Handle"


@pytest.mark.asyncio
async def test_ui_operation_adapters_route_held_edge_drag_through_drag_path() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_path_calls: list[tuple[list[dict[str, Any]], int, list[str]]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 24,
                "visible_rows": [
                    {
                        "index": 0,
                        "bounds": {"x": 10, "y": 20, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue one"},
                    },
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                    {
                        "index": 2,
                        "bounds": {"x": 10, "y": 100, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue three"},
                    },
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "found": True,
                "automation_id": automation_id,
                "rect": {"x": 10, "y": 20, "width": 120, "height": 200},
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            raise AssertionError("held edge drag must use drag_path")

        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            modifiers = list(hold_modifiers or [])
            self.drag_path_calls.append((points, speed_ms, modifiers))
            return {
                "status": "PASS",
                "path_points": points,
                "hold_points": [point for point in points if point.get("hold_ms")],
                "final_pointer": points[-1],
                "duration_ms": speed_ms,
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {
                "relative_to": "viewport",
                "selector": {"automation_id": "dataGrid"},
                "x": 0.5,
                "y": 0.96,
                "hold_ms": 900,
            },
            {
                "relative_to": "viewport",
                "selector": {"automation_id": "dataGrid"},
                "x": 0.5,
                "y": 0.90,
            },
        ],
        drop={
            "relative_to": "viewport",
            "selector": {"automation_id": "dataGrid"},
            "x": 0.5,
            "y": 0.90,
        },
        duration_ms=700,
    )

    assert result["status"] == "PASS"
    assert backend.drag_path_calls == [
        (
            [
                {"x": 70, "y": 75},
                {"x": 70, "y": 212, "hold_ms": 900},
                {"x": 70, "y": 200},
            ],
            700,
            [],
        )
    ]
    assert result["route_evidence"]["move_points"] == [
        {"x": 70, "y": 75},
        {"x": 70, "y": 212, "hold_ms": 900},
        {"x": 70, "y": 200},
    ]
    assert result["route_evidence"]["hold_points"] == [
        {"x": 70, "y": 212, "hold_ms": 900}
    ]
    assert result["route_evidence"]["final_pointer"] == {"x": 70, "y": 200}


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_reports_no_op_cleanup_evidence() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.drag_path_calls: list[tuple[list[dict[str, Any]], int, list[str]]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 3,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "found": True,
                "automation_id": automation_id,
                "rect": {"x": 10, "y": 20, "width": 120, "height": 200},
            }

        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            modifiers = list(hold_modifiers or [])
            self.drag_path_calls.append((points, speed_ms, modifiers))
            return {
                "status": "PASS",
                "path_points": points,
                "final_pointer": points[-1],
                "no_op": {
                    "expected": True,
                    "reason": "small_movement",
                    "route_attempted": True,
                    "movement_px": 1,
                },
                "modifier_cleanup": {"released": modifiers},
                "pointer_cleanup": {"left_button_released": True},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {
                "relative_to": "viewport",
                "selector": {"automation_id": "dataGrid"},
                "x": 0.5,
                "y": 0.27,
            },
            {
                "relative_to": "viewport",
                "selector": {"automation_id": "dataGrid"},
                "x": 0.5,
                "y": 0.28,
            },
        ],
        drop={
            "relative_to": "viewport",
            "selector": {"automation_id": "dataGrid"},
            "x": 0.5,
            "y": 0.28,
        },
        modifiers=["shift"],
        expect={"no_op": True, "no_op_reason": "small_movement"},
    )

    assert result["status"] == "PASS"
    assert result["no_op"] == {
        "expected": True,
        "reason": "small_movement",
        "route_attempted": True,
        "movement_px": 1,
    }
    assert result["cleanup"] == {
        "modifier_cleanup": {"released": ["shift"]},
        "pointer_cleanup": {"left_button_released": True},
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_does_not_synthesize_no_op_cleanup_evidence() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 3,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "found": True,
                "rect": {"x": 10, "y": 20, "width": 120, "height": 200},
            }

        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "path_points": points,
                "final_pointer": points[-1],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {"relative_to": "viewport", "x": 0.5, "y": 0.27},
            {"relative_to": "viewport", "x": 0.5, "y": 0.28},
        ],
        drop={"relative_to": "viewport", "x": 0.5, "y": 0.28},
        modifiers=["shift"],
        expect={"no_op": True, "no_op_reason": "small_movement"},
    )

    assert result["status"] == "PASS"
    assert "no_op" not in result
    assert "cleanup" not in result


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_passes_cancel_to_path_backend() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.cancel_key: str | None = None

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 3,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue two"},
                    },
                    {
                        "index": 2,
                        "bounds": {"x": 10, "y": 100, "width": 120, "height": 30},
                        "cells": {"Phrase": "Cue three"},
                    },
                ],
            }

        async def drag_path(
            self,
            points: list[dict[str, Any]],
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
            cancel_key: str | None = None,
        ) -> dict[str, Any]:
            self.cancel_key = cancel_key
            return {
                "status": "PASS",
                "path_points": points,
                "final_pointer": points[-1],
                "modifier_cleanup": {"released": []},
                "pointer_cleanup": {"left_button_released": True},
                "no_op": {"expected": True, "reason": "cancelled"},
                "cancel": {"key": cancel_key, "sent": True},
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"kind": "row_index", "selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[
            {"relative_to": "source", "x": 0.5, "y": 0.5},
            {"relative_to": "drop", "x": 0.5, "y": 0.5},
        ],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 2},
        cancel={"key": "escape"},
        expect={"no_op": True, "no_op_reason": "cancelled"},
    )

    assert result["status"] == "PASS"
    assert backend.cancel_key == "escape"
    assert result["cancel"] == {"key": "escape", "sent": True}
    assert result["no_op"]["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_grid_select_indices_uses_grid_select_range_for_contiguous_indices() -> None:
    class FakeBackend:
        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            self.selector = selector
            self.start_index = start_index
            self.end_index = end_index
            return {
                "status": "PASS",
                "selected_range": {"start": start_index, "end": end_index},
                "selected_rows": [
                    {"row_index": index}
                    for index in range(start_index, end_index + 1)
                ],
            }

        async def multi_select(self, container_id: str, indices: list[int]) -> int:
            raise AssertionError(
                "contiguous DataGrid selection must use selector-aware grid_select_range"
            )

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_indices"](
        selector={"automation_id": "dataGrid", "control_type": "DataGrid"},
        indices=[3, 1, 2],
    )

    assert result["status"] == "PASS"
    assert backend.selector == {"automation_id": "dataGrid", "control_type": "DataGrid"}
    assert backend.start_index == 1
    assert backend.end_index == 3
    assert result["selected_indices"] == [3, 1, 2]
    assert result["selected_count"] == 3
    assert result["selected_range"] == {"start": 1, "end": 3}


@pytest.mark.asyncio
async def test_grid_select_indices_uses_backend_multi_select_for_sparse_indices() -> None:
    class FakeBackend:
        async def multi_select(self, container_id: str, indices: list[int]) -> int:
            self.container_id = container_id
            self.indices = indices
            return len(indices)

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_indices"](
        selector={"automation_id": "dataGrid"},
        indices=[1, 4],
    )

    assert result["status"] == "PASS"
    assert backend.container_id == "dataGrid"
    assert backend.indices == [1, 4]
    assert result["selected_indices"] == [1, 4]


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_select_indices_blocks_empty_indices() -> None:
    class FakeBackend:
        async def multi_select(self, container_id: str, indices: list[int]) -> int:
            raise AssertionError("empty selection must fail before backend call")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_indices"](
        selector={"automation_id": "dataGrid"},
        indices=[],
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "indices list cannot be empty"
    assert result["requested"] == {"adapter": "ui.grid.select_indices"}


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_select_indices_blocks_invalid_indices() -> None:
    class FakeBackend:
        async def multi_select(self, container_id: str, indices: list[int]) -> int:
            raise AssertionError("invalid selection must fail before backend call")

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_indices"](
        selector={"automation_id": "dataGrid"},
        indices=["a", 1],
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "indices must be non-negative integers"
    assert result["requested"] == {"adapter": "ui.grid.select_indices"}


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_returns_selected_payload_evidence() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.selected_results = [
                ["Cue 001", "Cue 004"],
                ["Cue 001", "Cue 004"],
            ]

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 6,
                "visible_rows": [
                    {
                        "index": index,
                        "selected": False,
                        "bounds": {
                            "x": 10,
                            "y": 20 + (index * 30),
                            "width": 120,
                            "height": 25,
                        },
                        "cells": {"Phrase": phrase},
                    }
                    for index, phrase in enumerate(
                        [
                            "Cue 000",
                            "Cue 001",
                            "Cue 002",
                            "Cue 003",
                            "Cue 004",
                            "Cue 005",
                        ]
                    )
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            _ = columns
            selected = self.selected_results.pop(0)
            return {
                "status": "PASS",
                "selected_rows": [
                    {
                        "index": index,
                        "cells": {"Phrase": phrase},
                    }
                    for index, phrase in enumerate(selected)
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "backend": "fake"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 4},
        expect={"selected_payload_preserved": True},
    )

    assert result["status"] == "PASS"
    assert result["selected_payload"] == {
        "before": ["Cue 001", "Cue 004"],
        "after": ["Cue 001", "Cue 004"],
        "preserved": True,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_fails_when_selected_payload_changes() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.selected_results = [["Cue 001", "Cue 004"]] + [
                ["Cue 001", "Cue 005"] for _ in range(10)
            ]

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 6,
                "visible_rows": [
                    {
                        "index": index,
                        "bounds": {
                            "x": 10,
                            "y": 20 + (index * 30),
                            "width": 120,
                            "height": 25,
                        },
                        "cells": {"Phrase": phrase},
                    }
                    for index, phrase in enumerate(
                        [
                            "Cue 000",
                            "Cue 001",
                            "Cue 002",
                            "Cue 003",
                            "Cue 004",
                            "Cue 005",
                        ]
                    )
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            _ = columns
            selected = self.selected_results.pop(0)
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": index, "cells": {"Phrase": phrase}}
                    for index, phrase in enumerate(selected)
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "backend": "fake"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 4},
        expect={"selected_payload_preserved": True},
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "selected payload was not preserved after drag"
    assert result["selected_payload"] == {
        "before": ["Cue 001", "Cue 004"],
        "after": ["Cue 001", "Cue 005"],
        "preserved": False,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_blocks_selected_payload_postflight_exception() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.selected_calls = 0

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 20, "width": 120, "height": 25},
                        "cells": {"Phrase": "Cue 001"},
                    }
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            _ = columns
            self.selected_calls += 1
            if self.selected_calls > 1:
                raise RuntimeError("selection probe failed")
            return {
                "status": "PASS",
                "selected_rows": [{"index": 1, "cells": {"Phrase": "Cue 001"}}],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "backend": "fake"}

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"relative_to": "screen", "x": 80, "y": 90},
        expect={"selected_payload_preserved": True},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "selected payload postflight raised exception"
    assert result["exception"] == {
        "type": "RuntimeError",
        "message": "selection probe failed",
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_uses_configured_identity_for_rows() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.grid_snapshot_columns: list[list[str]] = []
            self.grid_selected_columns: list[list[str]] = []
            self.selected_results = [
                ["Task 001", "Task 004"],
                ["Task 001", "Task 004"],
            ]

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.grid_snapshot_columns.append(list(columns or []))
            return {
                "status": "PASS",
                "row_count": 6,
                "visible_rows": [
                    {
                        "index": index,
                        "bounds": {
                            "x": 10,
                            "y": 20 + (index * 30),
                            "width": 120,
                            "height": 25,
                        },
                        "cells": {"Title": title},
                    }
                    for index, title in enumerate(
                        [
                            "Task 000",
                            "Task 001",
                            "Task 002",
                            "Task 003",
                            "Task 004",
                            "Task 005",
                        ]
                    )
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.grid_selected_columns.append(list(columns or []))
            selected = self.selected_results.pop(0)
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": index, "cells": {"Title": title}}
                    for index, title in enumerate(selected)
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "backend": "fake"}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 4},
        identity={"column": "Title"},
        expect={"selected_payload_preserved": True},
    )

    assert backend.grid_snapshot_columns == [["Title"], ["Title"]]
    assert backend.grid_selected_columns == [["Title"], ["Title"]]
    assert result["route_evidence"]["source_identity"] == "Task 001"
    assert result["route_evidence"]["target_identity"] == "Task 004"
    assert result["selected_payload"] == {
        "before": ["Task 001", "Task 004"],
        "after": ["Task 001", "Task 004"],
        "preserved": True,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_drag_waits_for_selected_payload_to_settle() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.selected_results = [
                ["Cue 001", "Cue 002"],
                ["Cue 001"],
                ["Cue 001", "Cue 002"],
            ]

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 6,
                "visible_rows": [
                    {
                        "index": index,
                        "selected": False,
                        "bounds": {
                            "x": 10,
                            "y": 20 + (index * 30),
                            "width": 120,
                            "height": 25,
                        },
                        "cells": {"Phrase": phrase},
                    }
                    for index, phrase in enumerate(
                        [
                            "Cue 000",
                            "Cue 001",
                            "Cue 002",
                            "Cue 003",
                            "Cue 004",
                            "Cue 005",
                        ]
                    )
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            _ = columns
            selected = self.selected_results.pop(0)
            return {
                "status": "PASS",
                "selected_rows": [
                    {
                        "index": index,
                        "cells": {"Phrase": phrase},
                    }
                    for index, phrase in enumerate(selected)
                ],
            }

        async def drag(
            self,
            from_x: int,
            from_y: int,
            to_x: int,
            to_y: int,
            speed_ms: int = 200,
            hold_modifiers: list[str] | None = None,
        ) -> dict[str, Any]:
            return {"status": "PASS", "backend": "fake"}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.drag"](
        source={"selector": {"automation_id": "dataGrid"}, "row_index": 1},
        path=[{"relative_to": "source", "x": 0.5, "y": 0.5}],
        drop={"selector": {"automation_id": "dataGrid"}, "row_index": 4},
        expect={"selected_payload_preserved": True},
    )

    assert result["status"] == "PASS"
    assert result["selected_payload"] == {
        "before": ["Cue 001", "Cue 002"],
        "after": ["Cue 001", "Cue 002"],
        "preserved": True,
    }
    assert backend.selected_results == []


@pytest.mark.asyncio
async def test_ui_operation_adapters_build_grid_viewport_snapshot_with_derived_identity() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {
                        "index": 0,
                        "selected": True,
                        "cells": {"PhraseId": "Cue 001"},
                    },
                    {
                        "index": 1,
                        "selected": False,
                        "cells": {},
                    },
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.viewport"](
        selector={"automation_id": "dataGrid"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        expect={},
        phase="before",
        probe_name="cue_viewport",
    )

    assert result["status"] == "PASS"
    snapshot = result["snapshot"]
    assert snapshot["first_visible_index"] == 0
    assert snapshot["last_visible_index"] == 1
    assert snapshot["visible_rows"] == [
        {"index": 0, "identity": "Cue 001", "derived": False},
        {"index": 1, "identity": "row:1", "derived": True},
    ]
    assert snapshot["selected_rows"] == [
        {"index": 0, "identity": "Cue 001", "derived": False}
    ]
    assert snapshot["identity_strategy"] == {
        "kind": "configured_column",
        "column": "PhraseId",
        "derived": True,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_prefers_backend_row_index_for_viewport_snapshot() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 24,
                "visible_rows": [
                    {
                        "index": 0,
                        "row_index": 18,
                        "selected": False,
                        "cells": {"PhraseId": "Cue 018"},
                    },
                    {
                        "index": 1,
                        "row_index": 19,
                        "selected": True,
                        "cells": {"PhraseId": "Cue 019"},
                    },
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.viewport"](
        selector={"automation_id": "dataGrid"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        expect={},
        phase="after",
        probe_name="cue_viewport",
    )

    assert result["status"] == "PASS"
    snapshot = result["snapshot"]
    assert snapshot["first_visible_index"] == 18
    assert snapshot["last_visible_index"] == 19
    assert snapshot["visible_rows"] == [
        {"index": 18, "identity": "Cue 018", "derived": False},
        {"index": 19, "identity": "Cue 019", "derived": False},
    ]
    assert snapshot["selected_rows"] == [
        {"index": 19, "identity": "Cue 019", "derived": False}
    ]


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_get_state_combines_visible_and_selected_rows() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {"index": 0, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                    {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}},
                ],
                "rows": dict(rows or {}),
                "columns": list(columns or []),
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}}
                ],
                "columns": list(columns or []),
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.get_state"](
        selector={"automation_id": "dataGrid"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        columns=["PhraseId"],
    )

    assert result["status"] == "PASS"
    assert result["visible_rows"][1]["row_index"] == 19
    assert result["selected_rows"] == [
        {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}}
    ]
    assert result["identity_strategy"] == {
        "kind": "configured_column",
        "column": "PhraseId",
        "derived": True,
    }


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_select_row_resolves_visible_identity() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 0, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                    {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}},
                ],
            }

        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_range": {"start": start_index, "end": end_index},
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}}
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_row"](
        selector={"automation_id": "dataGrid"},
        row={"identity": "Cue 019"},
        identity={"column": "PhraseId"},
        columns=["PhraseId"],
    )

    assert result["status"] == "PASS"
    assert result["resolved_row"] == {
        "index": 1,
        "row_index": 19,
        "identity": "Cue 019",
    }
    assert result["confirmed_selection"] is True


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_select_row_can_opt_in_to_ensure_visible_first() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.ensure_requests: list[dict[str, Any]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return {
                    "status": "PASS",
                    "visible_rows": [
                        {"index": 0, "row_index": 18, "cells": {"PhraseId": "Cue 018"}}
                    ],
                }
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 0, "row_index": 42, "cells": {"PhraseId": "Cue 042"}}
                ],
            }

        async def grid_ensure_visible(
            self,
            selector: dict[str, Any],
            **request: Any,
        ) -> dict[str, Any]:
            self.ensure_requests.append({"selector": dict(selector), **request})
            return {"status": "PASS", "realized": True}

        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_range": {"start": start_index, "end": end_index},
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 0, "row_index": 42, "cells": {"PhraseId": "Cue 042"}}
                ],
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_row"](
        selector={"automation_id": "dataGrid"},
        row={"identity": "Cue 042"},
        identity={"column": "PhraseId"},
        columns=["PhraseId"],
        rows={"visible_only": True},
        ensure_visible=True,
        max_scrolls=12,
        scroll_settle_ms=25,
    )

    assert result["status"] == "PASS"
    assert result["ensure_visible_result"] == {
        "status": "PASS",
        "realized": True,
        "already_visible": False,
        "resolved_row": {"index": 0, "row_index": 42, "identity": "Cue 042"},
    }
    assert result["resolved_row"] == {
        "index": 0,
        "row_index": 42,
        "identity": "Cue 042",
    }
    assert result["confirmed_selection"] is True
    assert backend.ensure_requests == [
        {
            "selector": {"automation_id": "dataGrid"},
            "row_key": "Cue 042",
            "identity": {"column": "PhraseId"},
            "rows": {"visible_only": True},
            "columns": ["PhraseId"],
            "max_scrolls": 12,
            "scroll_settle_ms": 25,
        }
    ]
    assert backend.snapshot_calls == 3


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_select_identities_resolves_visible_indices() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.selector: dict[str, Any] | None = None
            self.start_index: int | None = None
            self.end_index: int | None = None

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 5, "row_index": 16, "cells": {"PhraseId": "Cue 016"}},
                    {"index": 6, "row_index": 17, "cells": {"PhraseId": "Cue 017"}},
                    {"index": 7, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                ],
                "rows": dict(rows or {}),
                "columns": list(columns or []),
            }

        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            self.selector = dict(selector)
            self.start_index = start_index
            self.end_index = end_index
            return {
                "status": "PASS",
                "selected_range": {"start": start_index, "end": end_index},
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 5, "row_index": 16, "cells": {"PhraseId": "Cue 016"}},
                    {"index": 6, "row_index": 17, "cells": {"PhraseId": "Cue 017"}},
                    {"index": 7, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                ],
                "columns": list(columns or []),
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_identities"](
        selector={"automation_id": "dataGrid"},
        row_identities=["Cue 016", "Cue 017", "Cue 018"],
        identity={"column": "PhraseId"},
        columns=["PhraseId"],
    )

    assert result["status"] == "PASS"
    assert backend.selector == {"automation_id": "dataGrid"}
    assert backend.start_index == 5
    assert backend.end_index == 7
    assert result["selected_indices"] == [5, 6, 7]
    assert result["selected_identities"] == ["Cue 016", "Cue 017", "Cue 018"]
    assert result["confirmed_selection"] is True


@pytest.mark.asyncio
async def test_grid_select_identities_rejects_empty_identity_values() -> None:
    class FakeBackend:
        pass

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_identities"](
        selector={"automation_id": "dataGrid"},
        row_identities=["Cue 016", ""],
        identity={"column": "PhraseId"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "row_identities list cannot contain empty values"


@pytest.mark.asyncio
async def test_grid_select_identities_matches_only_stable_identity_column() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "visible_rows": [
                    {
                        "index": 1,
                        "row_index": 1,
                        "cells": {"PhraseId": "Cue 001", "Text": "Cue 016"},
                    },
                    {
                        "index": 2,
                        "row_index": 2,
                        "cells": {"PhraseId": "Cue 016", "Text": "Actual"},
                    },
                ],
            }

        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            self.start_index = start_index
            self.end_index = end_index
            return {"status": "PASS"}

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {
                        "index": 2,
                        "row_index": 2,
                        "cells": {"PhraseId": "Cue 016", "Text": "Actual"},
                    }
                ],
            }

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_identities"](
        selector={"automation_id": "dataGrid"},
        row_identities=["Cue 016"],
        identity={"column": "PhraseId"},
        columns=["PhraseId", "Text"],
    )

    assert result["status"] == "PASS"
    assert backend.start_index == 2
    assert backend.end_index == 2
    assert result["resolved_rows"] == [
        {"index": 2, "identity": "Cue 016", "row_index": 2}
    ]


@pytest.mark.asyncio
async def test_grid_select_identities_confirms_selection_in_ui_order() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 5, "row_index": 16, "cells": {"PhraseId": "Cue 016"}},
                    {"index": 6, "row_index": 17, "cells": {"PhraseId": "Cue 017"}},
                    {"index": 7, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                ],
            }

        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_range": {"start": start_index, "end": end_index},
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 5, "row_index": 16, "cells": {"PhraseId": "Cue 016"}},
                    {"index": 6, "row_index": 17, "cells": {"PhraseId": "Cue 017"}},
                    {"index": 7, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.select_identities"](
        selector={"automation_id": "dataGrid"},
        row_identities=["Cue 018", "Cue 016", "Cue 017"],
        identity={"column": "PhraseId"},
        columns=["PhraseId"],
    )

    assert result["status"] == "PASS"
    assert result["confirmed_selection"] is True
    assert result["selected_identities"] == ["Cue 018", "Cue 016", "Cue 017"]
    assert result["observed_selected_identities"] == [
        "Cue 016",
        "Cue 017",
        "Cue 018",
    ]


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_click_row_can_opt_in_to_ensure_visible_first() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.snapshot_calls = 0
            self.ensure_requests: list[dict[str, Any]] = []
            self.click_requests: list[dict[str, Any]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return {
                    "status": "PASS",
                    "visible_rows": [
                        {"index": 0, "row_index": 18, "cells": {"PhraseId": "Cue 018"}}
                    ],
                }
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 0, "row_index": 42, "cells": {"PhraseId": "Cue 042"}}
                ],
            }

        async def grid_ensure_visible(
            self,
            selector: dict[str, Any],
            **request: Any,
        ) -> dict[str, Any]:
            self.ensure_requests.append({"selector": dict(selector), **request})
            return {"status": "PASS", "realized": True}

        async def grid_click_row(
            self,
            selector: dict[str, Any],
            visible_index: int,
            *,
            column: str | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.click_requests.append(
                {
                    "selector": dict(selector),
                    "visible_index": visible_index,
                    "column": column,
                    "columns": columns,
                }
            )
            return {"status": "PASS", "clicked_visible_index": visible_index}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.click_row"](
        selector={"automation_id": "dataGrid"},
        row={"identity": "Cue 042"},
        identity={"column": "PhraseId"},
        columns=["PhraseId"],
        rows={"visible_only": True},
        column="PhraseId",
        ensure_visible=True,
        max_scrolls=12,
        scroll_settle_ms=25,
    )

    assert result["status"] == "PASS"
    assert result["ensure_visible_result"] == {
        "status": "PASS",
        "realized": True,
        "already_visible": False,
        "resolved_row": {"index": 0, "row_index": 42, "identity": "Cue 042"},
    }
    assert result["resolved_row"] == {
        "index": 0,
        "row_index": 42,
        "identity": "Cue 042",
    }
    assert backend.ensure_requests == [
        {
            "selector": {"automation_id": "dataGrid"},
            "row_key": "Cue 042",
            "identity": {"column": "PhraseId"},
            "rows": {"visible_only": True},
            "columns": ["PhraseId"],
            "max_scrolls": 12,
            "scroll_settle_ms": 25,
        }
    ]
    assert backend.click_requests == [
        {
            "selector": {"automation_id": "dataGrid"},
            "visible_index": 0,
            "column": "PhraseId",
            "columns": ["PhraseId"],
        }
    ]
    assert backend.snapshot_calls == 3


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_click_row_requires_backend_click_primitive() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 0, "row_index": 18, "cells": {"PhraseId": "Cue 018"}},
                    {"index": 1, "row_index": 19, "cells": {"PhraseId": "Cue 019"}},
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.click_row"](
        selector={"automation_id": "dataGrid"},
        row={"identity": "Cue 019"},
        identity={"column": "PhraseId"},
        column="PhraseId",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "grid row click unavailable"
    assert result["requested"] == {"adapter": "ui.grid.click_row"}


@pytest.mark.asyncio
async def test_ui_operation_adapters_grid_ensure_visible_confirms_after_backend_setup() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.snapshots = [
                {
                    "status": "PASS",
                    "visible_rows": [
                        {
                            "index": 0,
                            "row_index": 18,
                            "cells": {"PhraseId": "Cue 018"},
                        }
                    ],
                },
                {
                    "status": "PASS",
                    "visible_rows": [
                        {
                            "index": 0,
                            "row_index": 42,
                            "cells": {"PhraseId": "Cue 042"},
                        }
                    ],
                },
            ]
            self.ensure_calls: list[dict[str, Any]] = []

        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            snapshot = self.snapshots.pop(0)
            snapshot["selector"] = dict(selector)
            snapshot["rows"] = dict(rows or {})
            snapshot["columns"] = list(columns or [])
            return snapshot

        async def grid_ensure_visible(
            self,
            selector: dict[str, Any],
            *,
            row_key: str,
            identity: dict[str, Any],
            rows: dict[str, Any],
            columns: list[str],
            max_scrolls: int | None = None,
            scroll_settle_ms: int | None = None,
        ) -> dict[str, Any]:
            self.ensure_calls.append(
                {
                    "selector": dict(selector),
                    "row_key": row_key,
                    "identity": dict(identity),
                    "rows": dict(rows),
                    "columns": list(columns),
                    "max_scrolls": max_scrolls,
                    "scroll_settle_ms": scroll_settle_ms,
                }
            )
            return {"status": "PASS", "realized": True}

    backend = FakeBackend()

    async def backend_provider() -> FakeBackend:
        return backend

    result = await ui_operation_adapters(backend_provider)["ui.grid.ensure_visible"](
        selector={"automation_id": "dataGrid"},
        row={"identity": "Cue 042"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        columns=["PhraseId"],
        max_scrolls=11,
        scroll_settle_ms=30,
    )

    assert result["status"] == "PASS"
    assert result["already_visible"] is False
    assert result["resolved_row"] == {
        "index": 0,
        "row_index": 42,
        "identity": "Cue 042",
    }
    assert backend.ensure_calls == [
        {
            "selector": {"automation_id": "dataGrid"},
            "row_key": "Cue 042",
            "identity": {"column": "PhraseId"},
            "rows": {"visible_only": True},
            "columns": ["PhraseId"],
            "max_scrolls": 11,
            "scroll_settle_ms": 30,
        }
    ]


@pytest.mark.asyncio
async def test_ui_operation_adapters_filter_grid_viewport_rows_by_grid_bounds() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 4,
                "visible_rows": [
                    {
                        "index": 0,
                        "bounds": {"x": 10, "y": -50, "width": 120, "height": 25},
                        "cells": {"PhraseId": "Cue 000"},
                    },
                    {
                        "index": 1,
                        "bounds": {"x": 10, "y": 20, "width": 120, "height": 25},
                        "cells": {"PhraseId": "Cue 001"},
                    },
                    {
                        "index": 2,
                        "bounds": {"x": 10, "y": 60, "width": 120, "height": 25},
                        "cells": {"PhraseId": "Cue 002"},
                    },
                    {
                        "index": 3,
                        "bounds": {"x": 10, "y": 170, "width": 120, "height": 25},
                        "cells": {"PhraseId": "Cue 003"},
                    },
                ],
            }

        async def find_element(
            self,
            automation_id: str | None = None,
            name: str | None = None,
            control_type: str | None = None,
            root_id: str | None = None,
            xpath: str | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "found": True,
                "rect": {"x": 0, "y": 0, "width": 160, "height": 120},
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.viewport"](
        selector={"automation_id": "dataGrid"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        expect={},
        phase="before",
        probe_name="cue_viewport",
    )

    assert result["status"] == "PASS"
    snapshot = result["snapshot"]
    assert snapshot["first_visible_index"] == 1
    assert snapshot["last_visible_index"] == 2
    assert snapshot["visible_rows"] == [
        {"index": 1, "identity": "Cue 001", "derived": False},
        {"index": 2, "identity": "Cue 002", "derived": False},
    ]


@pytest.mark.asyncio
async def test_ui_operation_adapters_block_viewport_selected_expectation_without_evidence() -> None:
    class FakeBackend:
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {"index": 0, "selected": False, "cells": {"PhraseId": "Cue 001"}},
                    {"index": 1, "selected": False, "cells": {"PhraseId": "Cue 002"}},
                ],
            }

    async def backend_provider() -> FakeBackend:
        return FakeBackend()

    result = await ui_operation_adapters(backend_provider)["ui.grid.viewport"](
        selector={"automation_id": "dataGrid"},
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        expect={"selected_payload_preserved": True},
        phase="after",
        probe_name="cue_viewport",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "selected row evidence unavailable"
    assert result["requested"] == {
        "selector": {"automation_id": "dataGrid"},
        "probe": "ui.grid.viewport",
    }
    assert result["accepted"]["selected_rows"]
    assert result["next_step"]


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

    result = await _runner(session).run(
        {
            "name": f"restore-{case}",
            "budgets": budgets,
            "actions": actions,
            "assertions": assertions,
            "cleanup": {
                "restore_files": [{"path": str(fixture), "baseline_text": f"baseline-{case}"}]
            },
        }
    )

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

    result = await _runner(session).run(
        {
            "name": "restore-baseline-file",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "cleanup": {"restore_files": [{"path": str(fixture), "baseline_file": str(baseline)}]},
        }
    )

    assert result["status"] == "PASS"
    assert fixture.read_text(encoding="utf-8") == "restored from file"
    assert result["cleanup"]["restored_files"][0]["source"] == "baseline_file"
    assert result["cleanup"]["restored_files"][0]["baseline_file"] == str(baseline.resolve())
    assert "restored from file" not in str(result["compact"]["cleanup"])


@pytest.mark.skipif(os.name != "nt", reason="Windows hidden attributes are NT-only")
@pytest.mark.asyncio
async def test_runner_restores_hidden_windows_file_and_preserves_attribute(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("mutated", encoding="utf-8")
    original_attributes = fixture.stat().st_file_attributes
    hidden_attributes = original_attributes | stat.FILE_ATTRIBUTE_HIDDEN
    _set_windows_file_attributes(fixture, hidden_attributes)

    try:
        result = await _runner(session).run(
            {
                "name": "restore-hidden-file",
                "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
                "cleanup": {"restore_files": [{"path": str(fixture), "baseline_text": "baseline"}]},
            }
        )

        assert result["status"] == "PASS"
        assert fixture.read_text(encoding="utf-8") == "baseline"
        assert fixture.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    finally:
        _set_windows_file_attributes(fixture, original_attributes)


@pytest.mark.asyncio
async def test_runner_retries_transient_restore_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("mutated", encoding="utf-8")
    runner = _runner(session)
    original_restore = runner._restore_file
    attempts = 0

    def flaky_restore(entry: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("fixture is still locked")
        return original_restore(entry)

    monkeypatch.setattr(runner, "_restore_file", flaky_restore)

    result = await runner.run(
        {
            "name": "restore-transient-lock",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "cleanup": {
                "restore_files": [{"path": str(fixture), "baseline_text": "restored after retry"}]
            },
        }
    )

    assert result["status"] == "PASS"
    assert attempts == 2
    assert fixture.read_text(encoding="utf-8") == "restored after retry"
    assert result["cleanup"]["restored_files"][0]["attempts"] == 2


@pytest.mark.asyncio
async def test_runner_accepts_already_matched_file_after_restore_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("baseline", encoding="utf-8")
    original_write_text = Path.write_text

    def fail_fixture_write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.resolve() == fixture.resolve():
            raise PermissionError("fixture is locked for writes")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_fixture_write)

    result = await _runner(session).run(
        {
            "name": "restore-already-matched",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "cleanup": {"restore_files": [{"path": str(fixture), "baseline_text": "baseline"}]},
        }
    )

    assert result["status"] == "PASS"
    assert result["cleanup"]["status"] == "PASS"
    assert result["cleanup"]["restored_files"][0]["already_matched"] is True


@pytest.mark.asyncio
async def test_runner_cleanup_failure_changes_success_to_fail_for_restore_error(
    tmp_path: Path,
) -> None:
    session = FakeRuntimeSmokeSession()
    session.allowed_root = tmp_path
    directory_target = tmp_path / "fixture-dir"
    directory_target.mkdir()

    result = await _runner(session).run(
        {
            "name": "restore-fails",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "cleanup": {
                "restore_files": [{"path": str(directory_target), "baseline_text": "baseline"}]
            },
        }
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "teardown failed"
    assert result["cleanup"]["status"] == "FAIL"
    assert result["cleanup"]["failures"][0]["operation"] == "fixture.restore"
    assert result["cleanup"]["failures"][0]["path"] == str(directory_target.resolve())


@pytest.mark.asyncio
async def test_runner_records_graceful_debug_stop_when_requested() -> None:
    session = FakeRuntimeSmokeSession()

    result = await _runner(session).run(
        {
            "name": "stop-debug",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
            "cleanup": {"stop_debug": "graceful"},
        }
    )

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

    result = await _runner(session).run(
        {
            "name": "unsafe-restore",
            "actions": [{"name": "append_output", "args": {"text": "must not run\n"}}],
            "cleanup": {"restore_files": [{"path": str(outside), "baseline_text": "baseline"}]},
        }
    )

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

    result = await _runner(session).run(
        {
            "name": "missing-baseline",
            "actions": [{"name": "append_output", "args": {"text": "must not run\n"}}],
            "cleanup": {"restore_files": [{"path": str(fixture)}]},
        }
    )

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

    result = await _runner(session).run(
        {
            "name": "invalid-cleanup",
            "cleanup": {
                "restore_files": [{"path": str(fixture)}],
                "stop_debug": "graceful",
                "debug_hygiene": True,
            },
        }
    )

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
        session=cast(Any, session),
        check_session_access=lambda ctx: None,
        resolve_project_root=_noop_resolve_project_root,
    )

    assert "verify_debug_freshness" in mcp.tools
    assert "run_runtime_smoke" in mcp.tools
    assert {
        "runtime_smoke_start",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
        "runtime_smoke_stop",
    }.issubset(mcp.tools)

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
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]
    assert "debug.output_checkpoint" in non_object["data"]["accepted_operation_names"]
    assert non_object["data"]["completed_steps"] == []
    assert failed["data"]["status"] == "FAIL"
    assert failed["data"]["cleanup"]["status"] == "PASS"

    lifecycle = await mcp.tools["runtime_smoke_start"](
        ctx=None,
        plan={"name": "tool-invalid", "actions": "not-a-list"},
    )
    run_id = lifecycle["data"]["run_id"]
    for _ in range(20):
        lifecycle_result = await mcp.tools["runtime_smoke_get_result"](
            ctx=None,
            run_id=run_id,
        )
        if lifecycle_result["data"].get("final"):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("runtime_smoke_get_result did not observe final result")
    lifecycle_tail = await mcp.tools["runtime_smoke_tail_events"](
        ctx=None,
        run_id=run_id,
    )
    lifecycle_stop = await mcp.tools["runtime_smoke_stop"](ctx=None, run_id=run_id)

    assert lifecycle["data"]["status"] == "RUNNING"
    assert lifecycle_result["data"]["status"] == "FAIL"
    assert lifecycle_result["data"]["reason"] == "invalid plan schema"
    assert lifecycle_result["data"]["final"] is True
    assert [event["kind"] for event in lifecycle_tail["data"]["events"]] == [
        "started",
        "completed",
    ]
    assert lifecycle_stop["data"]["run_id"] == run_id
    assert lifecycle_stop["data"]["status"] == lifecycle_result["data"]["status"]

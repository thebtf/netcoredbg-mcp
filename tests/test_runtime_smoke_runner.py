"""Bounded runtime smoke runner contract tests."""

from __future__ import annotations

import ctypes
import os
import stat
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

    adapters = ui_operation_adapters(lambda: None, session=session)
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

    adapters = ui_operation_adapters(lambda: None, session=session)
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
    assert result["route_evidence"]["move_points"] == [
        {"relative_to": "screen", "x": 25, "y": 40}
    ]
    assert result["route_evidence"]["final_pointer"] == {
        "relative_to": "screen",
        "x": 50,
        "y": 80,
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
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]
    assert "debug.output_checkpoint" in non_object["data"]["accepted_operation_names"]
    assert non_object["data"]["completed_steps"] == []
    assert failed["data"]["status"] == "FAIL"
    assert failed["data"]["cleanup"]["status"] == "PASS"

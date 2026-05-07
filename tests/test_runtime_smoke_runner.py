"""Bounded runtime smoke runner contract tests."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
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
        self.failing_action_calls = 0

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

    async def failing_action(self, **_: Any) -> dict[str, Any]:
        self.failing_action_calls += 1
        raise RuntimeError("adapter exploded")

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
        },
    )


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    return None


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
    assert result["cleanup"]["remaining_runtime_smoke_state"]["instrumentation_groups"] == ["leak"]


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
async def test_runtime_smoke_tools_register_freshness_and_runner() -> None:
    mcp = CapturingMCP()
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
    assert invalid["data"]["cleanup"]["status"] == "PASS"
    assert non_object["data"]["status"] == "FAIL"
    assert non_object["data"]["reason"] == "invalid plan schema"
    assert non_object["data"]["validation_errors"] == ["plan must be an object"]
    assert non_object["data"]["completed_steps"] == []
    assert failed["data"]["status"] == "FAIL"
    assert failed["data"]["cleanup"]["status"] == "PASS"

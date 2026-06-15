"""Runtime smoke session lifecycle tests."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from netcoredbg_mcp.session.runtime_smoke import (
    RuntimeSmokeRunner,
    RuntimeSmokeRunRegistry,
    RuntimeSmokeSession,
)
from netcoredbg_mcp.session.state import DebugState, EvidenceRef, OutputEntry


class LifecycleSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            process_id=1234,
            process_name="LifecycleSmoke",
            modules=[],
            loaded_sources={},
        )
        self.release_event = asyncio.Event()
        self.cleanup_calls = 0

    async def append_output(self, text: str = "ready\n") -> dict[str, Any]:
        self.state.output_sequence += 1
        self.state.output_buffer.append(
            OutputEntry(
                text,
                category="stdout",
                sequence=self.state.output_sequence,
            )
        )
        return {"status": "PASS", "reason": "output appended", "text_length": len(text)}

    async def wait_until_released(self) -> dict[str, Any]:
        await self.release_event.wait()
        return {"status": "PASS", "reason": "released"}

    async def clear_group(self, name: str) -> dict[str, Any]:
        self.cleanup_calls += 1
        self.runtime_smoke.instrumentation_groups.pop(name, None)
        return {"status": "PASS", "reason": "instrumentation group cleared"}


def _runner(session: LifecycleSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "append_output": session.append_output,
            "wait_until_released": session.wait_until_released,
            "instrumentation_group_clear": session.clear_group,
        },
    )


async def _wait_for_final(registry: RuntimeSmokeRunRegistry, run_id: str) -> dict[str, Any]:
    for _ in range(20):
        result = await registry.get_result(run_id)
        if result.get("final"):
            return result
        await asyncio.sleep(0)
    raise AssertionError("runtime smoke lifecycle run did not finish")


def test_runtime_smoke_session_reset_clears_owned_state() -> None:
    smoke = RuntimeSmokeSession()
    smoke.instrumentation_groups["group"] = {"breakpoints": [1]}
    smoke.output_checkpoints["start"] = 10
    smoke.evidence_refs.append(EvidenceRef(kind="output", ref="output:1", summary="one line"))

    failures = smoke.reset()

    assert failures == ()
    assert smoke.instrumentation_groups == {}
    assert smoke.output_checkpoints == {}
    assert smoke.evidence_refs == []


def test_runtime_smoke_session_runs_cleanup_callbacks_during_reset() -> None:
    smoke = RuntimeSmokeSession()
    calls: list[str] = []

    smoke.register_cleanup("release-modifier", lambda: calls.append("released"))

    failures = smoke.reset()

    assert failures == ()
    assert calls == ["released"]


def test_runtime_smoke_session_records_cleanup_failure_without_leaking_state() -> None:
    smoke = RuntimeSmokeSession()
    smoke.instrumentation_groups["group"] = {"breakpoints": [1]}

    def fail_cleanup() -> None:
        raise RuntimeError("release failed")

    smoke.register_cleanup("release-modifier", fail_cleanup)

    failures = smoke.reset()

    assert failures == ({"name": "release-modifier", "error": "release failed"},)
    assert smoke.last_reset_failures == failures
    assert smoke.instrumentation_groups == {}
    assert smoke.output_checkpoints == {}


@pytest.mark.asyncio
async def test_session_manager_stop_resets_runtime_smoke_state(mock_netcoredbg_path) -> None:
    from netcoredbg_mcp.session import SessionManager

    manager = SessionManager()
    manager.runtime_smoke.instrumentation_groups["group"] = {"breakpoints": [1]}
    manager.runtime_smoke.output_checkpoints["start"] = 10
    manager._state.state = DebugState.STOPPED

    result = await manager.stop()

    assert result == {"success": True}
    assert manager.state.state == DebugState.IDLE
    assert manager.runtime_smoke.instrumentation_groups == {}
    assert manager.runtime_smoke.output_checkpoints == {}


def test_runtime_smoke_state_does_not_cross_session_managers(mock_netcoredbg_path) -> None:
    from netcoredbg_mcp.session import SessionManager

    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        first = SessionManager()
        second = SessionManager()

    first.runtime_smoke.instrumentation_groups["group"] = {"breakpoints": [1]}

    assert second.runtime_smoke.instrumentation_groups == {}
    assert first.runtime_smoke is not second.runtime_smoke


@pytest.mark.asyncio
async def test_runtime_smoke_start_returns_run_id_and_initial_event() -> None:
    session = LifecycleSmokeSession()

    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "lifecycle-start",
            "actions": [{"name": "wait_until_released"}],
        },
        lambda: _runner(session),
    )

    assert started["status"] == "RUNNING"
    assert started["run_id"]
    assert started["final"] is False

    events = await session.runtime_smoke.lifecycle_runs.tail_events(started["run_id"])
    assert events["events"][0]["kind"] == "started"
    assert events["events"][0]["plan_name"] == "lifecycle-start"

    session.release_event.set()
    final = await _wait_for_final(session.runtime_smoke.lifecycle_runs, started["run_id"])
    assert final["status"] == "PASS"


@pytest.mark.asyncio
async def test_runtime_smoke_tail_events_returns_incremental_events_by_cursor() -> None:
    session = LifecycleSmokeSession()

    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "lifecycle-tail",
            "actions": [{"name": "wait_until_released"}],
        },
        lambda: _runner(session),
    )
    run_id = started["run_id"]

    first_tail = await session.runtime_smoke.lifecycle_runs.tail_events(run_id)
    first_cursor = first_tail["next_cursor"]
    second_tail = await session.runtime_smoke.lifecycle_runs.tail_events(
        run_id,
        after_cursor=first_cursor,
    )
    assert [event["kind"] for event in first_tail["events"]] == ["started"]
    assert second_tail["events"] == []

    session.release_event.set()
    await _wait_for_final(session.runtime_smoke.lifecycle_runs, run_id)
    final_tail = await session.runtime_smoke.lifecycle_runs.tail_events(
        run_id,
        after_cursor=first_cursor,
    )

    assert [event["kind"] for event in final_tail["events"]] == ["completed"]
    assert final_tail["next_cursor"] > first_cursor
    assert final_tail["final"] is True


@pytest.mark.asyncio
async def test_runtime_smoke_get_result_returns_final_runner_envelope() -> None:
    session = LifecycleSmokeSession()

    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "lifecycle-result",
            "actions": [{"name": "append_output", "args": {"text": "ready\n"}}],
        },
        lambda: _runner(session),
    )
    final = await _wait_for_final(session.runtime_smoke.lifecycle_runs, started["run_id"])

    assert final["run_id"] == started["run_id"]
    assert final["status"] == "PASS"
    assert final["reason"] == "runtime smoke scenario passed"
    assert final["elapsed_ms"] >= 0
    assert final["action_count"] == 1
    assert final["cleanup"]["status"] == "PASS"
    assert final["evidence_refs"] == []
    assert final["compact"]["status"] == "PASS"
    assert final["final"] is True


@pytest.mark.asyncio
async def test_runtime_smoke_stop_is_idempotent_and_runs_cleanup() -> None:
    session = LifecycleSmokeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}

    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "lifecycle-stop",
            "actions": [{"name": "wait_until_released"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        },
        lambda: _runner(session),
    )

    stopped = await session.runtime_smoke.lifecycle_runs.stop(started["run_id"])
    stopped_again = await session.runtime_smoke.lifecycle_runs.stop(started["run_id"])

    assert stopped["status"] == "IMPASSE"
    assert stopped["lifecycle_status"] == "STOPPED"
    assert stopped["stopped"] is True
    assert stopped["reason"] == "runtime smoke run stopped"
    assert stopped["cleanup"]["status"] == "PASS"
    assert "instrumentation_group_clear:flow" in stopped["cleanup"]["attempted"]
    assert "runtime_smoke_reset" in stopped["cleanup"]["attempted"]
    assert stopped_again["status"] == stopped["status"]
    assert session.cleanup_calls == 1
    assert session.runtime_smoke.instrumentation_groups == {}
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []


@pytest.mark.asyncio
async def test_runtime_smoke_lifecycle_retention_is_bounded() -> None:
    session = LifecycleSmokeSession()
    session.runtime_smoke.lifecycle_runs = RuntimeSmokeRunRegistry(
        max_runs=2,
        max_events_per_run=1,
    )
    completed: list[str] = []

    for index in range(3):
        started = await session.runtime_smoke.lifecycle_runs.start(
            {
                "name": f"retained-{index}",
                "actions": [{"name": "append_output", "args": {"text": f"{index}\n"}}],
            },
            lambda: _runner(session),
        )
        completed.append(started["run_id"])
        await _wait_for_final(session.runtime_smoke.lifecycle_runs, started["run_id"])

    first = await session.runtime_smoke.lifecycle_runs.get_result(completed[0])
    latest_tail = await session.runtime_smoke.lifecycle_runs.tail_events(completed[-1])

    assert first["status"] == "FAIL"
    assert first["reason"] == "runtime smoke run not found"
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == completed[1:]
    assert len(latest_tail["events"]) == 1
    assert latest_tail["events"][0]["kind"] == "completed"
    assert latest_tail["dropped_count"] == 1
    assert latest_tail["oldest_cursor"] == 2

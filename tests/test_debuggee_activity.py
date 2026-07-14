"""CR-111 bounded debuggee activity telemetry tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from netcoredbg_mcp.dap import DAPClient, DAPEvent
from netcoredbg_mcp.dap.protocol import Events
from netcoredbg_mcp.response import VALID_ACTIONS
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import DebugState, SessionState, TraceEntry
from netcoredbg_mcp.session.tracepoints import TracepointManager
from netcoredbg_mcp.tools.debug import register_debug_tools

INSTRUCTION_COUNTER_UNAVAILABLE = (
    "The current NetCoreDbg/DAP capability surface exposes no executed-instruction counter."
)


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.annotations: dict[str, Any] = {}

    def tool(self, annotations: Any = None):
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            self.annotations[func.__name__] = annotations
            return func

        return decorator


class Bomb:
    def __getattribute__(self, name: str) -> Any:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"unexpected dependency access: {name}")


class BombCollection:
    def __iter__(self):
        raise AssertionError("retained payload was enumerated")

    def __len__(self) -> int:
        raise AssertionError("retained payload length was read")

    def __bool__(self) -> bool:
        raise AssertionError("retained payload truthiness was read")


def make_manager() -> SessionManager:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        return SessionManager()


def event(kind: str, body: dict[str, Any]) -> DAPEvent:
    return DAPEvent(seq=1, event=kind, body=body)


def trace_entry(value: str = "1") -> TraceEntry:
    return TraceEntry(1.0, "Program.cs", 10, "value", value, 1, "tp-1")


def register_activity_tool(
    manager: SessionManager,
    *,
    access_error: str | None = None,
) -> tuple[ToolRegistry, MagicMock]:
    registry = ToolRegistry()
    access = MagicMock(return_value=access_error)
    register_debug_tools(
        registry,
        manager,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=AsyncMock(),
        check_session_access=access,
        execute_and_wait=AsyncMock(),
        resolve_project_root=AsyncMock(),
        resolve_project_root_readonly=AsyncMock(),
    )
    return registry, access


def register_activity_fastmcp(
    manager: SessionManager,
    *,
    access_error: str | None = None,
) -> tuple[FastMCP, MagicMock]:
    mcp = FastMCP("debuggee-activity-test")
    access = MagicMock(return_value=access_error)
    register_debug_tools(
        mcp,
        manager,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=AsyncMock(),
        check_session_access=access,
        execute_and_wait=AsyncMock(),
        resolve_project_root=AsyncMock(),
        resolve_project_root_readonly=AsyncMock(),
    )
    return mcp, access


@pytest.mark.asyncio
async def test_event_handlers_register_once_across_stop_start_cycle() -> None:
    manager = make_manager()
    manager._client = DAPClient("netcoredbg")

    manager._register_event_handlers()
    await manager.stop()
    manager._register_event_handlers()

    handlers = manager.client._event_handlers[Events.CONTINUED]
    assert len(handlers) == 1
    handlers[0](event("continued", {"allThreadsContinued": True}))
    assert manager.state.continued_events == 1


def test_activity_snapshot_uses_authoritative_event_counters() -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING

    manager._on_continued(event("continued", {"allThreadsContinued": True}))
    manager._on_continued(event("continued", {"allThreadsContinued": True}))
    manager._on_stopped(
        event(
            "stopped",
            {"reason": "step", "threadId": 7, "allThreadsStopped": True},
        )
    )

    module = {"id": "m1", "name": "App.dll"}
    manager._on_module(event("module", {"reason": "new", "module": module}))
    manager._on_module(event("module", {"reason": "new", "module": module}))
    manager._on_module(
        event(
            "module",
            {"reason": "changed", "module": {"id": "missing", "name": "x"}},
        )
    )
    manager._on_module(
        event(
            "module",
            {"reason": "removed", "module": {"id": "missing", "name": "x"}},
        )
    )
    manager._on_module(
        event(
            "module",
            {"reason": "unknown", "module": {"id": "ignored", "name": "x"}},
        )
    )

    manager._on_output(event("output", {"category": "stdout", "output": "one\n"}))
    manager.state.output_buffer.clear()
    manager._on_output(event("output", {"category": "stdout", "output": "two\n"}))

    snapshot = manager.activity_snapshot()

    assert snapshot.continued_events == 2
    assert snapshot.stopped_events == 1
    assert snapshot.step_stops == 1
    assert snapshot.output_events == 2
    assert snapshot.module_new_events == 2
    assert snapshot.module_changed_events == 1
    assert snapshot.module_removed_events == 1
    assert len(manager.state.modules) == 1


@pytest.mark.asyncio
async def test_step_request_does_not_count_until_stopped_step_event() -> None:
    manager = make_manager()
    manager.state.state = DebugState.STOPPED
    manager.state.current_thread_id = 7
    manager.client.step_over = AsyncMock(return_value=SimpleNamespace(success=True))

    before = manager.activity_snapshot()
    await manager.step_over()
    after_request = manager.activity_snapshot()

    assert after_request.stopped_events == before.stopped_events
    assert after_request.step_stops == before.step_stops

    manager._on_stopped(event("stopped", {"reason": "step", "threadId": 7}))
    after_event = manager.activity_snapshot()
    assert after_event.stopped_events == before.stopped_events + 1
    assert after_event.step_stops == before.step_stops + 1


@pytest.mark.asyncio
async def test_transparent_tracepoint_stop_is_counted_before_filtering() -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager._tracepoint_manager = TracepointManager()
    manager._update_hit_count = AsyncMock()  # type: ignore[method-assign]
    manager._check_tracepoint = AsyncMock()  # type: ignore[method-assign]

    manager._on_stopped(event("stopped", {"reason": "breakpoint", "threadId": 7}))
    await asyncio.sleep(0)

    assert manager.activity_snapshot().stopped_events == 1
    manager._check_tracepoint.assert_awaited_once_with(7)  # type: ignore[attr-defined]


def test_output_sequence_survives_buffer_trimming() -> None:
    manager = make_manager()
    with patch("netcoredbg_mcp.session.manager.MAX_OUTPUT_BYTES", 1):
        manager._on_output(event("output", {"category": "stdout", "output": "first"}))
        manager._on_output(event("output", {"category": "stdout", "output": "second"}))

    assert list(manager.state.output_buffer) == []
    assert manager.activity_snapshot().output_events == 2


def test_trace_generation_survives_clear_and_state_replacement() -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    tracepoints = TracepointManager()
    manager._tracepoint_manager = tracepoints

    tracepoints._trace_buffer.append(trace_entry("before-clear"))
    first = manager.activity_snapshot()
    tracepoints.clear_log()
    tracepoints._trace_buffer.append(trace_entry("after-clear"))
    second = manager.activity_snapshot()

    assert tracepoints.append_generation == 2
    assert first.trace_entries == 1
    assert second.trace_entries == 2
    assert first.epoch is second.epoch

    manager._state = SessionState(state=DebugState.RUNNING)
    replaced = manager.activity_snapshot()

    assert replaced.epoch is not second.epoch
    assert replaced.continued_events == 0
    assert replaced.stopped_events == 0
    assert replaced.step_stops == 0
    assert replaced.output_events == 0
    assert replaced.module_new_events == 0
    assert replaced.module_changed_events == 0
    assert replaced.module_removed_events == 0
    assert replaced.trace_entries == 2


def test_activity_snapshot_does_not_enumerate_retained_payloads() -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager.state.output_buffer = BombCollection()  # type: ignore[assignment]
    manager.state.modules = BombCollection()  # type: ignore[assignment]
    manager.state.threads = BombCollection()  # type: ignore[assignment]
    manager.state.loaded_sources = BombCollection()  # type: ignore[assignment]
    manager.state.active_progress = BombCollection()  # type: ignore[assignment]

    snapshot = manager.activity_snapshot()

    assert snapshot.state == DebugState.RUNNING
    assert snapshot.output_events == 0
    assert snapshot.trace_entries == 0


@pytest.mark.asyncio
async def test_debuggee_activity_returns_fixed_same_epoch_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager.state.process_id = 4242
    manager._tracepoint_manager = TracepointManager()
    registry, access = register_activity_tool(manager)

    async def fake_sleep(delay: float) -> None:
        assert delay == 0.001
        manager._on_continued(event("continued", {"allThreadsContinued": True}))
        manager._on_output(event("output", {"category": "stdout", "output": "tick\n"}))
        manager._on_module(
            event(
                "module",
                {"reason": "new", "module": {"id": "m1", "name": "App.dll"}},
            )
        )
        manager._tracepoint_manager._trace_buffer.append(trace_entry())
        manager._on_stopped(event("stopped", {"reason": "step", "threadId": 7}))

    sleep = AsyncMock(side_effect=fake_sleep)
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=1)

    access.assert_called_once()
    sleep.assert_awaited_once_with(0.001)
    assert result["state"] == "stopped"
    assert result["data"]["windowMs"] == 1
    assert result["data"]["startedAt"] <= result["data"]["endedAt"]
    assert result["data"]["elapsedMs"] >= 0
    assert result["data"]["start"] == {
        "state": "running",
        "execState": "running",
        "stopReason": None,
        "debuggeePid": 4242,
    }
    assert result["data"]["end"] == {
        "state": "stopped",
        "execState": "stepping",
        "stopReason": "step",
        "debuggeePid": 4242,
    }
    assert result["data"]["deltas"] == {
        "continuedEvents": 1,
        "stoppedEvents": 1,
        "stepStops": 1,
        "outputEvents": 1,
        "moduleEvents": {"total": 1, "new": 1, "changed": 0, "removed": 0},
        "traceEntries": 1,
    }
    assert result["data"]["observedActivity"] is True
    assert result["data"]["activitySignals"] == [
        "continuedEvents",
        "stoppedEvents",
        "stepStops",
        "outputEvents",
        "moduleEvents.new",
        "traceEntries",
    ]
    assert result["data"]["instructionsExecuted"] == {
        "available": False,
        "reason": INSTRUCTION_COUNTER_UNAVAILABLE,
    }
    assert {"get_debug_state", "get_output"} <= set(result["next_actions"])
    assert registry.annotations["debuggee_activity"].model_dump(exclude_none=True) == {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert "debuggee_activity" in VALID_ACTIONS[DebugState.RUNNING.value]


@pytest.mark.asyncio
@pytest.mark.parametrize("window_ms", [True, False, 0, 30001, 1.5, "1000", None])
async def test_debuggee_activity_rejects_invalid_window_before_snapshot_or_sleep(
    monkeypatch: pytest.MonkeyPatch,
    window_ms: object,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager.activity_snapshot = MagicMock()  # type: ignore[method-assign]
    registry, _ = register_activity_tool(manager)
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=window_ms)

    assert "window_ms must be an integer from 1 to 30000" in result["error"]
    manager.activity_snapshot.assert_not_called()  # type: ignore[attr-defined]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_fastmcp_boundary_preserves_access_first_exact_integer_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    mcp, access = register_activity_fastmcp(manager, access_error="denied")
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    denied = await mcp._tool_manager.call_tool(
        "debuggee_activity",
        {"window_ms": "not-an-integer"},
        context=SimpleNamespace(),
        convert_result=False,
    )
    assert denied["error"] == "denied"

    access.return_value = None
    rejected = await mcp._tool_manager.call_tool(
        "debuggee_activity",
        {"window_ms": "1000"},
        context=SimpleNamespace(),
        convert_result=False,
    )
    assert rejected["error"] == "window_ms must be an integer from 1 to 30000"
    sleep.assert_not_awaited()

    tool = {tool.name: tool for tool in await mcp.list_tools()}["debuggee_activity"]
    assert tool.inputSchema["properties"]["window_ms"] == {
        "default": 1000,
        "maximum": 30000,
        "minimum": 1,
        "title": "Window Ms",
        "type": "integer",
    }


@pytest.mark.asyncio
async def test_debuggee_activity_checks_access_before_window_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager.activity_snapshot = MagicMock()  # type: ignore[method-assign]
    registry, access = register_activity_tool(manager, access_error="denied")
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=True)

    assert result["error"] == "denied"
    access.assert_called_once()
    manager.activity_snapshot.assert_not_called()  # type: ignore[attr-defined]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_debuggee_activity_requires_initial_running_before_snapshot_or_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.STOPPED
    manager.activity_snapshot = MagicMock()  # type: ignore[method-assign]
    registry, _ = register_activity_tool(manager)
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    result = await registry.tools["debuggee_activity"](SimpleNamespace())

    assert result["error"] == "debuggee_activity requires state RUNNING"
    manager.activity_snapshot.assert_not_called()  # type: ignore[attr-defined]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_state", [DebugState.RUNNING, DebugState.IDLE])
async def test_debuggee_activity_rejects_lifecycle_replacement(
    monkeypatch: pytest.MonkeyPatch,
    replacement_state: DebugState,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    registry, _ = register_activity_tool(manager)

    async def replace_session(_: float) -> None:
        if replacement_state == DebugState.RUNNING:
            manager._state = SessionState(state=DebugState.RUNNING)
        else:
            manager.state.state = DebugState.IDLE

    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", replace_session)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=1)

    assert result["error"] == "debug session changed during activity window"
    assert "data" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["launch", "attach"])
async def test_debuggee_activity_rejects_terminated_to_new_session_overlap(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager._client.is_running = True
    manager._initialized_event.set()
    manager._enable_hot_reload_if_supported = AsyncMock()  # type: ignore[method-assign]
    manager._sync_all_breakpoints = AsyncMock()  # type: ignore[method-assign]
    manager._client.set_exception_breakpoints = AsyncMock()
    manager._client.launch = AsyncMock(return_value=SimpleNamespace(success=True))
    manager._client.attach = AsyncMock(return_value=SimpleNamespace(success=True))
    manager._client.configuration_done = AsyncMock()
    manager.check_dbgshim_compatibility = MagicMock(return_value=None)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "netcoredbg_mcp.setup.dbgshim.select_and_swap_dbgshim",
        MagicMock(return_value=False),
    )
    registry, _ = register_activity_tool(manager)

    async def replace_debuggee(_: float) -> None:
        manager._on_terminated(event("terminated", {}))
        if route == "launch":
            await manager.launch("Program.dll")
        else:
            await manager.attach(4242)

    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", replace_debuggee)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=1)

    assert result["error"] == "debug session changed during activity window"
    assert "data" not in result


@pytest.mark.asyncio
async def test_debuggee_activity_cancellation_reraises_without_t1_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager.state.output_sequence = 3
    original_snapshot = manager.activity_snapshot
    manager.activity_snapshot = MagicMock(wraps=original_snapshot)  # type: ignore[method-assign]
    registry, _ = register_activity_tool(manager)
    entered_sleep = False

    async def cancel(_: float) -> None:
        nonlocal entered_sleep
        entered_sleep = True
        raise asyncio.CancelledError

    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", cancel)

    with pytest.raises(asyncio.CancelledError):
        await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=1)

    assert entered_sleep is True
    assert manager.activity_snapshot.call_count == 1  # type: ignore[attr-defined]
    assert manager.state.output_sequence == 3


@pytest.mark.asyncio
async def test_debuggee_activity_probe_path_is_passive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager()
    manager.state.state = DebugState.RUNNING
    manager._client = Bomb()  # type: ignore[assignment]
    manager._process_registry = Bomb()  # type: ignore[assignment]
    manager.state.output_buffer = BombCollection()  # type: ignore[assignment]
    manager.state.modules = BombCollection()  # type: ignore[assignment]
    manager.state.threads = BombCollection()  # type: ignore[assignment]
    registry, _ = register_activity_tool(manager)
    sleep = AsyncMock()
    monkeypatch.setattr("netcoredbg_mcp.tools.debug.asyncio.sleep", sleep)

    result = await registry.tools["debuggee_activity"](SimpleNamespace(), window_ms=1)

    sleep.assert_awaited_once_with(0.001)
    assert result["data"]["observedActivity"] is False
    assert result["data"]["activitySignals"] == []
    assert result["data"]["deltas"] == {
        "continuedEvents": 0,
        "stoppedEvents": 0,
        "stepStops": 0,
        "outputEvents": 0,
        "moduleEvents": {"total": 0, "new": 0, "changed": 0, "removed": 0},
        "traceEntries": 0,
    }

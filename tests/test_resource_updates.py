from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.shared.exceptions import McpError

from netcoredbg_mcp.dap.protocol import DAPEvent
from netcoredbg_mcp.resource_updates import (
    BREAKPOINTS_URI,
    OUTPUT_URI,
    STATE_URI,
    THREADS_URI,
    ResourceSubscriptions,
    apply_subscribe_capability,
)
from netcoredbg_mcp.session.state import Breakpoint, DebugState, OutputEntry, ThreadInfo
from netcoredbg_mcp.tools.debug import register_debug_tools


class RecordingSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.block = False

    async def send_resource_updated(self, uri) -> None:
        self.send_started.set()
        if self.block:
            await self.release_send.wait()
        self.sent.append(str(uri))


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.mark.asyncio
async def test_subscription_authority_is_idempotent_ordered_and_stops_after_unsubscribe() -> None:
    tokens = {uri: 0 for uri in (STATE_URI, BREAKPOINTS_URI, OUTPUT_URI, THREADS_URI)}
    subscriptions = ResourceSubscriptions(tokens.__getitem__)
    session = RecordingSession()

    with pytest.raises(McpError, match="Unknown resource") as unknown:
        await subscriptions.subscribe("debug://unknown", session)  # type: ignore[arg-type]
    assert unknown.value.error.code == -32602

    await subscriptions.subscribe(STATE_URI, session)  # type: ignore[arg-type]
    await subscriptions.subscribe(STATE_URI, session)  # duplicate is a no-op
    tokens[STATE_URI] += 1
    await subscriptions.notify((STATE_URI, STATE_URI))
    await subscriptions.notify((STATE_URI,))
    assert session.sent == [STATE_URI]

    session.block = True
    tokens[STATE_URI] += 1
    in_flight = asyncio.create_task(subscriptions.notify((STATE_URI,)))
    await session.send_started.wait()
    unsubscribe = asyncio.create_task(subscriptions.unsubscribe(STATE_URI))
    await asyncio.sleep(0)
    assert not unsubscribe.done()

    session.release_send.set()
    await in_flight
    await unsubscribe
    tokens[STATE_URI] += 1
    await subscriptions.notify((STATE_URI,))
    assert session.sent == [STATE_URI, STATE_URI]


@pytest.mark.asyncio
async def test_session_manager_publishes_async_dap_resource_mutations() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    published: list[tuple[str, ...]] = []

    async def record(uris: tuple[str, ...]) -> None:
        published.append(uris)

    manager.set_resource_update_callback(record)
    manager._on_output(
        DAPEvent(
            seq=1,
            event="output",
            body={"category": "stdout", "output": "hello\n"},
        )
    )
    manager._on_thread(
        DAPEvent(seq=2, event="thread", body={"reason": "started", "threadId": 7})
    )
    manager._on_process(
        DAPEvent(
            seq=3,
            event="process",
            body={"name": "app", "systemProcessId": None, "isLocalProcess": True},
        )
    )
    manager.breakpoints.add(Breakpoint(file="test.cs", line=10, verified=False, id=42))
    manager._on_breakpoint(
        DAPEvent(
            seq=4,
            event="breakpoint",
            body={
                "reason": "changed",
                "breakpoint": {"id": 42, "verified": True, "line": 10},
            },
        )
    )
    manager._on_terminated(DAPEvent(seq=5, event="terminated", body={}))

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    flat = [uri for batch in published for uri in batch]
    assert OUTPUT_URI in flat
    assert THREADS_URI in flat
    assert STATE_URI in flat
    assert BREAKPOINTS_URI in flat
    assert manager.state.state == DebugState.TERMINATED

    count = len(published)
    await manager.close_resource_update_notifications()
    manager._on_output(
        DAPEvent(seq=6, event="output", body={"category": "stdout", "output": "late\n"})
    )
    await asyncio.sleep(0)
    assert len(published) == count


@pytest.mark.asyncio
async def test_blocked_subscriber_coalesces_to_one_live_task_per_uri() -> None:
    """1000 blocked updates must not retain 1000 Python delivery tasks."""
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    gate = asyncio.Event()
    started = asyncio.Event()
    deliveries = 0

    async def slow(uris: tuple[str, ...]) -> None:
        nonlocal deliveries
        deliveries += 1
        started.set()
        await gate.wait()

    manager.set_resource_update_callback(slow)
    manager._publish_resource_updates(STATE_URI)
    await started.wait()
    assert manager.resource_update_live_task_count() == 1

    for _ in range(1000):
        manager._publish_resource_updates(STATE_URI)

    assert manager.resource_update_live_task_count() == 1
    assert manager.resource_update_revision(STATE_URI) == 1001

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Initial delivery + one coalesced catch-up for the latest revision.
    assert deliveries == 2
    assert manager.resource_update_live_task_count() == 0

    await manager.close_resource_update_notifications()
    assert manager.resource_update_live_task_count() == 0


@pytest.mark.asyncio
async def test_get_threads_refresh_publishes_state_and_threads_resources() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    manager._client = SimpleNamespace(
        threads=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                body={"threads": [{"id": 7, "name": "Main"}]},
            )
        )
    )
    published: list[tuple[str, ...]] = []

    async def record(uris: tuple[str, ...]) -> None:
        published.append(uris)

    manager.set_resource_update_callback(record)

    await manager.get_threads()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(published) == 2
    assert {uris for uris in published} == {(STATE_URI,), (THREADS_URI,)}

    published.clear()
    await manager.get_threads()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert published == []
    await manager.close_resource_update_notifications()


@pytest.mark.asyncio
async def test_line_breakpoint_mutations_publish_one_final_resource_snapshot() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    published: list[tuple[str, ...]] = []

    async def record(uris: tuple[str, ...]) -> None:
        published.append(uris)

    async def sync_file(_file: str, breakpoints: list[dict]) -> SimpleNamespace:
        return SimpleNamespace(
            success=True,
            body={
                "breakpoints": [
                    {
                        "id": index,
                        "verified": True,
                        "line": breakpoint["line"] + 2,
                    }
                    for index, breakpoint in enumerate(breakpoints, start=1)
                ]
            },
        )

    async def flush_updates() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    manager.set_resource_update_callback(record)
    manager.state.state = DebugState.RUNNING
    manager._client.set_breakpoints = AsyncMock(side_effect=sync_file)

    await manager.add_breakpoint("test.cs", 10)
    await flush_updates()
    assert published == [(BREAKPOINTS_URI,)]
    breakpoint = manager.breakpoints.get_for_file("test.cs")[0]
    assert (breakpoint.line, breakpoint.dap_line, breakpoint.condition, breakpoint.verified) == (
        10,
        12,
        None,
        True,
    )
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 1

    published.clear()
    await manager.add_breakpoint("test.cs", 10)
    assert await manager.remove_breakpoint("test.cs", 999) is False
    await flush_updates()
    assert published == []
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 1

    await manager.add_breakpoint("test.cs", 10, condition="i > 0")
    await flush_updates()
    assert published == [(BREAKPOINTS_URI,)]
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 2

    published.clear()

    assert await manager.remove_breakpoint("test.cs", 10) is True
    await flush_updates()
    assert published == [(BREAKPOINTS_URI,)]
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 3

    published.clear()
    await manager.add_breakpoint("test.cs", 10)
    await manager.add_breakpoint("test.cs", 20, condition="i > 0")
    await flush_updates()
    published.clear()
    assert await manager.clear_breakpoints("test.cs") == 2
    await flush_updates()
    assert published == [(BREAKPOINTS_URI,)]
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 6


@pytest.mark.asyncio
async def test_sync_all_publishes_visible_line_changes_but_not_function_or_id_only_changes(
) -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    published: list[tuple[str, ...]] = []

    async def record(uris: tuple[str, ...]) -> None:
        published.append(uris)

    response_id = 42

    async def sync_file(_file: str, _breakpoints: list[dict]) -> SimpleNamespace:
        return SimpleNamespace(
            success=True,
            body={
                "breakpoints": [
                    {"id": response_id, "verified": True, "line": 12}
                ]
            },
        )

    async def flush_updates() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    manager.set_resource_update_callback(record)
    manager._client.capabilities = {}
    manager._client.set_breakpoints = AsyncMock(side_effect=sync_file)
    manager.breakpoints.add(Breakpoint(file="test.cs", line=10))

    await manager._sync_all_breakpoints()
    await flush_updates()
    assert published == [(BREAKPOINTS_URI,)]
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 1

    published.clear()
    response_id = 99
    await manager._sync_all_breakpoints()
    await flush_updates()
    assert published == []
    assert manager.breakpoints.get_for_file("test.cs")[0].id == 99
    assert manager.resource_update_revision(BREAKPOINTS_URI) == 1

    before = manager._breakpoint_resource_snapshot()
    await manager.add_function_breakpoint("Program.Main")
    await flush_updates()
    assert manager._breakpoint_resource_snapshot() == before
    assert published == []


@pytest.mark.asyncio
async def test_session_manager_publishes_every_serialized_state_mutation() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager

        manager = SessionManager()

    published: list[tuple[str, ...]] = []

    async def record(uris: tuple[str, ...]) -> None:
        published.append(uris)

    manager.set_resource_update_callback(record)
    manager.state.threads = [ThreadInfo(id=7, name="worker")]
    manager.state.current_thread_id = 7
    manager.state.current_frame_id = 9
    manager._on_thread(
        DAPEvent(seq=1, event="thread", body={"reason": "exited", "threadId": 7})
    )
    manager._on_invalidated(
        DAPEvent(seq=2, event="invalidated", body={"areas": ["variables"]})
    )
    manager._on_loaded_source(
        DAPEvent(
            seq=3,
            event="loadedSource",
            body={"reason": "new", "source": {"path": "generated.cs"}},
        )
    )
    manager._on_progress_start(
        DAPEvent(
            seq=4,
            event="progressStart",
            body={"progressId": "p1", "title": "Loading"},
        )
    )
    manager._on_progress_update(
        DAPEvent(
            seq=5,
            event="progressUpdate",
            body={"progressId": "p1", "percentage": 50},
        )
    )
    manager._on_progress_end(
        DAPEvent(seq=6, event="progressEnd", body={"progressId": "p1"})
    )
    manager._on_memory(
        DAPEvent(
            seq=7,
            event="memory",
            body={"memoryReference": "0x10", "offset": 0, "count": 4},
        )
    )
    manager._on_module(
        DAPEvent(
            seq=8,
            event="module",
            body={"reason": "new", "module": {"id": 1, "name": "app.dll"}},
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Burst mutations coalesce to one live task per URI, but each mutation still
    # advances the revision so a later delivery observes the latest snapshot.
    assert manager.resource_update_revision(STATE_URI) >= 8
    assert any(STATE_URI in uris for uris in published)
    assert any(THREADS_URI in uris for uris in published)
    assert manager.resource_update_live_task_count() == 0

    published.clear()
    manager.state.output_buffer.append(
        OutputEntry(text="captured", category="stdout", sequence=1)
    )
    manager.state.output_buffer.clear()
    await asyncio.sleep(0)
    assert published == [(OUTPUT_URI,)]


@pytest.mark.asyncio
async def test_terminate_fallback_notifies_state_threads_and_output_once() -> None:
    registry = ToolRegistry()
    session = SimpleNamespace(
        client=SimpleNamespace(capabilities={}),
        stop=AsyncMock(return_value={"success": True}),
        state=SimpleNamespace(state=DebugState.IDLE),
    )
    calls: list[str] = []

    async def notify_state(_ctx) -> None:
        calls.append(STATE_URI)

    async def notify_threads(_ctx) -> None:
        calls.append(THREADS_URI)

    async def notify_output(_ctx) -> None:
        calls.append(OUTPUT_URI)

    register_debug_tools(
        registry,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        ownership=SimpleNamespace(release=lambda: None),
        notify_state_changed=notify_state,
        notify_threads_changed=notify_threads,
        notify_output_changed=notify_output,
        check_session_access=lambda _ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=AsyncMock(),
    )

    result = await registry.tools["terminate_debug"](ctx=object())  # type: ignore[operator]

    session.stop.assert_awaited_once()
    assert result["data"]["state"] == DebugState.IDLE.value
    assert calls == [STATE_URI, THREADS_URI, OUTPUT_URI]

@pytest.mark.asyncio
async def test_attach_tool_notifies_state_threads_and_output_once() -> None:
    registry = ToolRegistry()
    session = SimpleNamespace(
        attach=AsyncMock(return_value={"success": True, "processId": 123}),
        state=SimpleNamespace(state=DebugState.RUNNING),
    )
    calls: list[str] = []

    async def notify_state(_ctx) -> None:
        calls.append(STATE_URI)

    async def notify_threads(_ctx) -> None:
        calls.append(THREADS_URI)

    async def notify_output(_ctx) -> None:
        calls.append(OUTPUT_URI)

    register_debug_tools(
        registry,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        ownership=SimpleNamespace(release=lambda: None),
        notify_state_changed=notify_state,
        notify_threads_changed=notify_threads,
        notify_output_changed=notify_output,
        check_session_access=lambda _ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=AsyncMock(),
    )

    result = await registry.tools["attach_debug"](ctx=object(), process_id=123)  # type: ignore[operator]

    assert result["data"]["success"] is True
    assert calls == [STATE_URI, THREADS_URI, OUTPUT_URI]


def test_apply_subscribe_capability_keeps_list_changed_static() -> None:
    from mcp.types import ResourcesCapability, ServerCapabilities

    capabilities = ServerCapabilities(
        resources=ResourcesCapability(subscribe=False, listChanged=False)
    )
    apply_subscribe_capability(capabilities)

    assert capabilities.resources is not None
    assert capabilities.resources.subscribe is True
    assert capabilities.resources.listChanged is False

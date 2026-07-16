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
from netcoredbg_mcp.session.state import Breakpoint, DebugState
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

    assert (OUTPUT_URI,) in published
    assert (THREADS_URI,) in published
    assert (STATE_URI,) in published
    assert (BREAKPOINTS_URI,) in published
    assert (STATE_URI, THREADS_URI) in published
    assert manager.state.state == DebugState.TERMINATED

    count = len(published)
    await manager.close_resource_update_notifications()
    manager._on_output(
        DAPEvent(seq=6, event="output", body={"category": "stdout", "output": "late\n"})
    )
    await asyncio.sleep(0)
    assert len(published) == count


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

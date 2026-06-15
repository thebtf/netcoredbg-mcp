"""Semantic UI monitor API contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.ui_evidence import register_ui_evidence_tools
from netcoredbg_mcp.ui.events import UIEventBufferStore


class FakeMonitorBackend:
    def __init__(self) -> None:
        self.process_id = 42
        self.calls: list[dict[str, Any]] = []
        self.active_queries = 0
        self.max_active_queries = 0
        self.cancel_next = False
        self.delay_s = 0.0
        self.ignore_cancellation_s = 0.0
        self.last_response: dict[str, Any] = {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A", "focus": False}],
            "element_count": 1,
        }
        self.responses: list[dict[str, Any]] = [
            dict(self.last_response)
        ]

    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        self.active_queries += 1
        self.max_active_queries = max(self.max_active_queries, self.active_queries)
        try:
            if self.cancel_next:
                self.cancel_next = False
                raise asyncio.CancelledError()
            if self.delay_s > 0:
                try:
                    await asyncio.sleep(self.delay_s)
                except asyncio.CancelledError:
                    if self.ignore_cancellation_s <= 0:
                        raise
                    await asyncio.sleep(self.ignore_cancellation_s)
            self.calls.append(
                {
                    "selector": dict(selector),
                    "fields": list(fields),
                    "max_results": max_results,
                }
            )
            if self.responses:
                self.last_response = self.responses.pop(0)
            return dict(self.last_response)
        finally:
            self.active_queries -= 1


class FakeUiSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            output_buffer=deque(),
        )
        self.process_registry = None


@pytest.mark.asyncio
async def test_ui_monitor_store_returns_cursor_filtered_events_and_stale_metadata() -> None:
    backend = FakeMonitorBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A", "focus": False}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B", "focus": False}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "C", "focus": True}],
            "element_count": 1,
        },
    ]
    store = UIEventBufferStore()

    started = await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text", "focus"],
        max_events=1,
    )
    first = await store.monitor_poll("flow", after_cursor=0)
    second = await store.monitor_poll("flow", after_cursor=0)
    history = store.monitor_events("flow", after_cursor=0)

    assert started["status"] == "PASS"
    assert started["monitor_id"] == "flow"
    assert started["source"] == "polling"
    assert started["cursor"] == 0
    assert started["next_cursor"] == 0
    assert started["baseline_count"] == 1
    assert first["events"][0]["sequence"] == 1
    assert first["events"][0]["changes"]["text"] == {"before": "A", "after": "B"}
    assert first["cursor"] == 0
    assert first["next_cursor"] == 1
    assert second["stale_cursor"] is True
    assert second["oldest_cursor"] == 2
    assert second["next_cursor"] == 2
    assert second["dropped_count"] == 1
    assert second["evidence_refs"] == [
        {
            "kind": "ui_monitor",
            "ref": "ui_monitor:flow",
            "summary": "events=1 dropped=1",
        }
    ]
    assert [event["sequence"] for event in history["events"]] == [2]
    history["events"][0]["changes"]["focus"]["after"] = False
    fresh_history = store.monitor_events("flow", after_cursor=0)
    assert fresh_history["events"][0]["changes"]["focus"]["after"] is True


@pytest.mark.asyncio
async def test_ui_monitor_wait_returns_event_or_bounded_timeout() -> None:
    backend = FakeMonitorBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B"}],
            "element_count": 1,
        },
    ]
    store = UIEventBufferStore()

    await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text"],
    )
    changed = await store.monitor_wait(
        "flow",
        after_cursor=0,
        timeout_ms=100,
        poll_interval_ms=1,
    )
    timed_out = await store.monitor_wait(
        "flow",
        after_cursor=changed["next_cursor"],
        timeout_ms=1,
        poll_interval_ms=1,
    )

    assert changed["status"] == "PASS"
    assert changed["events"][0]["changes"]["text"] == {"before": "A", "after": "B"}
    assert timed_out["status"] == "BLOCKED"
    assert timed_out["reason"] == "ui monitor wait timed out"
    assert timed_out["next_step"] == "Poll again with ui_monitor_poll or increase timeout_ms."


@pytest.mark.asyncio
async def test_ui_monitor_wait_bounds_slow_backend_poll() -> None:
    backend = FakeMonitorBackend()
    store = UIEventBufferStore()

    await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text"],
    )
    backend.delay_s = 0.5

    result = await asyncio.wait_for(
        store.monitor_wait(
            "flow",
            after_cursor=0,
            timeout_ms=10,
            poll_interval_ms=1,
        ),
        timeout=0.2,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ui monitor wait timed out"
    assert result["event_count"] == 0


@pytest.mark.asyncio
async def test_ui_monitor_wait_handles_cancelled_query_task() -> None:
    backend = FakeMonitorBackend()
    store = UIEventBufferStore()

    await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text"],
    )
    backend.cancel_next = True

    result = await store.monitor_wait(
        "flow",
        after_cursor=0,
        timeout_ms=100,
        poll_interval_ms=1,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ui monitor wait timed out"
    assert result["event_count"] == 0


@pytest.mark.asyncio
async def test_ui_monitor_wait_does_not_wait_for_cancellation_hostile_backend() -> None:
    backend = FakeMonitorBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B"}],
            "element_count": 1,
        },
    ]
    store = UIEventBufferStore()

    await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text"],
    )
    backend.delay_s = 0.5
    backend.ignore_cancellation_s = 0.05

    result = await asyncio.wait_for(
        store.monitor_wait(
            "flow",
            after_cursor=0,
            timeout_ms=10,
            poll_interval_ms=1,
        ),
        timeout=0.2,
    )
    await asyncio.sleep(backend.ignore_cancellation_s + 0.01)
    history = store.monitor_events("flow", after_cursor=0)

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ui monitor wait timed out"
    assert history["event_count"] == 0
    assert history["next_cursor"] == 0


@pytest.mark.asyncio
async def test_ui_monitor_poll_serializes_concurrent_state_updates() -> None:
    backend = FakeMonitorBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "C"}],
            "element_count": 1,
        },
    ]
    store = UIEventBufferStore()

    await store.monitor_start(
        backend,
        monitor_id="flow",
        selector={"automation_id": "grid"},
        fields=["text"],
    )
    backend.delay_s = 0.02

    await asyncio.gather(
        store.monitor_poll("flow", after_cursor=0),
        store.monitor_poll("flow", after_cursor=0),
    )
    history = store.monitor_events("flow", after_cursor=0)

    assert backend.max_active_queries == 1
    assert [event["sequence"] for event in history["events"]] == [1, 2]


@pytest.mark.asyncio
async def test_ui_monitor_tools_wrap_existing_access_and_response_contract(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    backend = FakeMonitorBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B"}],
            "element_count": 1,
        },
    ]

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    started = await capturing_mcp.tools["ui_monitor_start"](
        ctx=None,
        monitor_id="flow",
        fields=["text"],
        automation_id="grid",
    )
    polled = await capturing_mcp.tools["ui_monitor_poll"](
        ctx=None,
        monitor_id="flow",
        after_cursor=0,
    )
    history = await capturing_mcp.tools["ui_monitor_events"](
        ctx=None,
        monitor_id="flow",
        after_cursor=0,
    )

    assert started["data"]["status"] == "PASS"
    assert started["data"]["monitor_id"] == "flow"
    assert polled["data"]["events"][0]["changes"]["text"] == {"before": "A", "after": "B"}
    assert history["data"]["next_cursor"] == polled["data"]["next_cursor"]


@pytest.mark.asyncio
async def test_ui_monitor_poll_reconnects_backend_before_query(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    backend = FakeMonitorBackend()
    backend.process_id = None
    backend.responses = [
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "A"}],
            "element_count": 1,
        },
        {
            "status": "PASS",
            "elements": [{"element_id": "row-1", "text": "B"}],
            "element_count": 1,
        },
    ]
    connect_calls: list[int] = []

    async def connect_backend(backend_arg: FakeMonitorBackend, process_id: int, **_kwargs) -> None:
        connect_calls.append(process_id)
        backend_arg.process_id = process_id

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    monkeypatch.setattr("netcoredbg_mcp.ui.backend.connect_backend", connect_backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    await capturing_mcp.tools["ui_monitor_start"](
        ctx=None,
        monitor_id="flow",
        fields=["text"],
        automation_id="grid",
    )
    backend.process_id = None
    polled = await capturing_mcp.tools["ui_monitor_poll"](
        ctx=None,
        monitor_id="flow",
        after_cursor=0,
    )

    assert connect_calls == [42, 42]
    assert polled["data"]["events"][0]["changes"]["text"] == {"before": "A", "after": "B"}

"""Bounded selector-scoped UI event evidence via polling snapshots."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .snapshots import UISnapshotStore, diff_ui_snapshots, query_ui_fields


@dataclass
class UIEventBuffer:
    backend: Any
    selector: dict[str, Any]
    fields: list[str]
    max_events: int
    baseline: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped_count: int = 0
    next_sequence: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False, repr=False)

    @property
    def oldest_cursor(self) -> int:
        if self.events:
            return int(self.events[0]["sequence"])
        return self.next_sequence


@dataclass
class UIEventBufferStore:
    """Session-scoped bounded UI event buffers."""

    buffers: dict[str, UIEventBuffer] = field(default_factory=dict)

    async def start(
        self,
        backend: Any,
        *,
        buffer_id: str,
        selector: dict[str, Any],
        fields: list[str],
        max_events: int = 20,
    ) -> dict[str, Any]:
        if buffer_id in self.buffers:
            return {
                "status": "FAIL",
                "reason": "event buffer already exists",
                "buffer_id": buffer_id,
            }
        event_limit = max(1, min(max_events, 20))
        baseline = await query_ui_fields(
            backend,
            selector,
            fields=fields,
            max_results=event_limit,
        )
        if baseline.get("status") != "PASS":
            return baseline
        self.buffers[buffer_id] = UIEventBuffer(
            backend=backend,
            selector=dict(selector),
            fields=list(fields),
            max_events=event_limit,
            baseline=baseline,
        )
        return {
            "status": "PASS",
            "buffer_id": buffer_id,
            "source": "polling",
            "fields": list(fields),
            "event_count": 0,
            "dropped_count": 0,
            "cursor": 0,
            "next_cursor": 0,
            "oldest_cursor": 1,
            "stale_cursor": False,
        }

    async def read(self, buffer_id: str, *, backend: Any | None = None) -> dict[str, Any]:
        buffer = self.buffers.get(buffer_id)
        if buffer is None:
            return _missing_buffer(buffer_id, self.buffers, include_monitor_id=False)
        async with buffer.lock:
            _refresh_backend(buffer, backend)
            current = await _query_current(buffer)
            if current.get("status") != "PASS":
                return current
            _append_current_diff(buffer, current)
            buffer.baseline = current
            events = [_legacy_event(event) for event in buffer.events]
            result = {
                "status": "PASS",
                "buffer_id": buffer_id,
                "source": "polling",
                "events": events,
                "event_count": len(events),
                "dropped_count": buffer.dropped_count,
            }
            result["evidence_refs"] = [_evidence_ref(buffer_id, result)]
            return result

    def stop(self, buffer_id: str) -> dict[str, Any]:
        buffer = self.buffers.pop(buffer_id, None)
        if buffer is None:
            return _missing_buffer(buffer_id, self.buffers, include_monitor_id=False)
        return {
            "status": "PASS",
            "buffer_id": buffer_id,
            "source": "polling",
            "event_count": len(buffer.events),
            "dropped_count": buffer.dropped_count,
            "next_cursor": buffer.next_sequence - 1,
            "oldest_cursor": buffer.oldest_cursor,
        }

    async def monitor_start(
        self,
        backend: Any,
        *,
        monitor_id: str,
        selector: dict[str, Any],
        fields: list[str],
        max_events: int = 20,
    ) -> dict[str, Any]:
        result = await self.start(
            backend,
            buffer_id=monitor_id,
            selector=selector,
            fields=fields,
            max_events=max_events,
        )
        if result.get("status") == "PASS":
            result["monitor_id"] = monitor_id
            result["baseline_count"] = int(result.get("event_count") or 0) + _baseline_count(
                self.buffers[monitor_id].baseline
            )
        return result

    async def monitor_poll(
        self,
        monitor_id: str,
        *,
        after_cursor: int = 0,
        backend: Any | None = None,
    ) -> dict[str, Any]:
        buffer = self.buffers.get(monitor_id)
        if buffer is None:
            return _missing_buffer(monitor_id, self.buffers)
        async with buffer.lock:
            _refresh_backend(buffer, backend)
            current = await _query_current(buffer)
            if current.get("status") != "PASS":
                return current
            _append_current_diff(buffer, current)
            buffer.baseline = current
            return self.monitor_events(monitor_id, after_cursor=after_cursor)

    async def monitor_wait(
        self,
        monitor_id: str,
        *,
        after_cursor: int = 0,
        timeout_ms: int = 1000,
        poll_interval_ms: int = 100,
        backend: Any | None = None,
        backend_provider: Callable[[], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        interval = max(1, poll_interval_ms) / 1000
        result = self.monitor_events(monitor_id, after_cursor=after_cursor)
        if result.get("status") != "PASS" or result.get("events"):
            return result
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _monitor_wait_timed_out(result)
            result = await self._monitor_poll_with_deadline(
                monitor_id,
                after_cursor=after_cursor,
                timeout_s=remaining,
                backend=backend,
                backend_provider=backend_provider,
            )
            if result.get("status") != "PASS" or result.get("events"):
                return result
            if time.monotonic() >= deadline:
                return _monitor_wait_timed_out(result)
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    async def _monitor_poll_with_deadline(
        self,
        monitor_id: str,
        *,
        after_cursor: int,
        timeout_s: float,
        backend: Any | None,
        backend_provider: Callable[[], Awaitable[Any]] | None,
    ) -> dict[str, Any]:
        buffer = self.buffers.get(monitor_id)
        if buffer is None:
            return _missing_buffer(monitor_id, self.buffers)

        deadline = time.monotonic() + timeout_s
        if not await _acquire_lock_with_deadline(buffer.lock, timeout_s):
            return _monitor_wait_timed_out(
                self.monitor_events(monitor_id, after_cursor=after_cursor)
            )
        try:
            provider_ok, provided_backend = await _resolve_backend_with_deadline(
                backend_provider,
                max(0.0, deadline - time.monotonic()),
            )
            if not provider_ok:
                return _monitor_wait_timed_out(
                    self.monitor_events(monitor_id, after_cursor=after_cursor)
                )
            _refresh_backend(buffer, provided_backend if backend_provider is not None else backend)
            query_task = asyncio.create_task(_query_current(buffer))
            done, _pending = await asyncio.wait(
                {query_task},
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if query_task not in done or query_task.cancelled():
                _cancel_without_waiting(query_task)
                return _monitor_wait_timed_out(
                    self.monitor_events(monitor_id, after_cursor=after_cursor)
                )

            current = query_task.result()
            if current.get("status") != "PASS":
                return current
            if self.buffers.get(monitor_id) is not buffer:
                return _missing_buffer(monitor_id, self.buffers)
            _append_current_diff(buffer, current)
            buffer.baseline = current
            return self.monitor_events(monitor_id, after_cursor=after_cursor)
        finally:
            buffer.lock.release()

    def monitor_events(self, monitor_id: str, *, after_cursor: int = 0) -> dict[str, Any]:
        buffer = self.buffers.get(monitor_id)
        if buffer is None:
            return _missing_buffer(monitor_id, self.buffers)
        events = [
            deepcopy(event)
            for event in buffer.events
            if int(event["sequence"]) > after_cursor
        ]
        stale_cursor = bool(buffer.events) and after_cursor < buffer.oldest_cursor - 1
        return {
            "status": "PASS",
            "monitor_id": monitor_id,
            "source": "polling",
            "events": events,
            "event_count": len(events),
            "retained_event_count": len(buffer.events),
            "cursor": after_cursor,
            "next_cursor": buffer.next_sequence - 1,
            "oldest_cursor": buffer.oldest_cursor,
            "dropped_count": buffer.dropped_count,
            "stale_cursor": stale_cursor,
            "evidence_refs": [
                _evidence_ref(
                    monitor_id,
                    {
                        "event_count": len(events),
                        "dropped_count": buffer.dropped_count,
                    },
                    kind="ui_monitor",
                )
            ],
        }


def _events_from_diff(diff: dict[str, Any]) -> list[dict[str, Any]]:
    if diff.get("status") != "PASS":
        return []
    events = []
    for record in diff.get("changed", []):
        events.append(
            {
                "kind": "changed",
                "element_id": record["element_id"],
                "changes": record["changes"],
            }
        )
    for record in diff.get("added", []):
        events.append({"kind": "added", "element": record})
    for record in diff.get("removed", []):
        events.append({"kind": "removed", "element": record})
    return events


def _append_current_diff(buffer: UIEventBuffer, current: dict[str, Any]) -> None:
    diff_store = UISnapshotStore()
    diff_store.save({"snapshot": "before", **buffer.baseline})
    diff_store.save({"snapshot": "after", **current})
    diff = diff_ui_snapshots(diff_store, "before", "after", fields=buffer.fields)
    for event in _events_from_diff(diff):
        event["sequence"] = buffer.next_sequence
        buffer.next_sequence += 1
        buffer.events.append(event)
    overflow = max(0, len(buffer.events) - buffer.max_events)
    if overflow:
        del buffer.events[:overflow]
        buffer.dropped_count += overflow


def _legacy_event(event: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(event)
    result.pop("sequence", None)
    return result


def _refresh_backend(buffer: UIEventBuffer, backend: Any | None) -> None:
    if backend is not None:
        buffer.backend = backend


async def _resolve_backend_with_deadline(
    backend_provider: Callable[[], Awaitable[Any]] | None,
    timeout_s: float,
) -> tuple[bool, Any | None]:
    if backend_provider is None:
        return True, None

    backend_task = asyncio.create_task(backend_provider())
    done, _pending = await asyncio.wait({backend_task}, timeout=max(0.0, timeout_s))
    if backend_task not in done or backend_task.cancelled():
        _cancel_without_waiting(backend_task)
        return False, None
    return True, backend_task.result()


async def _query_current(buffer: UIEventBuffer) -> dict[str, Any]:
    return await query_ui_fields(
        buffer.backend,
        buffer.selector,
        fields=buffer.fields,
        max_results=buffer.max_events,
    )


def _cancel_without_waiting(task: asyncio.Task[Any]) -> None:
    task.cancel()
    task.add_done_callback(_consume_late_task_result)


async def _acquire_lock_with_deadline(lock: asyncio.Lock, timeout_s: float) -> bool:
    acquire_task = asyncio.create_task(lock.acquire())
    done, _pending = await asyncio.wait({acquire_task}, timeout=max(0.0, timeout_s))
    if acquire_task not in done:
        _cancel_without_waiting(acquire_task)
        return False
    if acquire_task.cancelled():
        _cancel_without_waiting(acquire_task)
        return False
    return bool(acquire_task.result())


def _consume_late_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def _missing_buffer(
    buffer_id: str,
    buffers: dict[str, UIEventBuffer],
    *,
    include_monitor_id: bool = True,
) -> dict[str, Any]:
    result = {
        "status": "FAIL",
        "reason": "event buffer not found",
        "buffer_id": buffer_id,
        "available_buffers": sorted(buffers),
    }
    if include_monitor_id:
        result["monitor_id"] = buffer_id
    return result


def _monitor_wait_timed_out(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "PASS":
        return result
    return {
        **result,
        "status": "BLOCKED",
        "reason": "ui monitor wait timed out",
        "next_step": "Poll again with ui_monitor_poll or increase timeout_ms.",
    }


def _baseline_count(baseline: dict[str, Any]) -> int:
    value = baseline.get("returned_count", baseline.get("element_count", 0))
    return value if isinstance(value, int) and value >= 0 else 0


def _evidence_ref(
    buffer_id: str,
    result: dict[str, Any],
    *,
    kind: str = "ui_events",
) -> dict[str, str]:
    event_count = int(result.get("event_count") or 0)
    dropped_count = int(result.get("dropped_count") or 0)
    return {
        "kind": kind,
        "ref": f"{kind}:{buffer_id}",
        "summary": f"events={event_count} dropped={dropped_count}",
    }

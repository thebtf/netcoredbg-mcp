"""Bounded selector-scoped UI event evidence via polling snapshots."""

from __future__ import annotations

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
        }

    async def read(self, buffer_id: str) -> dict[str, Any]:
        buffer = self.buffers.get(buffer_id)
        if buffer is None:
            return {
                "status": "FAIL",
                "reason": "event buffer not found",
                "buffer_id": buffer_id,
                "available_buffers": sorted(self.buffers),
            }
        current = await query_ui_fields(
            buffer.backend,
            buffer.selector,
            fields=buffer.fields,
            max_results=buffer.max_events,
        )
        if current.get("status") != "PASS":
            return current
        diff_store = UISnapshotStore()
        diff_store.save({"snapshot": "before", **buffer.baseline})
        diff_store.save({"snapshot": "after", **current})
        diff = diff_ui_snapshots(diff_store, "before", "after", fields=buffer.fields)
        new_events = _events_from_diff(diff)
        if new_events:
            buffer.events.extend(new_events)
            overflow = max(0, len(buffer.events) - buffer.max_events)
            if overflow:
                del buffer.events[:overflow]
                buffer.dropped_count += overflow
        buffer.baseline = current
        return {
            "status": "PASS",
            "buffer_id": buffer_id,
            "source": "polling",
            "events": list(buffer.events),
            "event_count": len(buffer.events),
            "dropped_count": buffer.dropped_count,
            "evidence_refs": [{
                "kind": "ui_events",
                "ref": f"ui_events:{buffer_id}",
                "summary": f"events={len(buffer.events)} dropped={buffer.dropped_count}",
            }],
        }

    def stop(self, buffer_id: str) -> dict[str, Any]:
        buffer = self.buffers.pop(buffer_id, None)
        if buffer is None:
            return {
                "status": "FAIL",
                "reason": "event buffer not found",
                "buffer_id": buffer_id,
                "available_buffers": sorted(self.buffers),
            }
        return {
            "status": "PASS",
            "buffer_id": buffer_id,
            "source": "polling",
            "event_count": len(buffer.events),
            "dropped_count": buffer.dropped_count,
        }


def _events_from_diff(diff: dict[str, Any]) -> list[dict[str, Any]]:
    if diff.get("status") != "PASS":
        return []
    events = []
    for record in diff.get("changed", []):
        events.append({
            "kind": "changed",
            "element_id": record["element_id"],
            "changes": record["changes"],
        })
    for record in diff.get("added", []):
        events.append({"kind": "added", "element": record})
    for record in diff.get("removed", []):
        events.append({"kind": "removed", "element": record})
    return events

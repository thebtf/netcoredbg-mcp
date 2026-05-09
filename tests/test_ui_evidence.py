"""Focused UI query, snapshot, diff, and event evidence tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.ui_evidence import register_ui_evidence_tools
from netcoredbg_mcp.ui.events import UIEventBufferStore
from netcoredbg_mcp.ui.snapshots import (
    ALLOWED_UI_FIELDS,
    UISnapshotStore,
    capture_ui_snapshot,
    diff_ui_snapshots,
    query_ui_fields,
)


class FakeEvidenceBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = [
            {
                "status": "PASS",
                "elements": [
                    {
                        "element_id": "row-1",
                        "focus": False,
                        "selection": {"selected": False},
                        "text": "Alice",
                        "enabled": True,
                        "visible": True,
                        "window": {"title": "Main"},
                        "full_tree": {"must": "not leak"},
                    }
                ],
                "element_count": 1,
            }
        ]

    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "selector": dict(selector),
                "fields": list(fields),
                "max_results": max_results,
            }
        )
        response = (
            self.responses.pop(0)
            if self.responses
            else {
                "status": "PASS",
                "elements": [],
                "element_count": 0,
            }
        )
        return response


class UnsupportedEvidenceBackend:
    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "unsupported": True,
            "backend": "pywinauto",
            "reason": "focused UI evidence requires FlaUI bridge",
        }


class FakeUiSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.IDLE,
            process_id=None,
            output_buffer=deque(),
        )
        self.process_registry = None


@pytest.mark.asyncio
async def test_query_ui_returns_only_requested_fields_and_bounded_counts() -> None:
    backend = FakeEvidenceBackend()

    result = await query_ui_fields(
        backend,
        {"automation_id": "grid"},
        fields=["focus", "selection", "text"],
        max_results=1,
    )

    assert result["status"] == "PASS"
    assert result["fields"] == ["focus", "selection", "text"]
    assert result["element_count"] == 1
    assert result["returned_count"] == 1
    assert result["omitted_count"] == 0
    assert result["evidence_refs"] == [
        {
            "kind": "ui_query",
            "ref": "ui_query:grid",
            "summary": "returned=1 omitted=0 fields=focus,selection,text",
        }
    ]
    assert result["elements"] == [
        {
            "element_id": "row-1",
            "focus": False,
            "selection": {"selected": False},
            "text": "Alice",
        }
    ]
    assert "full_tree" not in str(result)
    assert backend.calls[0]["fields"] == ["focus", "selection", "text"]


@pytest.mark.asyncio
async def test_query_ui_reports_omitted_records_when_backend_returns_past_limit() -> None:
    backend = FakeEvidenceBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [
                {"element_id": "row-1", "text": "Alice"},
                {"element_id": "row-2", "text": "Bob"},
            ],
            "element_count": 3,
            "returned_count": 2,
            "omitted_count": 1,
        }
    ]

    result = await query_ui_fields(
        backend,
        {},
        fields=["text"],
        max_results=2,
    )

    assert result["status"] == "PASS"
    assert result["element_count"] == 3
    assert result["returned_count"] == 2
    assert result["omitted_count"] == 1
    assert [element["element_id"] for element in result["elements"]] == ["row-1", "row-2"]


@pytest.mark.asyncio
async def test_query_ui_unknown_fields_fail_before_backend_call() -> None:
    backend = FakeEvidenceBackend()

    result = await query_ui_fields(
        backend,
        {"automation_id": "grid"},
        fields=["text", "layout"],
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "unknown UI fields"
    assert result["invalid_fields"] == ["layout"]
    assert sorted(result["allowed_fields"]) == sorted(ALLOWED_UI_FIELDS)
    assert backend.calls == []


@pytest.mark.asyncio
async def test_snapshot_capture_and_retrieve_by_name() -> None:
    backend = FakeEvidenceBackend()
    store = UISnapshotStore()

    snapshot = await capture_ui_snapshot(
        backend,
        store,
        name="before",
        selector={"automation_id": "grid"},
        fields=["text", "selection"],
    )
    duplicate = await capture_ui_snapshot(
        backend,
        store,
        name="before",
        selector={"automation_id": "grid"},
        fields=["text"],
    )

    assert snapshot["status"] == "PASS"
    assert snapshot["snapshot"] == "before"
    assert store.get("before")["elements"][0]["text"] == "Alice"
    assert duplicate["status"] == "FAIL"
    assert duplicate["reason"] == "snapshot name already exists"


@pytest.mark.asyncio
async def test_unsupported_backend_returns_blocked_without_false_positive() -> None:
    result = await query_ui_fields(
        UnsupportedEvidenceBackend(),
        {"automation_id": "grid"},
        fields=["text"],
    )

    assert result["status"] == "BLOCKED"
    assert result["unsupported"] is True
    assert result["backend"] == "pywinauto"
    assert result["elements"] == []


def test_diff_ui_snapshots_reports_added_removed_and_changed_records() -> None:
    store = UISnapshotStore()
    store.save(
        {
            "snapshot": "before",
            "fields": ["text", "selection"],
            "elements": [
                {"element_id": "row-1", "text": "Alice", "selection": {"selected": False}},
                {"element_id": "row-2", "text": "Bob", "selection": {"selected": False}},
            ],
        }
    )
    store.save(
        {
            "snapshot": "after",
            "fields": ["text", "selection"],
            "elements": [
                {"element_id": "row-1", "text": "Alice", "selection": {"selected": True}},
                {"element_id": "row-3", "text": "Charlie", "selection": {"selected": False}},
            ],
        }
    )

    result = diff_ui_snapshots(store, "before", "after", fields=["selection", "text"])

    assert result["status"] == "PASS"
    assert result["added"] == [
        {"element_id": "row-3", "text": "Charlie", "selection": {"selected": False}}
    ]
    assert result["removed"] == [
        {"element_id": "row-2", "text": "Bob", "selection": {"selected": False}}
    ]
    assert result["changed"] == [
        {
            "element_id": "row-1",
            "changes": {
                "selection": {
                    "before": {"selected": False},
                    "after": {"selected": True},
                }
            },
        }
    ]
    assert "text" not in result["changed"][0]["changes"]


def test_diff_unknown_snapshot_fails_with_available_names() -> None:
    store = UISnapshotStore()
    store.save({"snapshot": "before", "fields": ["text"], "elements": []})

    result = diff_ui_snapshots(store, "before", "missing", fields=["text"])

    assert result["status"] == "FAIL"
    assert result["reason"] == "snapshot not found"
    assert result["available_snapshots"] == ["before"]


def test_snapshot_store_missing_name_raises_clear_keyerror() -> None:
    store = UISnapshotStore()

    with pytest.raises(KeyError, match="UI snapshot not found: missing"):
        store.get("missing")


@pytest.mark.asyncio
async def test_event_buffer_polls_diff_and_enforces_max_size() -> None:
    backend = FakeEvidenceBackend()
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

    started = await store.start(
        backend,
        buffer_id="flow",
        selector={"automation_id": "grid"},
        fields=["text", "focus"],
        max_events=1,
    )
    first_read = await store.read("flow")
    second_read = await store.read("flow")

    assert started["status"] == "PASS"
    assert started["source"] == "polling"
    assert first_read["events"][0]["changes"]["text"] == {"before": "A", "after": "B"}
    assert second_read["status"] == "PASS"
    assert len(second_read["events"]) == 1
    assert second_read["dropped_count"] == 1
    assert second_read["events"][0]["changes"]["focus"] == {"before": False, "after": True}


@pytest.mark.asyncio
async def test_event_unknown_buffer_fails() -> None:
    result = await UIEventBufferStore().read("missing")

    assert result["status"] == "FAIL"
    assert result["reason"] == "event buffer not found"


@pytest.mark.asyncio
async def test_ui_evidence_tools_register_and_reject_invalid_fields_before_backend(
    mock_netcoredbg_path,
    capturing_mcp,
) -> None:
    server = create_server(str(os.getcwd()))
    tool_names = {tool.name for tool in await server.list_tools()}

    assert {"ui_query", "ui_snapshot", "ui_diff", "ui_events"}.issubset(tool_names)

    mcp = capturing_mcp
    session = FakeUiSession()
    register_ui_evidence_tools(
        mcp=mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await mcp.tools["ui_query"](
        ctx=None,
        fields=["text", "unknown"],
        automation_id="grid",
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["reason"] == "unknown UI fields"


def test_manual_smoke_list_includes_focused_ui_evidence_scenario() -> None:
    result = subprocess.run(
        [sys.executable, "tests/smoke_test_manual.py", "--list"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "UI Focused Evidence" in result.stdout


def test_bridge_snapshot_query_uses_bounded_child_scan() -> None:
    command = Path("bridge/Commands/SnapshotCommands.cs").read_text(encoding="utf-8")

    assert "root.FindAllChildren().Take" in command
    assert "root.FindAllDescendants().Take" not in command

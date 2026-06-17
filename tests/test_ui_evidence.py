"""Focused UI query, snapshot, diff, and event evidence tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
from netcoredbg_mcp.ui.text import assert_text_selection, read_textbox_state


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

    async def extract_text(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "extract_text": {
                    "automation_id": automation_id,
                    "name": name,
                    "control_type": control_type,
                    "root_id": root_id,
                    "xpath": xpath,
                }
            }
        )
        return {
            "status": "PASS",
            "text": "Fixture cue one",
            "source": "ValuePattern",
            "full_tree": {"must": "not leak"},
        }

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "find_element": {
                    "automation_id": automation_id,
                    "name": name,
                    "control_type": control_type,
                    "root_id": root_id,
                    "xpath": xpath,
                }
            }
        )
        return {
            "status": "PASS",
            "automationId": automation_id or "CueTextBox",
            "name": "Cue text",
            "controlType": control_type or "TextBox",
            "className": "TextBox",
            "full_tree": {"must": "not leak"},
            "raw_tree": {"must": "not leak"},
        }

    async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"textbox_state": {"selector": dict(selector)}})
        return {
            "status": "PASS",
            "text": "Fixture cue one",
            "value": "Fixture cue one",
            "selection": {
                "start": 3,
                "end": 10,
                "length": 7,
                "selected_text": "ture cu",
            },
            "caret_index": 10,
            "focus_within": True,
            "enabled": True,
            "visible": True,
            "source": "TextPattern",
            "full_tree": {"must": "not leak"},
            "raw_tree": {"must": "not leak"},
        }

    async def grid_snapshot(
        self,
        selector: dict[str, Any],
        rows: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "grid_snapshot": {
                    "selector": dict(selector),
                    "rows": dict(rows or {}),
                    "columns": list(columns or []),
                }
            }
        )
        return {
            "status": "PASS",
            "visible_rows": [
                {"index": 0, "cells": {"Phrase": "Fixture cue one", "Start": "00:00:01"}}
            ],
            "row_count": 1,
        }

    async def grid_selected_rows(
        self,
        selector: dict[str, Any],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "grid_selected_rows": {
                    "selector": dict(selector),
                    "columns": list(columns or []),
                }
            }
        )
        return {
            "status": "PASS",
            "selected_rows": [
                {
                    "index": 1,
                    "automation_id": "Row_1",
                    "cells": {"Phrase": "Fixture cue two"},
                }
            ],
        }

    async def grid_select_range(
        self,
        selector: dict[str, Any],
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "grid_select_range": {
                    "selector": dict(selector),
                    "start_index": start_index,
                    "end_index": end_index,
                }
            }
        )
        return {
            "status": "PASS",
            "selected_range": {"start": start_index, "end": end_index},
        }

    async def grid_click_row(
        self,
        selector: dict[str, Any],
        row_index: int,
        column: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "grid_click_row": {
                    "selector": dict(selector),
                    "row_index": row_index,
                    "column": column,
                    "columns": list(columns or []),
                }
            }
        )
        return {
            "status": "PASS",
            "clicked": True,
            "row": {"index": row_index, "cells": {"Phrase": "Fixture cue two"}},
            "hit_target": {"x": 42, "y": 84, "column": column},
        }

    async def assert_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"assert_focus": {"selector": dict(selector)}})
        return {
            "status": "PASS",
            "focused": True,
            "reason": "focus matched",
            "expected": {"automationId": selector.get("automation_id")},
            "actual": {"automationId": "CueTextBox"},
            "full_tree": {"must": "not leak"},
        }


class FakeSetTextClient:
    def __init__(self, owner: FakeSetTextBackend) -> None:
        self._owner = owner

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._owner.calls.append({"client_call": {"method": method, "payload": dict(payload)}})
        if method == "set_focus":
            return {"status": "PASS", "focused": True}
        return {"status": "FAIL", "reason": f"unexpected method {method}"}


class RawFailingSetTextClient:
    def __init__(self, owner: FakeSetTextBackend) -> None:
        self._owner = owner

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._owner.calls.append({"client_call": {"method": method, "payload": dict(payload)}})
        return {
            "status": "BLOCKED",
            "reason": "focus rejected",
            "full_tree": {"must": "not leak"},
            "result": {"raw_tree": {"must": "not leak"}},
        }


class FakeSetTextBackend:
    def __init__(
        self,
        *,
        state_result: dict[str, Any] | None = None,
        read_result: dict[str, Any] | None = None,
        find_result: dict[str, Any] | None = None,
    ) -> None:
        self.process_id = 42
        self.calls: list[dict[str, Any]] = []
        self.client = FakeSetTextClient(self)
        self.state_result = state_result or {
            "status": "PASS",
            "text": "Fixture cue one",
            "value": "Fixture cue one",
            "selection": {"start": 0, "end": 15, "length": 15},
            "focus_within": True,
            "source": "TextPattern",
            "full_tree": {"must": "not leak"},
        }
        self.read_result = read_result or {
            "status": "PASS",
            "text": "Replaced text",
            "source": "ValuePattern",
            "full_tree": {"must": "not leak"},
        }
        self.find_result = find_result or {"status": "PASS", "found": True}

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "find_element": {
                    "automation_id": automation_id,
                    "name": name,
                    "control_type": control_type,
                    "root_id": root_id,
                    "xpath": xpath,
                }
            }
        )
        return dict(self.find_result)

    def send_keys(self, keys: str) -> dict[str, Any]:
        self.calls.append({"send_keys": keys})
        return {"status": "PASS", "keys": keys}

    async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"textbox_state": {"selector": dict(selector)}})
        return dict(self.state_result)

    async def extract_text(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "extract_text": {
                    "automation_id": automation_id,
                    "name": name,
                    "control_type": control_type,
                    "root_id": root_id,
                    "xpath": xpath,
                }
            }
        )
        return dict(self.read_result)


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


class UnsupportedGridFallbackBackend(FakeEvidenceBackend):
    async def grid_selected_rows(
        self,
        selector: dict[str, Any],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "grid_selected_rows": {
                    "selector": dict(selector),
                    "columns": list(columns or []),
                }
            }
        )
        return {
            "status": "UNSUPPORTED",
            "unsupported": True,
            "backend": "pywinauto",
            "reason": "DataGrid selection evidence requires the FlaUI bridge backend.",
        }


class RaisingTextStateBackend:
    async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("closed pipe")


class RaisingTextQueryBackend:
    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        raise TimeoutError("bridge timeout")


class UnsupportedTextQueryBackend:
    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        return {
            "status": "UNSUPPORTED",
            "unsupported": True,
            "backend": "pywinauto",
            "reason": "TextBox state requires FlaUI bridge",
        }


class UnsupportedSelectionStateBackend:
    async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "PASS",
            "selection": {"supported": False},
            "source": "SelectionItemPattern",
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
async def test_query_ui_datagrid_selection_uses_grid_selected_rows() -> None:
    backend = FakeEvidenceBackend()
    backend.responses = [
        {
            "status": "BLOCKED",
            "reason": "FlaUI bridge error: Internal error: Element not found",
            "elements": [],
        }
    ]

    result = await query_ui_fields(
        backend,
        {"automation_id": "dataGrid2", "control_type": "DataGrid"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "PASS"
    assert result["fields"] == ["selection"]
    assert result["element_count"] == 1
    assert result["returned_count"] == 1
    assert result["elements"] == [
        {
            "element_id": "dataGrid2",
            "selection": {
                "source": "grid_selected_rows",
                "selected_count": 1,
                "selected_rows": [
                    {
                        "index": 1,
                        "automation_id": "Row_1",
                        "cells": {"Phrase": "Fixture cue two"},
                    }
                ],
            },
        }
    ]
    assert backend.calls == [
        {
            "grid_selected_rows": {
                "selector": {"automation_id": "dataGrid2", "control_type": "DataGrid"},
                "columns": [],
            }
        }
    ]


@pytest.mark.asyncio
async def test_query_ui_datagrid_selection_honors_xpath_with_generic_resolver() -> None:
    backend = FakeEvidenceBackend()
    backend.responses = [
        {
            "status": "PASS",
            "elements": [
                {
                    "element_id": "xpath-grid",
                    "selection": {"selected": True},
                }
            ],
            "element_count": 1,
        }
    ]

    result = await query_ui_fields(
        backend,
        {"xpath": "//DataGrid[2]", "control_type": "DataGrid"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "PASS"
    assert result["elements"] == [
        {
            "element_id": "xpath-grid",
            "selection": {"selected": True},
        }
    ]
    assert backend.calls == [
        {
            "selector": {"xpath": "//DataGrid[2]", "control_type": "DataGrid"},
            "fields": ["selection"],
            "max_results": 1,
        }
    ]


@pytest.mark.asyncio
async def test_query_ui_datagrid_selection_falls_back_after_automation_id_only_miss() -> None:
    backend = FakeEvidenceBackend()
    backend.responses = [
        {
            "status": "BLOCKED",
            "reason": "FlaUI bridge error: Internal error: Element not found",
            "elements": [],
        }
    ]

    result = await query_ui_fields(
        backend,
        {"automation_id": "dataGrid2"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "PASS"
    assert result["elements"][0]["selection"]["source"] == "grid_selected_rows"
    assert result["elements"][0]["selection"]["selected_rows"] == [
        {
            "index": 1,
            "automation_id": "Row_1",
            "cells": {"Phrase": "Fixture cue two"},
        }
    ]
    assert backend.calls == [
        {
            "selector": {"automation_id": "dataGrid2"},
            "fields": ["selection"],
            "max_results": 1,
        },
        {
            "grid_selected_rows": {
                "selector": {"automation_id": "dataGrid2"},
                "columns": [],
            }
        },
    ]


@pytest.mark.asyncio
async def test_query_ui_datagrid_selection_keeps_xpath_miss_blocked() -> None:
    backend = FakeEvidenceBackend()
    backend.responses = [
        {
            "status": "BLOCKED",
            "reason": "FlaUI bridge error: Internal error: Element not found",
            "elements": [],
        }
    ]

    result = await query_ui_fields(
        backend,
        {"xpath": "//DataGrid[2]"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "FlaUI bridge error: Internal error: Element not found"
    assert result["elements"] == []
    assert backend.calls == [
        {
            "selector": {"xpath": "//DataGrid[2]"},
            "fields": ["selection"],
            "max_results": 1,
        }
    ]


@pytest.mark.asyncio
async def test_query_ui_datagrid_selection_keeps_generic_block_when_fallback_blocks() -> None:
    backend = UnsupportedGridFallbackBackend()
    backend.responses = [
        {
            "status": "BLOCKED",
            "reason": "FlaUI bridge error: Internal error: Element not found",
            "elements": [],
        }
    ]

    result = await query_ui_fields(
        backend,
        {"automation_id": "notAGrid"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "FlaUI bridge error: Internal error: Element not found"
    assert result["elements"] == []


@pytest.mark.asyncio
async def test_query_ui_explicit_datagrid_does_not_repeat_missing_grid_fallback() -> None:
    backend = FakeEvidenceBackend()
    backend.grid_selected_rows = None  # type: ignore[method-assign]
    backend.responses = [
        {
            "status": "BLOCKED",
            "reason": "focused UI evidence requires FlaUI bridge",
            "elements": [],
        }
    ]

    result = await query_ui_fields(
        backend,
        {"automation_id": "dataGrid2", "control_type": "DataGrid"},
        fields=["selection"],
        max_results=1,
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "focused UI evidence requires FlaUI bridge"
    assert backend.calls == [
        {
            "selector": {"automation_id": "dataGrid2", "control_type": "DataGrid"},
            "fields": ["selection"],
            "max_results": 1,
        }
    ]


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
    assert "monitor_id" not in first_read
    assert "sequence" not in first_read["events"][0]
    assert second_read["status"] == "PASS"
    assert len(second_read["events"]) == 1
    assert second_read["dropped_count"] == 1
    assert second_read["events"][0]["changes"]["focus"] == {"before": False, "after": True}
    second_read["events"][0]["changes"]["focus"]["after"] = False
    assert store.buffers["flow"].events[0]["changes"]["focus"]["after"] is True


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

    assert {"ui_text", "ui_query", "ui_snapshot", "ui_diff", "ui_events"}.issubset(
        tool_names
    )

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


@pytest.mark.asyncio
async def test_ui_text_tool_reads_text_without_assertion(capturing_mcp, monkeypatch) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="read",
        automation_id="CueTextBox",
        control_type="TextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["text"] == "Fixture cue one"
    assert response["data"]["source"] == "ValuePattern"
    assert response["data"]["selector"] == {
        "automation_id": "CueTextBox",
        "control_type": "TextBox",
    }
    assert "full_tree" not in str(response["data"])
    assert backend.calls[-1]["extract_text"] == {
        "automation_id": "CueTextBox",
        "name": None,
        "control_type": "TextBox",
        "root_id": None,
        "xpath": None,
    }


@pytest.mark.asyncio
async def test_ui_text_tool_get_state_returns_textbox_selection_state(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="get_state",
        automation_id="CueTextBox",
        control_type="TextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["text"] == "Fixture cue one"
    assert response["data"]["value"] == "Fixture cue one"
    assert response["data"]["selection"] == {
        "start": 3,
        "end": 10,
        "length": 7,
        "selected_text": "ture cu",
    }
    assert response["data"]["caret_index"] == 10
    assert response["data"]["focus_within"] is True
    assert response["data"]["enabled"] is True
    assert response["data"]["visible"] is True
    assert response["data"]["source"] == "TextPattern"
    assert response["data"]["selector"] == {
        "automation_id": "CueTextBox",
        "control_type": "TextBox",
    }
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])
    assert backend.calls[-1]["textbox_state"] == {
        "selector": {
            "automation_id": "CueTextBox",
            "control_type": "TextBox",
        }
    }


@pytest.mark.asyncio
async def test_read_textbox_state_blocks_when_textbox_state_reader_raises() -> None:
    result = await read_textbox_state(
        RaisingTextStateBackend(),
        {"automation_id": "CueTextBox"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "TextBox state reader raised exception: closed pipe"
    assert result["requested"] == {
        "selector": {"automation_id": "CueTextBox"},
        "fields": ["focus", "selection", "value", "text", "enabled", "visible"],
    }
    assert result["accepted"] == {
        "backend": "connected UI backend supporting textbox_state"
    }
    assert result["next_step"] == "Inspect UI backend or bridge transport diagnostics."


@pytest.mark.asyncio
async def test_read_textbox_state_blocks_when_query_ui_raises() -> None:
    result = await read_textbox_state(
        RaisingTextQueryBackend(),
        {"automation_id": "CueTextBox"},
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "TextBox state query_ui raised exception: bridge timeout"
    assert result["requested"] == {
        "selector": {"automation_id": "CueTextBox"},
        "fields": ["focus", "selection", "value", "text", "enabled", "visible"],
    }
    assert result["accepted"] == {"backend": "connected UI backend supporting query_ui"}
    assert result["next_step"] == "Inspect UI backend or bridge transport diagnostics."


@pytest.mark.asyncio
async def test_read_textbox_state_normalizes_unsupported_query_to_blocked() -> None:
    result = await read_textbox_state(
        UnsupportedTextQueryBackend(),
        {"automation_id": "CueTextBox"},
    )

    assert result["status"] == "BLOCKED"
    assert result["unsupported"] is True
    assert result["backend"] == "pywinauto"
    assert result["reason"] == "TextBox state requires FlaUI bridge"
    assert result["selector"] == {"automation_id": "CueTextBox"}


@pytest.mark.asyncio
async def test_assert_text_selection_blocks_when_range_endpoints_are_absent() -> None:
    result = await assert_text_selection(
        UnsupportedSelectionStateBackend(),
        {"automation_id": "NotATextBox"},
        selection_start=0,
        selection_end=0,
    )

    assert result["status"] == "BLOCKED"
    assert result["matched"] is False
    assert result["reason"] == "TextBox selection evidence unavailable"
    assert result["expected_selection"] == {"start": 0, "end": 0}
    assert result["actual_selection"] == {
        "supported": False,
        "source": "SelectionItemPattern",
    }
    assert result["selector"] == {"automation_id": "NotATextBox"}


@pytest.mark.asyncio
async def test_ui_text_tool_assert_selection_passes_with_expected_range(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="assert_selection",
        automation_id="CueTextBox",
        control_type="TextBox",
        selection_start=3,
        selection_end=10,
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["matched"] is True
    assert response["data"]["expected_selection"] == {"start": 3, "end": 10}
    assert response["data"]["actual_selection"] == {
        "start": 3,
        "end": 10,
        "length": 7,
        "selected_text": "ture cu",
    }


@pytest.mark.asyncio
async def test_ui_text_tool_assert_selection_fails_with_observed_range(
    capturing_mcp,
    monkeypatch,
) -> None:
    class MismatchTextBackend(FakeEvidenceBackend):
        async def textbox_state(self, selector: dict[str, Any]) -> dict[str, Any]:
            result = await super().textbox_state(selector)
            result["selection"] = {
                "start": 0,
                "end": 0,
                "length": 0,
                "selected_text": "",
            }
            result["caret_index"] = 0
            return result

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = MismatchTextBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="assert_selection",
        automation_id="CueTextBox",
        control_type="TextBox",
        selection_start=3,
        selection_end=10,
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["matched"] is False
    assert response["data"]["reason"] == "selection mismatch"
    assert response["data"]["expected_selection"] == {"start": 3, "end": 10}
    assert response["data"]["actual_selection"] == {
        "start": 0,
        "end": 0,
        "length": 0,
        "selected_text": "",
    }


@pytest.mark.asyncio
async def test_ui_text_set_text_focuses_selects_types_and_verifies(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend()

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="CueTextBox",
        control_type="TextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["route"] == "text_type_replace_selection"
    assert response["data"]["verified"] is True
    assert response["data"]["text"] == "Replaced text"
    assert response["data"]["precondition"]["selected"] is True
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])
    assert backend.calls == [
        {
            "find_element": {
                "automation_id": "CueTextBox",
                "name": None,
                "control_type": "TextBox",
                "root_id": None,
                "xpath": None,
            }
        },
        {
            "client_call": {
                "method": "set_focus",
                "payload": {
                    "automationId": "CueTextBox",
                    "controlType": "TextBox",
                },
            }
        },
        {"send_keys": "^a"},
        {"textbox_state": {"selector": {"automation_id": "CueTextBox", "control_type": "TextBox"}}},
        {"send_keys": "Replaced text"},
        {
            "extract_text": {
                "automation_id": "CueTextBox",
                "name": None,
                "control_type": "TextBox",
                "root_id": None,
                "xpath": None,
            }
        },
    ]


@pytest.mark.asyncio
async def test_ui_text_set_text_blocks_selector_miss_before_typing(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend(
        find_result={"status": "PASS", "found": False, "full_tree": {"must": "not leak"}}
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="MissingCueTextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "selector not found"
    assert "full_tree" not in str(response["data"])
    assert backend.calls == [
        {
            "find_element": {
                "automation_id": "MissingCueTextBox",
                "name": None,
                "control_type": None,
                "root_id": None,
                "xpath": None,
            }
        }
    ]


@pytest.mark.asyncio
async def test_ui_text_set_text_strips_raw_backend_failure_evidence(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend()
    backend.client = RawFailingSetTextClient(backend)

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="CueTextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "focus rejected"
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])


@pytest.mark.asyncio
async def test_ui_text_set_text_blocks_bad_select_all_state_before_literal_input(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend(
        state_result={
            "status": "PASS",
            "text": "Fixture cue one",
            "selection": {"start": 0, "end": 3, "length": 3},
            "focus_within": True,
            "full_tree": {"must": "not leak"},
        }
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="CueTextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "select-all precondition failed"
    assert response["data"]["precondition"]["selected"] is False
    assert "full_tree" not in str(response["data"])
    assert {"send_keys": "Replaced text"} not in backend.calls


@pytest.mark.asyncio
async def test_ui_text_set_text_blocks_without_textbox_state_evidence(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend(
        state_result={
            "status": "PASS",
            "selection": {"start": 0, "end": 15, "length": 15},
            "focus_within": True,
            "full_tree": {"must": "not leak"},
        }
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="CueTextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["precondition"]["reason"] == "TextBox text evidence unavailable"
    assert "full_tree" not in str(response["data"])
    assert {"send_keys": "Replaced text"} not in backend.calls


@pytest.mark.asyncio
async def test_ui_text_set_text_fails_on_post_read_mismatch(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeSetTextBackend(read_result={"status": "PASS", "text": "Different text"})

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="set_text",
        automation_id="CueTextBox",
        text="Replaced text",
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["reason"] == "post-read text mismatch"
    assert response["data"]["expected"] == "Replaced text"
    assert response["data"]["actual"] == "Different text"


@pytest.mark.asyncio
async def test_ui_text_tool_maps_selector_miss_to_blocked(capturing_mcp, monkeypatch) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        extract_text=AsyncMock(
            return_value={
                "status": "FAIL",
                "reason": "Element not found",
                "full_tree": {"must": "not leak"},
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="read",
        automation_id="MissingCueTextBox",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "selector not found"
    assert response["data"]["requested"] == {
        "selector": {"automation_id": "MissingCueTextBox"}
    }
    assert response["data"]["accepted"]["selector_keys"] == [
        "automation_id",
        "name",
        "control_type",
        "root_id",
        "xpath",
    ]
    assert response["data"]["next_step"]
    assert "full_tree" not in str(response["data"])


@pytest.mark.asyncio
async def test_ui_text_unknown_action_reports_state_actions_without_backend(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    create_backend = AsyncMock()

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", create_backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_text"](
        ctx=None,
        action="type",
        automation_id="CueTextBox",
    )

    assert response["data"] == {
        "status": "FAIL",
        "reason": "unknown text action",
        "action": "type",
        "accepted_actions": ["read", "get_state", "state", "assert_selection", "set_text"],
        "next_step": (
            'Use ui_text(action="read"|"get_state"|"assert_selection"|"set_text") '
            "for bounded TextBox evidence."
        ),
    }
    create_backend.assert_not_called()


@pytest.mark.asyncio
async def test_ui_property_tool_reads_element_property_without_unbounded_payload(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="read",
        property="Name",
        automation_id="CueTextBox",
        control_type="TextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["property"] == "Name"
    assert response["data"]["value"] == "Cue text"
    assert response["data"]["selector"] == {
        "automation_id": "CueTextBox",
        "control_type": "TextBox",
    }
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])
    assert backend.calls[-1]["find_element"] == {
        "automation_id": "CueTextBox",
        "name": None,
        "control_type": "TextBox",
        "root_id": None,
        "xpath": None,
    }


@pytest.mark.asyncio
async def test_ui_property_tool_reads_text_value_via_extract_text(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="read",
        property="Value",
        automation_id="CueTextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["property"] == "Value"
    assert response["data"]["value"] == "Fixture cue one"
    assert response["data"]["source"] == "ValuePattern"
    assert response["data"]["selector"] == {"automation_id": "CueTextBox"}
    assert "full_tree" not in str(response["data"])
    assert backend.calls[-1]["extract_text"] == {
        "automation_id": "CueTextBox",
        "name": None,
        "control_type": None,
        "root_id": None,
        "xpath": None,
    }


@pytest.mark.asyncio
async def test_ui_property_tool_keeps_missing_text_value_as_none(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        extract_text=AsyncMock(
            return_value={
                "status": "PASS",
                "text": None,
                "source": "ValuePattern",
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="read",
        property="Value",
        automation_id="CueTextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["value"] is None


@pytest.mark.asyncio
async def test_ui_property_tool_uses_case_insensitive_property_fallback(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        find_element=AsyncMock(
            return_value={
                "status": "PASS",
                "localizedControlType": "edit",
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="read",
        property="LocalizedControlType",
        automation_id="CueTextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["value"] == "edit"


@pytest.mark.asyncio
async def test_ui_property_unknown_action_reports_accepted_actions_without_backend(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    create_backend = AsyncMock()

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", create_backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="set",
        property="Name",
        automation_id="CueTextBox",
    )

    assert response["data"] == {
        "status": "FAIL",
        "reason": "unknown property action",
        "action": "set",
        "accepted_actions": ["read"],
        "next_step": "Use ui_property(action=\"read\") for read-only property evidence.",
    }
    create_backend.assert_not_called()


@pytest.mark.asyncio
async def test_ui_property_tool_maps_selector_miss_to_blocked(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        find_element=AsyncMock(
            return_value={
                "status": "FAIL",
                "reason": "Element not found",
                "full_tree": {"must": "not leak"},
                "raw_tree": {"must": "not leak"},
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_property"](
        ctx=None,
        action="read",
        property="Name",
        automation_id="MissingCueTextBox",
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["reason"] == "selector not found"
    assert response["data"]["requested"] == {
        "selector": {"automation_id": "MissingCueTextBox"}
    }
    assert response["data"]["accepted"]["selector_keys"] == [
        "automation_id",
        "name",
        "control_type",
        "root_id",
        "xpath",
    ]
    assert response["data"]["next_step"]
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])


@pytest.mark.asyncio
async def test_ui_grid_accepts_rows_alias_for_visible_rows(capturing_mcp, monkeypatch) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        grid_visible_rows=AsyncMock(
            return_value={
                "status": "PASS",
                "visible_rows": [{"index": 0, "cells": {"Phrase": "Fixture cue"}}],
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="rows",
        automation_id="dataGrid",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["visible_rows"][0]["index"] == 0
    assert response["data"]["requested_action"] == "rows"
    assert response["data"]["canonical_action"] == "visible_rows"


@pytest.mark.asyncio
async def test_ui_grid_accepts_snapshot_alias_with_columns(capturing_mcp, monkeypatch) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="snapshot",
        automation_id="CueGrid",
        columns=["Phrase", "Start"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["visible_rows"][0]["cells"] == {
        "Phrase": "Fixture cue one",
        "Start": "00:00:01",
    }
    assert response["data"]["requested_action"] == "snapshot"
    assert response["data"]["canonical_action"] == "snapshot"
    assert backend.calls[-1]["grid_snapshot"] == {
        "selector": {"automation_id": "CueGrid"},
        "rows": {},
        "columns": ["Phrase", "Start"],
    }


@pytest.mark.parametrize("action", ["cells", "cell_values"])
@pytest.mark.asyncio
async def test_ui_grid_accepts_cell_snapshot_aliases(
    capturing_mcp,
    monkeypatch,
    action: str,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action=action,
        automation_id="CueGrid",
        rows={"start": 0, "count": 2},
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["requested_action"] == action
    assert response["data"]["canonical_action"] == "snapshot"
    assert backend.calls[-1]["grid_snapshot"] == {
        "selector": {"automation_id": "CueGrid"},
        "rows": {"start": 0, "count": 2},
        "columns": ["Phrase"],
    }


@pytest.mark.asyncio
async def test_ui_focus_tool_is_registered_and_asserts_focus(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_focus"](
        ctx=None,
        action="assert",
        automation_id="CueTextBox",
        control_type="TextBox",
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["focused"] is True
    assert response["data"]["selector"] == {
        "automation_id": "CueTextBox",
        "control_type": "TextBox",
    }
    assert response["data"]["expected"] == {"automationId": "CueTextBox"}
    assert response["data"]["actual"] == {"automationId": "CueTextBox"}
    assert "full_tree" not in str(response["data"])
    assert backend.calls[-1]["assert_focus"] == {
        "selector": {
            "automation_id": "CueTextBox",
            "control_type": "TextBox",
        }
    }


@pytest.mark.asyncio
async def test_ui_grid_accepts_selection_alias_for_selected_rows(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="selection",
        automation_id="CueGrid",
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["requested_action"] == "selection"
    assert response["data"]["canonical_action"] == "selected_rows"
    assert response["data"]["selected_rows"] == [
        {
            "index": 1,
            "automation_id": "Row_1",
            "cells": {"Phrase": "Fixture cue two"},
        }
    ]
    assert backend.calls[-1]["grid_selected_rows"] == {
        "selector": {"automation_id": "CueGrid"},
        "columns": ["Phrase"],
    }


@pytest.mark.asyncio
async def test_ui_grid_select_range_returns_confirmed_selected_row_content(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["confirmed_selection"] is True
    assert response["data"]["selected_range"] == {"start": 1, "end": 1}
    assert response["data"]["selected_rows"] == [
        {
            "index": 1,
            "automation_id": "Row_1",
            "cells": {"Phrase": "Fixture cue two"},
        }
    ]


@pytest.mark.asyncio
async def test_ui_grid_select_range_blocks_when_confirmation_mismatches_requested_range(
    capturing_mcp,
    monkeypatch,
) -> None:
    class MismatchGridBackend(FakeEvidenceBackend):
        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 2, "cells": {"Phrase": "Wrong cue"}},
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = MismatchGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["confirmed_selection"] is False
    assert response["data"]["reason"] == "selected row confirmation failed"
    assert response["data"]["requested_range"] == {"start": 1, "end": 1}
    assert response["data"]["observed_selected_indices"] == [2]


@pytest.mark.asyncio
async def test_ui_grid_select_range_strips_unbounded_confirmation_failure(
    capturing_mcp,
    monkeypatch,
) -> None:
    class UnboundedFailureGridBackend(FakeEvidenceBackend):
        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "BLOCKED",
                "reason": "bridge returned unbounded diagnostic",
                "full_tree": {"must": "not leak"},
                "raw_tree": {"also": "not leak"},
                "selected_rows": [
                    {
                        "index": 1,
                        "cells": {"Phrase": "Fixture cue two"},
                        "full_tree": {"row": "not leak"},
                    }
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = UnboundedFailureGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "BLOCKED"
    assert response["data"]["confirmed_selection"] is False
    assert response["data"]["reason"] == "bridge returned unbounded diagnostic"
    assert response["data"]["requested_range"] == {"start": 1, "end": 1}
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])


@pytest.mark.asyncio
async def test_ui_grid_select_range_strips_unbounded_confirmed_rows(
    capturing_mcp,
    monkeypatch,
) -> None:
    class UnboundedSuccessGridBackend(FakeEvidenceBackend):
        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            result = await super().grid_select_range(selector, start_index, end_index)
            result["full_tree"] = {"selection": "not leak"}
            result["raw_tree"] = {"selection": "not leak"}
            return result

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "selected_rows": [
                    {
                        "index": 1,
                        "cells": {"Phrase": "Fixture cue two"},
                        "full_tree": {"row": "not leak"},
                        "raw_tree": {"row": "not leak"},
                    }
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = UnboundedSuccessGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["confirmed_selection"] is True
    assert response["data"]["selected_rows"] == [
        {"index": 1, "cells": {"Phrase": "Fixture cue two"}}
    ]
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])


@pytest.mark.asyncio
async def test_ui_grid_select_range_confirms_by_viewport_index_when_row_index_differs(
    capturing_mcp,
    monkeypatch,
) -> None:
    class VirtualizedGridBackend(FakeEvidenceBackend):
        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "selected_rows": [
                    {
                        "index": 1,
                        "row_index": 19,
                        "cells": {"Phrase": "Virtualized cue"},
                    }
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = VirtualizedGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["confirmed_selection"] is True
    assert response["data"]["observed_selected_indices"] == [1]
    assert response["data"]["selected_rows"] == [
        {"index": 1, "row_index": 19, "cells": {"Phrase": "Virtualized cue"}}
    ]


@pytest.mark.asyncio
async def test_ui_grid_get_state_returns_bounded_snapshot_and_selection(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = FakeEvidenceBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="get_state",
        automation_id="CueGrid",
        rows={"visible_only": True},
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["requested_action"] == "get_state"
    assert response["data"]["canonical_action"] == "get_state"
    assert response["data"]["visible_rows"] == [
        {"index": 0, "cells": {"Phrase": "Fixture cue one", "Start": "00:00:01"}}
    ]
    assert response["data"]["selected_rows"] == [
        {"index": 1, "automation_id": "Row_1", "cells": {"Phrase": "Fixture cue two"}}
    ]
    assert "full_tree" not in str(response["data"])
    assert "raw_tree" not in str(response["data"])
    assert backend.calls == [
        {
            "grid_snapshot": {
                "selector": {"automation_id": "CueGrid"},
                "rows": {"visible_only": True},
                "columns": ["Phrase"],
            }
        },
        {
            "grid_selected_rows": {
                "selector": {"automation_id": "CueGrid"},
                "columns": ["Phrase"],
            }
        },
    ]


@pytest.mark.asyncio
async def test_ui_grid_select_row_resolves_visible_logical_row_index(
    capturing_mcp,
    monkeypatch,
) -> None:
    class VirtualizedGridBackend(FakeEvidenceBackend):
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_snapshot": {
                        "selector": dict(selector),
                        "rows": dict(rows or {}),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "row_count": 24,
                "visible_rows": [
                    {"index": 0, "row_index": 18, "cells": {"Phrase": "Cue 018"}},
                    {"index": 1, "row_index": 19, "cells": {"Phrase": "Cue 019"}},
                ],
            }

        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "selected_rows": [
                    {"index": 1, "row_index": 19, "cells": {"Phrase": "Cue 019"}}
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = VirtualizedGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_row",
        automation_id="CueGrid",
        row_index=19,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["canonical_action"] == "select_row"
    assert response["data"]["resolved_row"] == {
        "index": 1,
        "row_index": 19,
        "identity": "Cue 019",
    }
    assert response["data"]["confirmed_selection"] is True
    assert backend.calls[1] == {
        "grid_select_range": {
            "selector": {"automation_id": "CueGrid"},
            "start_index": 1,
            "end_index": 1,
        }
    }


@pytest.mark.asyncio
async def test_ui_grid_click_row_uses_backend_row_click_after_identity_resolution(
    capturing_mcp,
    monkeypatch,
) -> None:
    class KeyedGridBackend(FakeEvidenceBackend):
        async def grid_snapshot(
            self,
            selector: dict[str, Any],
            rows: dict[str, Any] | None = None,
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_snapshot": {
                        "selector": dict(selector),
                        "rows": dict(rows or {}),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "visible_rows": [
                    {"index": 0, "row_index": 18, "cells": {"Phrase": "Cue 018"}},
                    {"index": 1, "row_index": 19, "cells": {"Phrase": "Cue 019"}},
                ],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = KeyedGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="click_row",
        automation_id="CueGrid",
        row_key="Cue 019",
        column="Phrase",
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["canonical_action"] == "click_row"
    assert response["data"]["clicked"] is True
    assert response["data"]["resolved_row"] == {
        "index": 1,
        "row_index": 19,
        "identity": "Cue 019",
    }
    assert backend.calls[1] == {
        "grid_click_row": {
            "selector": {"automation_id": "CueGrid"},
            "row_index": 1,
            "column": "Phrase",
            "columns": ["Phrase"],
        }
    }


@pytest.mark.asyncio
async def test_ui_grid_select_range_ignores_malformed_string_index(
    capturing_mcp,
    monkeypatch,
) -> None:
    class MalformedIndexGridBackend(FakeEvidenceBackend):
        async def grid_selected_rows(
            self,
            selector: dict[str, Any],
            columns: list[str] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(
                {
                    "grid_selected_rows": {
                        "selector": dict(selector),
                        "columns": list(columns or []),
                    }
                }
            )
            return {
                "status": "PASS",
                "selected_rows": [{"index": "--5", "cells": {"Phrase": "Bad index"}}],
            }

    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = MalformedIndexGridBackend()
    backend.process_id = 42

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="select_range",
        automation_id="CueGrid",
        start_index=1,
        end_index=1,
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["confirmed_selection"] is False
    assert response["data"]["observed_selected_indices"] == []
    assert response["data"]["selected_rows"] == [
        {"index": "--5", "cells": {"Phrase": "Bad index"}}
    ]


@pytest.mark.asyncio
async def test_ui_grid_selected_rows_forwards_columns(capturing_mcp, monkeypatch) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    backend = SimpleNamespace(
        process_id=42,
        grid_selected_rows=AsyncMock(
            return_value={
                "status": "PASS",
                "selected_rows": [
                    {"index": 1, "cells": {"Phrase": "Fixture cue two"}},
                ],
            }
        ),
    )

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="selected_rows",
        automation_id="CueGrid",
        columns=["Phrase"],
    )

    assert response["data"]["status"] == "PASS"
    assert response["data"]["requested_action"] == "selected_rows"
    assert response["data"]["canonical_action"] == "selected_rows"
    backend.grid_selected_rows.assert_awaited_once_with(
        {"automation_id": "CueGrid"},
        columns=["Phrase"],
    )


@pytest.mark.asyncio
async def test_ui_grid_unknown_action_reports_accepted_actions_without_backend(
    capturing_mcp,
    monkeypatch,
) -> None:
    session = FakeUiSession()
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    create_backend = AsyncMock()

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", create_backend)
    register_ui_evidence_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
    )

    response = await capturing_mcp.tools["ui_grid"](
        ctx=None,
        action="row",
        automation_id="dataGrid",
    )

    assert response["data"] == {
        "status": "FAIL",
        "reason": "unknown grid action",
        "action": "row",
        "accepted_actions": [
            "visible_rows",
            "rows",
            "snapshot",
            "cells",
            "cell_values",
            "selected_rows",
            "selected",
            "selection",
            "select_range",
            "select_row",
            "click_row",
            "assert_range",
            "get_state",
            "state",
        ],
        "aliases": {
            "rows": "visible_rows",
            "cells": "snapshot",
            "cell_values": "snapshot",
            "selected": "selected_rows",
            "selection": "selected_rows",
            "state": "get_state",
        },
        "next_step": "Use one of the accepted ui_grid actions.",
    }
    create_backend.assert_not_called()


def test_manual_smoke_list_includes_focused_ui_evidence_scenario() -> None:
    result = subprocess.run(
        [sys.executable, "tests/smoke_test_manual.py", "--list"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "UI Focused Evidence" in result.stdout
    wpf_fixture = Path("tests/fixtures/WpfSmokeApp/bin/Debug/net8.0-windows/WpfSmokeApp.dll")
    if wpf_fixture.exists():
        assert "WPF UI Grid Rows Alias Fixture Replay" in result.stdout


def test_bridge_snapshot_query_uses_bounded_child_scan() -> None:
    command = Path("bridge/Commands/SnapshotCommands.cs").read_text(encoding="utf-8")

    assert "root.FindAllChildren().Take" in command
    assert "root.FindAllDescendants().Take" not in command

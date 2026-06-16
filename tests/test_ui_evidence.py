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
            "assert_range",
        ],
        "aliases": {
            "rows": "visible_rows",
            "cells": "snapshot",
            "cell_values": "snapshot",
            "selected": "selected_rows",
            "selection": "selected_rows",
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

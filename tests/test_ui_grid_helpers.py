"""DataGrid helper evidence tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.ui import grid as grid_helpers
from netcoredbg_mcp.ui.flaui_client import BRIDGE_DEFAULT_CALL_TIMEOUT_SECONDS, FlaUIBackend
from netcoredbg_mcp.ui.grid import (
    assert_grid_range,
    assert_grid_rows,
    read_grid_selected_rows,
    read_grid_visible_rows,
    select_grid_range,
    snapshot_grid,
)
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeGridBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def grid_visible_rows(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("visible", dict(selector)))
        return {
            "status": "PASS",
            "row_count": 3,
            "visible_rows": [
                {
                    "index": 0,
                    "automation_id": "Row_0",
                    "name": "CueRow",
                    "selected": False,
                    "cells": {
                        "Start": "00:00:01.0",
                        "End": "00:00:03.0",
                        "Character": "Narrator",
                        "Phrase": "Fixture cue one",
                    },
                },
                {
                    "index": 1,
                    "automation_id": "Row_1",
                    "name": "CueRow",
                    "selected": True,
                    "cells": {
                        "Start": "00:00:04.0",
                        "End": "00:00:06.0",
                        "Character": "ALICE",
                        "Phrase": "Fixture cue two",
                    },
                },
            ],
        }

    async def grid_snapshot(
        self,
        selector: dict[str, Any],
        rows: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("snapshot", dict(selector)))
        result = await self.grid_visible_rows(selector)
        result["requested_rows"] = dict(rows or {})
        result["requested_columns"] = list(columns or [])
        return result

    async def grid_selected_rows(
        self,
        selector: dict[str, Any],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        call = dict(selector)
        call["columns"] = list(columns or [])
        self.calls.append(("selected", call))
        return {
            "status": "PASS",
            "selected_rows": [{"index": 1, "automation_id": "Row_1", "name": "B"}],
        }

    async def grid_select_range(
        self,
        selector: dict[str, Any],
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        self.calls.append(("select", dict(selector)))
        return {
            "status": "PASS",
            "selected_range": {"start": start_index, "end": end_index},
            "selected_rows": [
                {"index": start_index, "automation_id": f"Row_{start_index}"},
                {"index": end_index, "automation_id": f"Row_{end_index}"},
            ],
        }

    async def grid_assert_range(
        self,
        selector: dict[str, Any],
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        self.calls.append(("assert", dict(selector)))
        return {
            "status": "PASS",
            "asserted": True,
            "expected_range": {"start": start_index, "end": end_index},
            "selected_rows": [
                {"index": start_index, "automation_id": f"Row_{start_index}"},
                {"index": end_index, "automation_id": f"Row_{end_index}"},
            ],
        }

    async def grid_click_row(
        self,
        selector: dict[str, Any],
        row_index: int,
        column: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "click",
                {
                    **dict(selector),
                    "row_index": row_index,
                    "column": column,
                    "columns": list(columns or []),
                },
            )
        )
        return {
            "status": "PASS",
            "clicked": True,
            "row": {"index": row_index},
        }

    async def grid_right_click_row(
        self,
        selector: dict[str, Any],
        row_index: int,
        column: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "right_click",
                {
                    **dict(selector),
                    "row_index": row_index,
                    "column": column,
                    "columns": list(columns or []),
                },
            )
        )
        return {
            "status": "PASS",
            "clicked": True,
            "right_clicked": True,
            "click_kind": "right",
            "row": {"index": row_index},
        }

    async def grid_double_click_row(
        self,
        selector: dict[str, Any],
        row_index: int,
        column: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "double_click",
                {
                    **dict(selector),
                    "row_index": row_index,
                    "column": column,
                    "columns": list(columns or []),
                },
            )
        )
        return {
            "status": "PASS",
            "clicked": True,
            "double_clicked": True,
            "click_kind": "double",
            "row": {"index": row_index},
        }


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


@pytest.mark.asyncio
async def test_grid_helpers_preserve_selector() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    visible = await read_grid_visible_rows(backend, selector)
    selected = await read_grid_selected_rows(backend, selector)
    state = await grid_helpers.read_grid_state(backend, selector, columns=["Phrase"])
    selected_range = await select_grid_range(backend, selector, 1, 2)
    selected_row = await grid_helpers.select_grid_row(
        backend,
        selector,
        1,
        columns=["Phrase"],
    )
    clicked_row = await grid_helpers.click_grid_row(
        backend,
        selector,
        1,
        column="Phrase",
    )
    double_clicked_row = await grid_helpers.double_click_grid_row(
        backend,
        selector,
        1,
        column="Phrase",
    )
    asserted_range = await assert_grid_range(backend, selector, 1, 2)

    assert selector == {"automation_id": "CueGrid"}
    assert visible["visible_rows"][0]["automation_id"] == "Row_0"
    assert selected["selected_rows"] == [{"index": 1, "automation_id": "Row_1", "name": "B"}]
    assert state["selected_rows"] == [{"index": 1, "automation_id": "Row_1", "name": "B"}]
    assert selected_range["selected_range"] == {"start": 1, "end": 2}
    assert selected_row["selected_range"] == {"start": 1, "end": 1}
    assert clicked_row["clicked"] is True
    assert double_clicked_row["double_clicked"] is True
    assert asserted_range["asserted"] is True


@pytest.mark.asyncio
async def test_read_grid_state_requests_identity_columns() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    state = await grid_helpers.read_grid_state(
        backend,
        selector,
        identity={"column": "PhraseId"},
    )

    assert state["identity_strategy"] == {
        "kind": "configured_column",
        "derived": True,
        "column": "PhraseId",
    }
    assert state["requested_columns"] == ["PhraseId"]
    assert ("selected", {"automation_id": "CueGrid", "columns": ["PhraseId"]}) in (
        backend.calls
    )


@pytest.mark.asyncio
async def test_click_grid_row_forwards_columns_to_backend() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    result = await grid_helpers.click_grid_row(
        backend,
        selector,
        row_key="Fixture cue two",
        column="Phrase",
        columns=["Phrase"],
    )

    assert result["status"] == "PASS"
    assert (
        "click",
        {
            "automation_id": "CueGrid",
            "row_index": 1,
            "column": "Phrase",
            "columns": ["Phrase"],
        },
    ) in backend.calls


@pytest.mark.asyncio
async def test_right_click_grid_row_forwards_columns_to_backend() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    result = await grid_helpers.right_click_grid_row(
        backend,
        selector,
        row_key="Fixture cue two",
        column="Phrase",
        columns=["Phrase"],
    )

    assert result["status"] == "PASS"
    assert (
        "right_click",
        {
            "automation_id": "CueGrid",
            "row_index": 1,
            "column": "Phrase",
            "columns": ["Phrase"],
        },
    ) in backend.calls


@pytest.mark.asyncio
async def test_double_click_grid_row_forwards_columns_to_backend() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    result = await grid_helpers.double_click_grid_row(
        backend,
        selector,
        row_key="Fixture cue two",
        column="Phrase",
        columns=["Phrase"],
    )

    assert result["status"] == "PASS"
    assert (
        "double_click",
        {
            "automation_id": "CueGrid",
            "row_index": 1,
            "column": "Phrase",
            "columns": ["Phrase"],
        },
    ) in backend.calls


@pytest.mark.asyncio
async def test_ensure_grid_row_visible_returns_already_visible_without_backend_realize() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    result = await grid_helpers.ensure_grid_row_visible(
        backend,
        selector,
        row_key="Fixture cue two",
        identity={"column": "Phrase"},
        rows={"visible_only": True},
        columns=["Phrase"],
    )

    assert result["status"] == "PASS"
    assert result["already_visible"] is True
    assert result["resolved_row"] == {
        "index": 1,
        "identity": "Fixture cue two",
    }
    assert result["viewport_delta"] == {
        "before": {
            "first_visible_index": 0,
            "last_visible_index": 1,
            "visible_rows": [
                {"index": 0, "identity": "Fixture cue one"},
                {"index": 1, "identity": "Fixture cue two"},
            ],
            "identity_strategy": {
                "kind": "configured_column",
                "column": "Phrase",
                "derived": True,
            },
            "row_count": 3,
        },
        "after": {
            "first_visible_index": 0,
            "last_visible_index": 1,
            "visible_rows": [
                {"index": 0, "identity": "Fixture cue one"},
                {"index": 1, "identity": "Fixture cue two"},
            ],
            "identity_strategy": {
                "kind": "configured_column",
                "column": "Phrase",
                "derived": True,
            },
            "row_count": 3,
        },
        "comparison": {
            "first_visible_index_changed": False,
            "last_visible_index_changed": False,
            "viewport_moved": False,
            "direction": "unchanged",
        },
    }
    assert ("snapshot", {"automation_id": "CueGrid"}) in backend.calls


@pytest.mark.asyncio
async def test_grid_snapshot_and_assert_rows_require_cell_text_evidence() -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    snapshot = await snapshot_grid(
        backend,
        selector,
        columns=["Start", "End", "Character", "Phrase"],
    )
    asserted = await assert_grid_rows(
        backend,
        selector,
        rows=[
            {
                "index": 0,
                "contains": {
                    "Start": "00:00:01.0",
                    "End": "00:00:03.0",
                    "Character": "Narrator",
                    "Phrase": "Fixture cue one",
                },
            },
            {
                "index": 1,
                "contains": {
                    "Start": "00:00:04.0",
                    "End": "00:00:06.0",
                    "Character": "ALICE",
                    "Phrase": "Fixture cue two",
                },
            },
        ],
    )

    assert snapshot["visible_rows"][1]["selected"] is True
    assert snapshot["visible_rows"][0]["cells"]["Phrase"] == "Fixture cue one"
    assert asserted["status"] == "PASS"
    assert asserted["asserted"] is True
    assert asserted["matched_rows"] == [0, 1]


@pytest.mark.asyncio
async def test_grid_assert_rows_fails_without_cells_even_when_row_count_exists() -> None:
    class RowCountOnlyBackend:
        async def grid_visible_rows(self, selector: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "PASS",
                "row_count": 2,
                "visible_rows": [
                    {"index": 0, "name": "CueViewModel"},
                    {"index": 1, "name": "CueViewModel"},
                ],
            }

    result = await assert_grid_rows(
        RowCountOnlyBackend(),
        {"automation_id": "CueGrid"},
        rows=[{"index": 0, "contains": {"Phrase": "Fixture cue one"}}],
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "row cell evidence unavailable"
    assert result["failed_rows"][0]["index"] == 0


@pytest.mark.asyncio
async def test_grid_helpers_pass_through_ambiguous_without_mutating_selection() -> None:
    class AmbiguousBackend(FakeGridBackend):
        async def grid_select_range(
            self,
            selector: dict[str, Any],
            start_index: int,
            end_index: int,
        ) -> dict[str, Any]:
            return {
                "status": "AMBIGUOUS",
                "reason": "row lookup ambiguous",
                "selection_mutated": False,
            }

    result = await select_grid_range(
        AmbiguousBackend(),
        {"automation_id": "VirtualizedGrid"},
        100,
        105,
    )

    assert result["status"] == "AMBIGUOUS"
    assert result["selection_mutated"] is False


@pytest.mark.asyncio
async def test_pywinauto_backend_returns_unsupported_for_grid_helpers() -> None:
    backend = PywinautoBackend()

    visible = await read_grid_visible_rows(backend, {"automation_id": "CueGrid"})
    click_result = await backend.grid_click_row(
        {"automation_id": "CueGrid"},
        1,
        column="Phrase",
        columns=["Phrase"],
    )
    right_click_result = await backend.grid_right_click_row(
        {"automation_id": "CueGrid"},
        1,
        column="Phrase",
        columns=["Phrase"],
    )
    double_click_result = await backend.grid_double_click_row(
        {"automation_id": "CueGrid"},
        1,
        column="Phrase",
        columns=["Phrase"],
    )

    assert visible["status"] == "UNSUPPORTED"
    assert visible["unsupported"] is True
    assert visible["backend"] == "pywinauto"
    assert click_result["status"] == "UNSUPPORTED"
    assert click_result["unsupported"] is True
    assert click_result["backend"] == "pywinauto"
    assert right_click_result["status"] == "UNSUPPORTED"
    assert right_click_result["unsupported"] is True
    assert right_click_result["backend"] == "pywinauto"
    assert double_click_result["status"] == "UNSUPPORTED"
    assert double_click_result["unsupported"] is True
    assert double_click_result["backend"] == "pywinauto"


@pytest.mark.asyncio
async def test_pywinauto_backend_returns_unsupported_for_grid_ensure_visible() -> None:
    backend = PywinautoBackend()

    result = await backend.grid_ensure_visible(
        {"automation_id": "CueGrid"},
        row_key="Cue 042",
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        columns=["PhraseId"],
        max_scrolls=11,
        scroll_settle_ms=30,
    )

    assert result["status"] == "UNSUPPORTED"
    assert result["unsupported"] is True
    assert result["backend"] == "pywinauto"


@pytest.mark.asyncio
async def test_flaui_backend_forwards_grid_helpers_to_bridge() -> None:
    backend = _make_flaui()
    backend._client.call.return_value = {"status": "PASS", "visible_rows": []}

    selector = {"automation_id": "CueGrid"}
    result = await backend.grid_visible_rows(selector)
    await backend.grid_selected_rows(selector, columns=["Phrase"])
    await backend.grid_select_range(selector, 0, 2)
    await backend.grid_click_row(selector, 1, column="Phrase", columns=["Phrase"])
    await backend.grid_right_click_row(selector, 1, column="Phrase", columns=["Phrase"])
    await backend.grid_double_click_row(selector, 1, column="Phrase", columns=["Phrase"])
    await backend.grid_assert_range(selector, 0, 2)
    await snapshot_grid(backend, selector, columns=["Start"])
    await assert_grid_rows(
        backend,
        selector,
        columns=["Start"],
        rows=[{"index": 0, "contains": {"Start": "00:00:01.0"}}],
    )

    assert result["status"] == "PASS"
    calls = backend._client.call.await_args_list
    assert calls[0].args[0] == "grid_visible_rows"
    assert calls[0].args[1]["selector"]["automationId"] == "CueGrid"
    assert calls[1].args[0] == "grid_selected_rows"
    assert calls[1].args[1]["columns"] == ["Phrase"]
    assert calls[2].args[0] == "grid_select_range"
    assert calls[2].args[1]["start_index"] == 0
    assert calls[2].args[1]["end_index"] == 2
    assert calls[3].args[0] == "grid_click_row"
    assert calls[3].args[1]["row_index"] == 1
    assert calls[3].args[1]["column"] == "Phrase"
    assert calls[3].args[1]["columns"] == ["Phrase"]
    assert calls[4].args[0] == "grid_right_click_row"
    assert calls[4].args[1]["row_index"] == 1
    assert calls[4].args[1]["column"] == "Phrase"
    assert calls[4].args[1]["columns"] == ["Phrase"]
    assert calls[5].args[0] == "grid_double_click_row"
    assert calls[5].args[1]["row_index"] == 1
    assert calls[5].args[1]["column"] == "Phrase"
    assert calls[5].args[1]["columns"] == ["Phrase"]
    assert calls[7].args[0] == "grid_snapshot"
    assert calls[7].args[1]["columns"] == ["Start"]
    assert calls[8].args[0] == "grid_assert_rows"
    assert calls[8].args[1]["columns"] == ["Start"]


@pytest.mark.asyncio
async def test_flaui_backend_forwards_grid_ensure_visible_to_bridge() -> None:
    backend = _make_flaui()
    backend._client.call.return_value = {
        "status": "PASS",
        "already_visible": False,
        "resolved_row": {"index": 0, "row_index": 42, "cells": {"PhraseId": "Cue 042"}},
    }

    result = await backend.grid_ensure_visible(
        {"automation_id": "CueGrid"},
        row_key="Cue 042",
        identity={"column": "PhraseId"},
        rows={"visible_only": True},
        columns=["PhraseId"],
        max_scrolls=11,
        scroll_settle_ms=30,
    )

    assert result["status"] == "PASS"
    backend._client.call.assert_awaited_once()
    call = backend._client.call.await_args
    assert call.args == (
        "grid_ensure_visible",
        {
            "selector": {"automationId": "CueGrid"},
            "row_key": "Cue 042",
            "identity": {"column": "PhraseId"},
            "rows": {"visible_only": True},
            "columns": ["PhraseId"],
            "max_scrolls": 11,
            "scroll_settle_ms": 30,
        },
    )
    assert call.kwargs["timeout"] > BRIDGE_DEFAULT_CALL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_ui_grid_tool_is_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tool_names = {tool.name for tool in await server.list_tools()}

    assert "ui_grid" in tool_names


def test_bridge_grid_assertion_requires_exact_selected_range() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )

    assert "selectedRows.Count == expected.Count" in command
    assert "expected.All(selectedRows.Contains)" not in command


def test_bridge_grid_rejects_invalid_control_type_and_prevalidates_selection_range() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )

    assert "Unknown DataGrid controlType" in command
    validation_index = command.index("itemPatterns.Add(itemPattern)")
    mutation_index = command.index("itemPattern.Select()")
    assert validation_index < mutation_index


def test_bridge_double_click_row_captures_row_evidence_before_click_side_effects() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )

    start = command.index("public static JsonNode DoubleClickRow(")
    evidence_index = command.index("var rowEvidence = BuildRow(", start)
    click_index = command.index("ClickCommands.DoubleClick(", start)
    output_index = command.index('["row"] = rowEvidence', start)

    assert evidence_index < click_index < output_index


def test_bridge_find_element_rejects_invalid_control_type() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "ElementCommands.cs").read_text(
        encoding="utf-8",
    )

    assert 'throw new ArgumentException($"Unknown controlType: {controlType}")' in command
    assert "!Enum.IsDefined(typeof(ControlType), ct)" in command
    assert command.count("ParseControlType(controlType)") >= 3
    assert "if (!string.IsNullOrWhiteSpace(controlType) &&" not in command


def test_bridge_grid_builds_cell_text_evidence_for_rows() -> None:
    grid_command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )
    ensure_visible_command = (
        PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.EnsureVisible.cs"
    ).read_text(
        encoding="utf-8",
    )
    command = grid_command + "\n" + ensure_visible_command
    handler = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(
        encoding="utf-8",
    )

    assert "public static partial class GridCommands" in grid_command
    assert "public static partial class GridCommands" in ensure_visible_command
    assert '["grid_snapshot"] = GridCommands.Snapshot' in handler
    assert '["grid_assert_rows"] = GridCommands.AssertRows' in handler
    assert '["grid_click_row"] = GridCommands.ClickRow' in handler
    assert '["grid_right_click_row"] = GridCommands.RightClickRow' in handler
    assert '["grid_double_click_row"] = GridCommands.DoubleClickRow' in handler
    assert '["grid_ensure_visible"] = GridCommands.EnsureVisible' in handler
    assert '["cells"]' in command
    assert '["grid_bounds"] = SafeRect(grid)' in command
    assert '["bounds"] = SafeRect(row)' in command
    assert '["row_index"] = RowIndex(row, index)' in command
    assert "private static int RowIndex(" in command
    assert "ReadCellText" in command
    assert "new Grid(grid.FrameworkAutomationElement)" in command
    assert "gridElement.ColumnHeaders" in command
    assert "public static JsonNode ClickRow(" in command
    assert "public static JsonNode RightClickRow(" in command
    assert "public static JsonNode DoubleClickRow(" in command
    assert "public static JsonNode EnsureVisible(" in command
    assert "ScrollIntoView()" in command
    assert "ScanForRowWithBoundedScroll" in command
    assert "DefaultMaxEnsureVisibleScrolls" in command
    assert "ReadBoundedInt(" in command
    assert "ScrollAmount.LargeDecrement" in command
    assert "ScrollAmount.LargeIncrement" in command
    assert "ScanDownward(" in ensure_visible_command
    assert "TryScrollToVerticalStart(scrollPattern, settleMs)" in ensure_visible_command
    assert "SetScrollPercent(ScrollPatternConstants.NoScroll, 0)" in ensure_visible_command
    assert '"rewind_to_start"' in ensure_visible_command
    assert "grid rewind-to-start failed before bounded scroll scan" in ensure_visible_command
    assert "var rewindLimit = maxScrolls + currentDownwardScrolls;" in ensure_visible_command
    current_scan_index = ensure_visible_command.index('"current_downward"')
    rewind_index = ensure_visible_command.index("TryScrollToVerticalStart(scrollPattern, settleMs)")
    rewound_scan_index = ensure_visible_command.index('"rewound_downward"')
    assert current_scan_index < rewind_index < rewound_scan_index
    assert 'var rowKey = StringValue(@params?["row_key"]);' in ensure_visible_command
    assert "private static string? StringValue(JsonNode? node)" in ensure_visible_command
    assert 'var value = StringValue(row[key]);' in ensure_visible_command
    assert "var value = StringValue(cell.Value);" in ensure_visible_command
    assert 'var text = StringValue(cell["text"]);' in ensure_visible_command
    assert 'var value = StringValue(cell["value"]);' in ensure_visible_command
    assert 'var value = row[key]?.GetValue<string>();' not in ensure_visible_command
    assert "var value = cell.Value?.GetValue<string>();" not in ensure_visible_command
    assert 'var text = cell["text"]?.GetValue<string>();' not in ensure_visible_command
    assert 'var value = cell["value"]?.GetValue<string>();' not in ensure_visible_command
    assert "if (before is null || after is null)\n            return false;" in (
        ensure_visible_command.replace("\r\n", "\n")
    )
    assert "SafeVerticallyScrollable(scrollPattern)" in command
    assert "grid vertical scrollability evidence unavailable" in command
    assert "grid row ScrollItemPattern failed" in command
    assert "scrollItemPattern.ScrollIntoView();" in command
    assert "grid row identity is not present after bounded scroll scan" in command
    assert "grid bounded ensure-visible scan requires ScrollPattern" in command
    assert "grid row identity is not present in realized rows" not in command
    assert "if (!scrollPattern.VerticallyScrollable.Value)" not in command
    assert "row bounds are empty" in command
    assert "new GridRow(row.FrameworkAutomationElement)" in command
    assert "gridRow.Cells" in command
    assert "CellColumnIndex(cell, ordinal)" in command
    assert "return pattern.Row.Value;" in command
    assert "ReadDescendantCellText" in command
    assert "IsLikelyCellPlaceholder" in command
    assert "CellPlaceholderSubstrings" in command
    assert "cells.Array.Count >= expectedColumns" in command
    assert "ReadGridFindTimeout" in command
    assert command.count("IsLikelyCellPlaceholder(text)") >= 3
    assert "FindGridWithRetry" in command
    assert "row cell evidence unavailable" in command
    assert "ColumnsFromAssertions(expectedRows)" in command
    assert "string.Equals(actualValue, expectedValue, StringComparison.Ordinal)" in command
    assert "CellKey(cell, currentOrdinal, columns, headers)" in command
    direct_children_index = command.index("row.FindAllChildren()")
    descendant_fallback_index = command.index("row.FindAllDescendants()")
    ordinal_index = command.index("var currentOrdinal = ordinal")
    increment_index = command.index("ordinal++;", ordinal_index)
    read_text_index = command.index("ReadCellText(cell)", ordinal_index)
    blank_continue_index = command.index("continue;", read_text_index)
    pattern_value_index = command.index("var text = SafeString(() => cell.Value);")
    pattern_read_text_index = command.index("text = ReadCellText(cell)", pattern_value_index)
    pattern_placeholder_guard_index = command.index(
        "IsLikelyCellPlaceholder(text)",
        pattern_value_index,
    )
    assert direct_children_index < descendant_fallback_index
    assert ordinal_index < increment_index < blank_continue_index
    assert pattern_value_index < pattern_placeholder_guard_index < pattern_read_text_index


def test_bridge_multi_select_uses_data_grid_rows_not_raw_children() -> None:
    command = (
        PROJECT_ROOT / "bridge" / "Commands" / "SelectionCommands.cs"
    ).read_text(encoding="utf-8")

    assert "SelectionTargets(container, automation)" in command
    assert "FindAllChildren(RowCondition(automation))" in command
    assert "FindAllDescendants(RowCondition(automation))" in command
    assert "var children = container.FindAllChildren();" not in command

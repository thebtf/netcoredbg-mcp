"""DataGrid helper evidence tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
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

    async def grid_selected_rows(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("selected", dict(selector)))
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


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


@pytest.mark.asyncio
async def test_grid_helpers_return_visible_selected_and_range_evidence_without_mutating_selector(
) -> None:
    backend = FakeGridBackend()
    selector = {"automation_id": "CueGrid"}

    visible = await read_grid_visible_rows(backend, selector)
    selected = await read_grid_selected_rows(backend, selector)
    selected_range = await select_grid_range(backend, selector, 1, 2)
    asserted_range = await assert_grid_range(backend, selector, 1, 2)

    assert selector == {"automation_id": "CueGrid"}
    assert visible["visible_rows"][0]["automation_id"] == "Row_0"
    assert selected["selected_rows"] == [
        {"index": 1, "automation_id": "Row_1", "name": "B"}
    ]
    assert selected_range["selected_range"] == {"start": 1, "end": 2}
    assert asserted_range["asserted"] is True


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

    assert visible["status"] == "UNSUPPORTED"
    assert visible["unsupported"] is True
    assert visible["backend"] == "pywinauto"


@pytest.mark.asyncio
async def test_flaui_backend_forwards_grid_helpers_to_bridge() -> None:
    backend = _make_flaui()
    backend._client.call.return_value = {"status": "PASS", "visible_rows": []}

    selector = {"automation_id": "CueGrid"}
    result = await backend.grid_visible_rows(selector)
    await backend.grid_selected_rows(selector)
    await backend.grid_select_range(selector, 0, 2)
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
    assert calls[2].args[0] == "grid_select_range"
    assert calls[2].args[1]["start_index"] == 0
    assert calls[2].args[1]["end_index"] == 2
    assert calls[4].args[0] == "grid_snapshot"
    assert calls[4].args[1]["columns"] == ["Start"]
    assert calls[5].args[0] == "grid_assert_rows"
    assert calls[5].args[1]["columns"] == ["Start"]


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


def test_bridge_grid_rejects_invalid_control_type_and_prevalidates_selection_range(
) -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )

    assert "Unknown DataGrid controlType" in command
    validation_index = command.index("itemPatterns.Add(itemPattern)")
    mutation_index = command.index("itemPattern.Select()")
    assert validation_index < mutation_index


def test_bridge_grid_builds_cell_text_evidence_for_rows() -> None:
    command = (PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.cs").read_text(
        encoding="utf-8",
    )
    handler = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(
        encoding="utf-8",
    )

    assert '["grid_snapshot"] = GridCommands.Snapshot' in handler
    assert '["grid_assert_rows"] = GridCommands.AssertRows' in handler
    assert '["cells"]' in command
    assert "ReadCellText" in command
    assert "new Grid(grid.FrameworkAutomationElement)" in command
    assert "gridElement.ColumnHeaders" in command
    assert "new GridRow(row.FrameworkAutomationElement)" in command
    assert "gridRow.Cells" in command
    assert "CellColumnIndex(cell, ordinal)" in command
    assert "ReadDescendantCellText" in command
    assert "IsLikelyCellPlaceholder" in command
    assert "CellPlaceholderSubstrings" in command
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

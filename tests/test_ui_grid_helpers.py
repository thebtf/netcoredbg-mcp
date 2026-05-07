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
    read_grid_selected_rows,
    read_grid_visible_rows,
    select_grid_range,
)
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend


class FakeGridBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def grid_visible_rows(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("visible", dict(selector)))
        return {
            "status": "PASS",
            "row_count": 3,
            "visible_rows": [
                {"index": 0, "automation_id": "Row_0", "name": "A"},
                {"index": 1, "automation_id": "Row_1", "name": "B"},
            ],
        }

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

    result = await backend.grid_visible_rows({"automation_id": "CueGrid"})

    assert result["status"] == "PASS"
    backend._client.call.assert_awaited_once_with(
        "grid_visible_rows",
        {"selector": {"automationId": "CueGrid"}},
    )


@pytest.mark.asyncio
async def test_ui_grid_tool_is_registered(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tool_names = {tool.name for tool in await server.list_tools()}

    assert "ui_grid" in tool_names


def test_bridge_grid_assertion_requires_exact_selected_range() -> None:
    command = Path("bridge/Commands/GridCommands.cs").read_text(encoding="utf-8")

    assert "selectedRows.Count == expected.Count" in command
    assert "expected.All(selectedRows.Contains)" not in command

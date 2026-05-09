"""Scoped list item helper tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from netcoredbg_mcp.ui.list_items import invoke_list_item, toggle_list_item_child

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeListBackend:
    def __init__(self) -> None:
        self.invoked: list[dict[str, Any]] = []
        self.toggled: list[dict[str, Any]] = []

    async def list_invoke_item(
        self,
        selector: dict[str, Any],
        item: dict[str, Any],
        invoke: str = "default",
    ) -> dict[str, Any]:
        self.invoked.append({"selector": dict(selector), "item": dict(item), "invoke": invoke})
        return {
            "status": "PASS",
            "invoked": True,
            "item": dict(item),
            "method": "InvokePattern",
        }

    async def list_toggle_item_child(
        self,
        selector: dict[str, Any],
        item: dict[str, Any],
        child: dict[str, Any],
        target_state: str | None = None,
    ) -> dict[str, Any]:
        self.toggled.append(
            {
                "selector": dict(selector),
                "item": dict(item),
                "child": dict(child),
                "target_state": target_state,
            }
        )
        return {
            "status": "PASS",
            "toggled": True,
            "item": dict(item),
            "child": dict(child),
            "new_state": target_state or "On",
        }


@pytest.mark.asyncio
async def test_scoped_list_item_helpers_keep_item_and_child_targets_together() -> None:
    backend = FakeListBackend()
    selector = {"automation_id": "CharactersListBox"}

    invoked = await invoke_list_item(
        backend,
        selector,
        item={"name": "ALICE"},
        invoke="enter",
    )
    toggled = await toggle_list_item_child(
        backend,
        selector,
        item={"name": "ALICE"},
        child={"automation_id": "CharGender", "control_type": "CheckBox"},
        target_state="On",
    )

    assert invoked["status"] == "PASS"
    assert toggled["status"] == "PASS"
    assert backend.invoked[0]["item"] == {"name": "ALICE"}
    assert backend.toggled[0]["item"] == {"name": "ALICE"}
    assert backend.toggled[0]["child"]["automation_id"] == "CharGender"


def test_bridge_list_toggle_searches_child_from_resolved_item_not_main_window() -> None:
    command_path = PROJECT_ROOT / "bridge" / "Commands" / "ListCommands.cs"
    command = command_path.read_text(encoding="utf-8")
    handler = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(
        encoding="utf-8",
    )

    assert '["list_invoke_item"] = ListCommands.InvokeItem' in handler
    assert '["list_toggle_item_child"] = ListCommands.ToggleItemChild' in handler
    start = command.index("ToggleItemChild")
    end = command.find("private static", start + 1)
    method_body = command[start : end if end != -1 else len(command)]
    assert "ResolveListItem" in method_body
    assert "ResolveChild(itemElement" in method_body
    assert "mainWindow.FindFirstDescendant" not in method_body
    assert "maxAttempts = 10" in method_body
    assert 'info["status"] = string.IsNullOrWhiteSpace(targetState)' in method_body

    child_start = command.index("private static AutomationElement ResolveChild")
    child_end = command.find("private static", child_start + 1)
    child_body = command[child_start : child_end if child_end != -1 else len(command)]
    assert "itemElement.FindFirstDescendant" in child_body

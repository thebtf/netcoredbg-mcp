"""Focus assertion helper tests."""

from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.ui.focus import assert_focus


class FakeFocusBackend:
    def __init__(self, focused: bool) -> None:
        self.focused = focused
        self.calls: list[dict[str, Any]] = []

    async def assert_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(selector))
        return {
            "status": "PASS" if self.focused else "FAIL",
            "focused": self.focused,
            "selector": dict(selector),
            "reason": "focus matched" if self.focused else "focus outside selector",
        }


@pytest.mark.asyncio
async def test_focus_assertion_passes_when_backend_reports_matching_focus() -> None:
    backend = FakeFocusBackend(focused=True)

    result = await assert_focus(backend, {"automation_id": "CueDataGrid"})

    assert result["status"] == "PASS"
    assert result["focused"] is True
    assert backend.calls == [{"automation_id": "CueDataGrid"}]


@pytest.mark.asyncio
async def test_focus_assertion_fails_when_focus_is_outside_selector() -> None:
    result = await assert_focus(
        FakeFocusBackend(focused=False),
        {"automation_id": "CueDataGrid"},
    )

    assert result["status"] == "FAIL"
    assert result["focused"] is False
    assert result["reason"] == "focus outside selector"

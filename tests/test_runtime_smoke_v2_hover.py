from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.actions import (
    ActionContext,
    accepted_action_kinds,
    dispatch_action,
)


def _valid_hover_evidence() -> dict[str, Any]:
    return {
        "status": "PASS",
        "resolvedSelector": {
            "criterion": "automationId",
            "automationId": "hoverTrigger",
        },
        "target": {
            "automationId": "hoverTrigger",
            "name": "Hover trigger",
            "controlType": "Custom",
        },
        "matchCount": 1,
        "targetRootHwnd": 101,
        "targetProcessId": 42,
        "foregroundHwndBefore": 101,
        "foregroundHwndAfter": 101,
        "foregroundVerified": True,
        "focusBefore": {
            "automationId": "hoverFocusSentinel",
            "name": "Arm",
            "controlType": "Button",
        },
        "focusAfter": {
            "automationId": "hoverFocusSentinel",
            "name": "Arm",
            "controlType": "Button",
        },
        "focusUnchanged": True,
        "targetRect": {"x": 10, "y": 20, "width": 100, "height": 40},
        "requestedPoint": {"x": 60, "y": 40},
        "actualPointer": {"x": 60, "y": 40},
        "hitElement": {
            "automationId": "hoverTriggerText",
            "name": "Hover trigger",
            "controlType": "Text",
        },
        "hitRelation": "descendant",
        "underPointer": True,
        "hovered": True,
        "click": False,
        "button": "none",
        "timeoutMs": 1250,
        "elapsedMs": 12,
        "pointerMutationState": "moved",
    }


class HoverAdapter:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or _valid_hover_evidence()

    async def hover(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return deepcopy(self.result)


def _context(
    adapter: HoverAdapter | None,
    *,
    input_policy: dict[str, Any] | None = None,
    extra_adapters: dict[str, Any] | None = None,
) -> ActionContext:
    adapters = dict(extra_adapters or {})
    if adapter is not None:
        adapters["ui.hover"] = adapter.hover
    return ActionContext(
        service_adapters=adapters,
        clock=lambda: 1.0,
        input_policy=input_policy,
    )


@pytest.mark.asyncio
async def test_ui_hover_action_is_registered_routes_exact_payload_and_marks_runner_input() -> None:
    adapter = HoverAdapter()
    selector = {"automation_id": "hoverTrigger", "root_id": "hoverRegion"}

    result = await dispatch_action(
        {"kind": "ui.hover", "selector": selector, "timeout_ms": 1250},
        _context(adapter),
    )

    assert "ui.hover" in accepted_action_kinds()
    assert adapter.calls == [{"selector": selector, "timeout_ms": 1250}]
    assert result["status"] == "PASS"
    assert result["route"] == "hover"
    assert result["selector"] == selector
    assert result["timeout_ms"] == 1250
    assert result["foregroundVerified"] is True
    assert result["focusUnchanged"] is True
    assert result["underPointer"] is True
    assert result["runner_input"] == {
        "source": "runner_injected",
        "kind": "ui.hover",
        "window": "action",
        "route": "hover",
    }


@pytest.mark.parametrize("timeout_ms", [True, False, 0, 30001, 2.5, "5000"])
@pytest.mark.asyncio
async def test_ui_hover_action_rejects_invalid_timeout_before_adapter(
    timeout_ms: object,
) -> None:
    adapter = HoverAdapter()

    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": {"automation_id": "hoverTrigger"},
            "timeout_ms": timeout_ms,
        },
        _context(adapter),
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "invalid ui.hover timeout_ms"
    assert result["requested"] == {"timeout_ms": timeout_ms}
    assert adapter.calls == []
    assert "runner_input" not in result


@pytest.mark.asyncio
async def test_ui_hover_action_level_no_global_input_blocks_before_adapter() -> None:
    adapter = HoverAdapter()
    selector = {"automation_id": "hoverTrigger", "root_id": "hoverRegion"}

    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": selector,
            "timeout_ms": 1250,
            "input_policy": {"no_global_input": True},
        },
        _context(adapter),
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "global input prohibited by no_global_input policy"
    assert result["input_classification"] == "REQUIRES_GLOBAL_INPUT"
    assert result["physical_fallback_attempted"] is False
    assert result["operator_isolated"] is True
    assert result["requested_target"] == selector
    assert result["requested"]["target"] == selector
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_ui_hover_plan_level_no_global_input_blocks_before_adapter() -> None:
    adapter = HoverAdapter()
    selector = {"automation_id": "hoverTrigger", "root_id": "hoverRegion"}

    result = await dispatch_action(
        {"kind": "ui.hover", "selector": selector, "timeout_ms": 1250},
        _context(adapter, input_policy={"no_global_input": True}),
    )

    assert result["status"] == "BLOCKED"
    assert result["input_policy"] == {"no_global_input": True}
    assert result["input_classification"] == "REQUIRES_GLOBAL_INPUT"
    assert result["operator_isolated"] is True
    assert result["requested_target"] == selector
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_ui_hover_missing_adapter_lists_accepted_names() -> None:
    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": {"automation_id": "hoverTrigger"},
            "timeout_ms": 1250,
        },
        _context(None, extra_adapters={"ui.find_element": lambda: None}),
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "service adapter not available"
    assert result["requested"] == {"adapter": "ui.hover"}
    assert result["accepted"] == {"adapter_names": ["ui.find_element"]}
    assert result["next_step"]
    assert "runner_input" not in result


@pytest.mark.asyncio
async def test_ui_hover_action_blocks_malformed_pass_and_never_marks_runner_input() -> None:
    evidence = _valid_hover_evidence()
    evidence.pop("actualPointer")
    adapter = HoverAdapter(evidence)

    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": {"automation_id": "hoverTrigger"},
            "timeout_ms": 1250,
        },
        _context(adapter),
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "hover backend returned malformed success evidence"
    assert "actualPointer" in result["result"]["missing"]
    assert result["result"]["evidence"]
    assert "runner_input" not in result


@pytest.mark.asyncio
async def test_ui_hover_action_fails_contradictory_pass_and_never_marks_runner_input() -> None:
    evidence = _valid_hover_evidence()
    evidence["focusUnchanged"] = False
    adapter = HoverAdapter(evidence)

    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": {"automation_id": "hoverTrigger"},
            "timeout_ms": 1250,
        },
        _context(adapter),
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "hover evidence contradicted the required contract"
    assert result["result"]["contradictions"]
    assert result["result"]["evidence"]
    assert "runner_input" not in result


@pytest.mark.asyncio
async def test_ui_hover_action_preserves_structured_bridge_timeout() -> None:
    adapter = HoverAdapter(
        {
            "status": "BLOCKED",
            "reason": "FlaUI bridge hover timed out before acknowledgement",
            "phase": "bridge_timeout",
            "timeoutMs": 1250,
            "pointerMutationState": "unknown",
            "requested": {
                "selector": {"automation_id": "hoverTrigger"},
                "timeout_ms": 1250,
            },
            "accepted": {"timeout_ms": "integer from 1 to 30000"},
            "next_step": "Re-establish foreground and retry.",
        }
    )

    result = await dispatch_action(
        {
            "kind": "ui.hover",
            "selector": {"automation_id": "hoverTrigger"},
            "timeout_ms": 1250,
        },
        _context(adapter),
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "FlaUI bridge hover timed out before acknowledgement"
    assert result["result"]["phase"] == "bridge_timeout"
    assert result["result"]["pointerMutationState"] == "unknown"
    assert result["requested"] == result["result"]["requested"]
    assert result["accepted"] == result["result"]["accepted"]
    assert "runner_input" not in result

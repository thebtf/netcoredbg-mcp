from __future__ import annotations

from typing import Any

from ..blocked import build_blocked, selector_guidance
from ..evidence import attach_blocked_details


async def handle_ui_property(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    selector = dict(probe.get("selector") or {})
    property_name = str(probe.get("property") or probe.get("property_name") or "")
    result = await context.call_adapter(
        "ui.get_property",
        selector=selector,
        property_name=property_name,
    )
    if result.get("found") is False:
        blocked = build_blocked(
            reason="selector not found",
            requested={"selector": selector, "property": property_name},
            accepted=selector_guidance(),
            next_step="Run ui_get_window_tree or ui_find_element before reading property.",
        )
        return {
            "name": str(probe.get("name") or property_name or "ui.property"),
            "kind": "ui.property",
            "status": "BLOCKED",
            "value": None,
            **blocked,
        }
    result_status = str(result.get("status", "PASS"))
    if result_status != "PASS":
        status = result_status
        output = {
            "name": str(probe.get("name") or property_name or "ui.property"),
            "kind": "ui.property",
            "status": status,
            "value": result.get("value"),
            "reason": _failure_reason(result),
        }
        attach_blocked_details(output, result)
        return output

    status = str(result.get("status", "PASS"))
    value = result.get("value")
    expected = probe.get("expected")
    if phase == "after" and "expected" in probe and status == "PASS" and value != expected:
        status = "FAIL"
    output = {
        "name": str(probe.get("name") or property_name or "ui.property"),
        "kind": "ui.property",
        "status": status,
        "value": value,
    }
    if "expected" in probe:
        output["expected"] = expected
    if status == "FAIL":
        output["reason"] = result.get("reason", "expected property value did not match")
    return output


def _failure_reason(result: dict[str, Any]) -> str:
    return str(
        result.get("reason")
        or result.get("error")
        or result.get("message")
        or f"ui.get_property returned status {result.get('status')}"
    )

from __future__ import annotations

from typing import Any

from ..blocked import build_blocked, selector_guidance


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
    if result.get("status") != "PASS" or result.get("found") is False:
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

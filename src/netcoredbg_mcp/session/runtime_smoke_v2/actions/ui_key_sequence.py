from __future__ import annotations

from typing import Any

from ..blocked import build_blocked, selector_guidance


def _keys_as_string(raw_keys: Any) -> str:
    if isinstance(raw_keys, str):
        return raw_keys
    if isinstance(raw_keys, list):
        return "".join(str(key) for key in raw_keys)
    return str(raw_keys or "")


async def handle_ui_key_sequence(
    action: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    started = context.clock()
    selector = dict(action.get("selector") or {})
    keys = _keys_as_string(action.get("keys"))
    find_result = await context.call_adapter("ui.find_element", selector=selector)
    if find_result.get("status") != "PASS" or find_result.get("found") is False:
        blocked = build_blocked(
            reason="selector not found",
            requested={"selector": selector},
            accepted=selector_guidance(),
            next_step="Run ui_get_window_tree or ui_find_element with name/control_type.",
        )
        return {
            "status": "BLOCKED",
            **blocked,
            "duration_ms": context.elapsed_ms(started),
            "route": "key_sequence",
        }

    await context.call_adapter("ui.set_focus", selector=selector)
    await context.call_adapter("ui.send_keys_focused", keys=keys)
    return {
        "status": "PASS",
        "route": "key_sequence",
        "selector": selector,
        "keys": keys,
        "duration_ms": context.elapsed_ms(started),
    }

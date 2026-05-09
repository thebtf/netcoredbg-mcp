from __future__ import annotations

from collections.abc import Mapping
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
    selector, invalid_selector = _selector_from_action(action)
    if invalid_selector is not None:
        return {
            **invalid_selector,
            "duration_ms": context.elapsed_ms(started),
            "route": "key_sequence",
        }
    keys = _keys_as_string(action.get("keys"))
    find_result = await context.call_adapter("ui.find_element", selector=selector)
    if find_result.get("found") is False:
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
    if find_result.get("status") != "PASS":
        return _adapter_failure_result(
            find_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="key_sequence",
            default_reason="failed to find target element",
        )

    focus_result = await context.call_adapter("ui.set_focus", selector=selector)
    if focus_result.get("status") != "PASS":
        return _adapter_failure_result(
            focus_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="key_sequence",
            default_reason="failed to focus target element",
        )

    send_result = await context.call_adapter("ui.send_keys_focused", keys=keys)
    if send_result.get("status") != "PASS":
        failed = _adapter_failure_result(
            send_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="key_sequence",
            default_reason="failed to send key sequence",
        )
        failed["keys"] = keys
        return failed
    return {
        "status": "PASS",
        "route": "key_sequence",
        "selector": selector,
        "keys": keys,
        "duration_ms": context.elapsed_ms(started),
    }


def _selector_from_action(
    action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_selector = action.get("selector")
    if raw_selector is None:
        return {}, None
    if not isinstance(raw_selector, Mapping):
        blocked = build_blocked(
            reason="invalid selector payload",
            requested={"selector": raw_selector},
            accepted={"selector_type": "object"},
            next_step="Provide selector as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    return dict(raw_selector), None


def _adapter_failure_result(
    result: dict[str, Any],
    *,
    selector: dict[str, Any],
    duration_ms: int,
    route: str,
    default_reason: str,
) -> dict[str, Any]:
    return {
        "status": str(result.get("status") or "FAIL"),
        "reason": str(result.get("reason") or result.get("error") or default_reason),
        "selector": selector,
        "route": route,
        "duration_ms": duration_ms,
        "result": result,
    }

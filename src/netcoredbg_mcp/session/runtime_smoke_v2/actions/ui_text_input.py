from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..blocked import build_blocked, selector_guidance

_SENDKEYS_LITERAL_ESCAPES = {
    "+": "{+}",
    "^": "{^}",
    "%": "{%}",
    "{": "{{}",
    "}": "{}}",
    "(": "{(}",
    ")": "{)}",
    "~": "{~}",
}


async def handle_ui_text_type_replace_selection(
    action: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    started = context.clock()
    selector, invalid_selector = _selector_from_action(action)
    if invalid_selector is not None:
        return {
            **invalid_selector,
            "duration_ms": context.elapsed_ms(started),
            "route": "text_type_replace_selection",
        }
    text, invalid_text = _text_from_action(action)
    if invalid_text is not None:
        return {
            **invalid_text,
            "duration_ms": context.elapsed_ms(started),
            "route": "text_type_replace_selection",
        }

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
            "route": "text_type_replace_selection",
        }
    if not _is_success(find_result):
        return _adapter_failure_result(
            find_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to find target element",
        )

    focus_result = await context.call_adapter("ui.set_focus", selector=selector)
    if not _is_success(focus_result):
        return _adapter_failure_result(
            focus_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to focus target element",
        )

    keys = replacement_keys(text)
    send_result = await context.call_adapter("ui.send_keys_focused", keys=keys)
    if not _is_success(send_result):
        failed = _adapter_failure_result(
            send_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to send key sequence",
        )
        failed["keys"] = keys
        return failed

    read_result = await context.call_adapter("ui.text.read", selector=selector)
    if not _is_success(read_result):
        failed = _adapter_failure_result(
            read_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to verify typed text",
        )
        failed["keys"] = keys
        return failed

    actual = _text_value(read_result)
    if actual != text:
        return {
            "status": "FAIL",
            "reason": "post-read text mismatch",
            "route": "text_type_replace_selection",
            "selector": selector,
            "keys": keys,
            "expected": text,
            "actual": actual,
            "duration_ms": context.elapsed_ms(started),
            "result": read_result,
        }

    return {
        "status": "PASS",
        "route": "text_type_replace_selection",
        "selector": selector,
        "keys": keys,
        "text": text,
        "verified": True,
        "duration_ms": context.elapsed_ms(started),
        "result": read_result,
    }


def escape_sendkeys_literal(text: str) -> str:
    return "".join(_SENDKEYS_LITERAL_ESCAPES.get(char, char) for char in text)


def replacement_keys(text: str) -> str:
    if text == "":
        return "^a{BACKSPACE}"
    return f"^a{escape_sendkeys_literal(text)}"


def _text_value(result: dict[str, Any]) -> str:
    value = result.get("text", result.get("value", ""))
    return "" if value is None else str(value)


def _is_success(result: dict[str, Any]) -> bool:
    return str(result.get("status", "PASS")).upper() in {"PASS", "OK", "SUCCESS"}


def _selector_from_action(
    action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_selector = action.get("selector")
    if raw_selector is None:
        blocked = build_blocked(
            reason="missing selector payload",
            requested={"selector": None},
            accepted={"selector_type": "object"},
            next_step="Provide selector as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    if not isinstance(raw_selector, Mapping):
        blocked = build_blocked(
            reason="invalid selector payload",
            requested={"selector": raw_selector},
            accepted={"selector_type": "object"},
            next_step="Provide selector as an object.",
        )
        return {}, {"status": "BLOCKED", **blocked}
    return dict(raw_selector), None


def _text_from_action(action: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    raw_text = action.get("text")
    if not isinstance(raw_text, str):
        blocked = build_blocked(
            reason="invalid text payload",
            requested={"text": raw_text},
            accepted={"text": "string"},
            next_step="Provide text as the literal string to type.",
        )
        return "", {"status": "BLOCKED", **blocked}
    return raw_text, None


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

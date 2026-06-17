from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..blocked import build_blocked, selector_guidance

_SELECT_ALL_KEYS = "^a"
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

    select_result = await context.call_adapter("ui.send_keys_focused", keys=_SELECT_ALL_KEYS)
    if not _is_success(select_result):
        failed = _adapter_failure_result(
            select_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to select existing text",
        )
        failed["keys"] = replacement_keys(text)
        failed["selection_keys"] = _SELECT_ALL_KEYS
        return failed

    state_result = await context.call_adapter("ui.text.get_state", selector=selector)
    if not _is_success(state_result):
        failed = _adapter_failure_result(
            state_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to verify select-all precondition",
        )
        failed["keys"] = replacement_keys(text)
        failed["selection_keys"] = _SELECT_ALL_KEYS
        return failed

    precondition = _select_all_precondition(state_result)
    if precondition["selected"] is not True:
        return {
            "status": "BLOCKED",
            "reason": "select-all precondition failed",
            "route": "text_type_replace_selection",
            "selector": selector,
            "keys": replacement_keys(text),
            "selection_keys": _SELECT_ALL_KEYS,
            "precondition": precondition,
            "duration_ms": context.elapsed_ms(started),
            "result": state_result,
        }

    input_keys = replacement_input_keys(text)
    send_result = await context.call_adapter("ui.send_keys_focused", keys=input_keys)
    if not _is_success(send_result):
        failed = _adapter_failure_result(
            send_result,
            selector=selector,
            duration_ms=context.elapsed_ms(started),
            route="text_type_replace_selection",
            default_reason="failed to send replacement text",
        )
        failed["keys"] = replacement_keys(text)
        failed["selection_keys"] = _SELECT_ALL_KEYS
        failed["input_keys"] = input_keys
        failed["precondition"] = precondition
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
        failed["keys"] = replacement_keys(text)
        failed["selection_keys"] = _SELECT_ALL_KEYS
        failed["input_keys"] = input_keys
        failed["precondition"] = precondition
        return failed

    actual = _text_value(read_result)
    if actual != text:
        return {
            "status": "FAIL",
            "reason": "post-read text mismatch",
            "route": "text_type_replace_selection",
            "selector": selector,
            "keys": replacement_keys(text),
            "selection_keys": _SELECT_ALL_KEYS,
            "input_keys": input_keys,
            "precondition": precondition,
            "expected": text,
            "actual": actual,
            "duration_ms": context.elapsed_ms(started),
            "result": read_result,
        }

    return {
        "status": "PASS",
        "route": "text_type_replace_selection",
        "selector": selector,
        "keys": replacement_keys(text),
        "selection_keys": _SELECT_ALL_KEYS,
        "input_keys": input_keys,
        "text": text,
        "verified": True,
        "precondition": precondition,
        "duration_ms": context.elapsed_ms(started),
        "result": read_result,
    }


def escape_sendkeys_literal(text: str) -> str:
    return "".join(_SENDKEYS_LITERAL_ESCAPES.get(char, char) for char in text)


def replacement_keys(text: str) -> str:
    return f"{_SELECT_ALL_KEYS}{replacement_input_keys(text)}"


def replacement_input_keys(text: str) -> str:
    if text == "":
        return "{BACKSPACE}"
    return escape_sendkeys_literal(text)


def _text_value(result: dict[str, Any]) -> str:
    value = result.get("text", result.get("value", ""))
    return "" if value is None else str(value)


def _select_all_precondition(result: dict[str, Any]) -> dict[str, Any]:
    text, text_available = _text_evidence(result)
    if not text_available:
        selection = result.get("selection")
        return {
            "selected": False,
            "reason": "TextBox text evidence unavailable",
            "expected": {"text": "bounded TextBox text or value evidence"},
            "actual": _selection_evidence(selection),
            "state": _bounded_state_evidence(result),
        }
    text_length = len(text)
    expected = {
        "selection_start": 0,
        "selection_end": text_length,
        "selection_length": text_length,
        "text_length": text_length,
    }
    selection = result.get("selection")
    if not isinstance(selection, Mapping):
        return {
            "selected": False,
            "reason": "TextBox selection evidence unavailable",
            "expected": expected,
            "actual": {"text_length": text_length},
            "state": _bounded_state_evidence(result),
        }

    actual = _selection_evidence(selection)
    actual["text_length"] = text_length
    selected = (
        actual["selection_start"] == 0
        and actual["selection_end"] == text_length
        and actual["selection_length"] == text_length
    )
    return {
        "selected": selected,
        "expected": expected,
        "actual": actual,
        "state": _bounded_state_evidence(result),
    }


def _text_evidence(result: dict[str, Any]) -> tuple[str, bool]:
    for key in ("text", "value"):
        if key in result and result[key] is not None:
            return str(result[key]), True
    return "", False


def _is_success(result: dict[str, Any]) -> bool:
    return str(result.get("status", "PASS")).upper() in {"PASS", "OK", "SUCCESS"}


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
            return int(parsed) if parsed.is_integer() else None
        except ValueError:
            return None
    return None


def _bounded_state_evidence(result: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "status",
        "text",
        "value",
        "source",
        "selection",
        "caret_index",
        "focus_within",
        "enabled",
        "visible",
        "selector",
    )
    return {key: result[key] for key in allowed_keys if key in result}


def _selection_evidence(selection: Any) -> dict[str, Any]:
    if not isinstance(selection, Mapping):
        return {}
    start = _optional_int(
        selection.get("start", selection.get("selection_start", selection.get("selectionStart")))
    )
    length = _optional_int(
        selection.get(
            "length",
            selection.get("selection_length", selection.get("selectionLength")),
        )
    )
    end = _optional_int(
        selection.get("end", selection.get("selection_end", selection.get("selectionEnd")))
    )
    if end is None and start is not None and length is not None:
        end = start + length
    if length is None and start is not None and end is not None:
        length = max(0, end - start)
    if start is None and end is not None and length is not None:
        start = max(0, end - length)
    return {
        "selection_start": start,
        "selection_end": end,
        "selection_length": length,
    }


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
    adapter_status = str(result.get("status") or "FAIL").upper()
    return {
        "status": _terminal_failure_status(adapter_status),
        "reason": str(result.get("reason") or result.get("error") or default_reason),
        "selector": selector,
        "route": route,
        "duration_ms": duration_ms,
        "adapter_status": adapter_status,
        "result": result,
    }


def _terminal_failure_status(adapter_status: str) -> str:
    if adapter_status in {"FAIL", "BLOCKED", "IMPASSE"}:
        return adapter_status
    if adapter_status in {"UNSUPPORTED", "INVALID_SETUP"}:
        return "BLOCKED"
    return "FAIL"

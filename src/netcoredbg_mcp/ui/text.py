"""TextBox state and selection evidence helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TEXTBOX_STATE_FIELDS = ["focus", "selection", "value", "text", "enabled", "visible"]


async def read_textbox_state(backend: Any, selector: dict[str, Any]) -> dict[str, Any]:
    """Read bounded TextBox state evidence through the active backend."""
    state_reader = getattr(backend, "textbox_state", None)
    if callable(state_reader):
        try:
            result = await state_reader(dict(selector))
        except Exception as exc:
            return _blocked_state_exception(
                selector,
                reason=f"TextBox state reader raised exception: {exc}",
                backend="connected UI backend supporting textbox_state",
            )
        return _bounded_textbox_state(selector, result)

    query_ui = getattr(backend, "query_ui", None)
    if not callable(query_ui):
        return {
            "status": "BLOCKED",
            "reason": "TextBox state evidence unavailable",
            "requested": {"selector": dict(selector), "fields": list(TEXTBOX_STATE_FIELDS)},
            "accepted": {"backend": "connected UI backend supporting query_ui"},
            "next_step": "Use the FlaUI bridge backend for TextBox state evidence.",
        }

    try:
        result = await query_ui(
            dict(selector),
            fields=list(TEXTBOX_STATE_FIELDS),
            max_results=1,
        )
    except Exception as exc:
        return _blocked_state_exception(
            selector,
            reason=f"TextBox state query_ui raised exception: {exc}",
            backend="connected UI backend supporting query_ui",
        )
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "TextBox state backend returned non-object result",
            "selector": dict(selector),
            "result": _strip_unbounded(result),
        }
    status = str(result.get("status", "PASS")).upper()
    if status not in {"PASS", "OK", "SUCCESS"}:
        bounded = _strip_unbounded(result)
        if isinstance(bounded, dict):
            bounded["status"] = _textbox_state_status(result)
            bounded.setdefault("selector", dict(selector))
            return bounded
        return {
            "status": "BLOCKED",
            "reason": "TextBox state backend did not pass",
            "selector": dict(selector),
            "result": bounded,
        }

    elements = result.get("elements")
    if not isinstance(elements, list) or not elements:
        return {
            "status": "BLOCKED",
            "reason": "selector not found",
            "requested": {"selector": dict(selector)},
            "accepted": {"fields": list(TEXTBOX_STATE_FIELDS)},
            "next_step": "Inspect the fixture UI tree and update the selector.",
            "result": _strip_unbounded(result),
        }
    element = elements[0]
    if not isinstance(element, dict):
        return {
            "status": "FAIL",
            "reason": "TextBox state element evidence was not an object",
            "selector": dict(selector),
            "result": _strip_unbounded(element),
        }
    state = dict(element)
    state["status"] = "PASS"
    state.setdefault("source", _selection_source(state) or "ui_query")
    return _bounded_textbox_state(selector, state)


async def assert_text_selection(
    backend: Any,
    selector: dict[str, Any],
    *,
    selection_start: int,
    selection_end: int,
) -> dict[str, Any]:
    """Assert a TextBox selection range using bounded TextBox state evidence."""
    state = await read_textbox_state(backend, selector)
    if str(state.get("status", "PASS")).upper() not in {"PASS", "OK", "SUCCESS"}:
        return state

    expected = {"start": selection_start, "end": selection_end}
    actual = state.get("selection")
    if not isinstance(actual, dict):
        return {
            "status": "BLOCKED",
            "matched": False,
            "reason": "TextBox selection evidence unavailable",
            "expected_selection": expected,
            "actual_selection": {},
            "selector": dict(selector),
            "state": _strip_unbounded(state),
        }

    actual_start = _optional_int(actual.get("start"))
    actual_end = _optional_int(actual.get("end"))
    if actual_start is None or actual_end is None:
        actual_selection = _strip_unbounded(actual)
        if isinstance(actual_selection, dict) and "source" not in actual_selection:
            source = state.get("source")
            if source is not None:
                actual_selection["source"] = _strip_unbounded(source)
        return {
            "status": "BLOCKED",
            "matched": False,
            "reason": "TextBox selection evidence unavailable",
            "expected_selection": expected,
            "actual_selection": actual_selection,
            "selector": dict(selector),
            "state": _strip_unbounded(state),
        }
    matched = actual_start == selection_start and actual_end == selection_end
    return {
        "status": "PASS" if matched else "FAIL",
        "matched": matched,
        **({} if matched else {"reason": "selection mismatch"}),
        "expected_selection": expected,
        "actual_selection": _strip_unbounded(actual),
        "selector": dict(selector),
    }


def _bounded_textbox_state(selector: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "FAIL",
            "reason": "TextBox state backend returned non-object result",
            "selector": dict(selector),
            "result": _strip_unbounded(result),
        }

    bounded: dict[str, Any] = {}
    bounded["status"] = _textbox_state_status(result)
    for key in ("text", "value", "source", "found", "reason", "error", "unsupported", "backend"):
        if key in result:
            bounded[key] = _strip_unbounded(result[key])
    selection = _normalized_selection(result.get("selection"))
    if selection is not None:
        bounded["selection"] = selection
    caret_index = _first_present_int(result, "caret_index", "caretIndex")
    if caret_index is None and selection is not None:
        caret_index = _optional_int(selection.get("end"))
    if caret_index is not None:
        bounded["caret_index"] = caret_index
    focus_within = _first_present_bool(result, "focus_within", "focusWithin", "focus")
    if focus_within is not None:
        bounded["focus_within"] = focus_within
    for key in ("enabled", "visible"):
        if key in result:
            bounded[key] = bool(result[key])
    if "requested" in result:
        bounded["requested"] = _strip_unbounded(result["requested"])
    if "accepted" in result:
        bounded["accepted"] = _strip_unbounded(result["accepted"])
    if "next_step" in result:
        bounded["next_step"] = str(result["next_step"])
    bounded["selector"] = dict(selector)
    return bounded


def _blocked_state_exception(
    selector: dict[str, Any],
    *,
    reason: str,
    backend: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "requested": {"selector": dict(selector), "fields": list(TEXTBOX_STATE_FIELDS)},
        "accepted": {"backend": backend},
        "next_step": "Inspect UI backend or bridge transport diagnostics.",
    }


def _textbox_state_status(result: Mapping[str, Any]) -> str:
    if result.get("unsupported") is True:
        return "BLOCKED"
    status = str(result.get("status") or "PASS")
    if status.upper() == "UNSUPPORTED":
        return "BLOCKED"
    return status


def _normalized_selection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    selection: dict[str, Any] = {}
    start = _first_present_int(value, "start", "selection_start", "selectionStart")
    end = _first_present_int(value, "end", "selection_end", "selectionEnd")
    length = _first_present_int(value, "length", "selection_length", "selectionLength")
    if end is None and start is not None and length is not None:
        end = start + length
    if length is None and start is not None and end is not None:
        length = max(0, end - start)
    if start is not None:
        selection["start"] = start
    if end is not None:
        selection["end"] = end
    if length is not None:
        selection["length"] = length
    selected_text = _first_present(value, "selected_text", "selectedText")
    if selected_text is not None:
        selection["selected_text"] = str(selected_text)
    for key in ("supported", "selected", "source", "range_count"):
        if key in value:
            selection[key] = _strip_unbounded(value[key])
    return selection or _strip_unbounded(dict(value))


def _selection_source(result: Mapping[str, Any]) -> str | None:
    selection = result.get("selection")
    if not isinstance(selection, Mapping):
        return None
    source = selection.get("source")
    return str(source) if source else None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _first_present_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    return _optional_int(_first_present(mapping, *keys))


def _first_present_bool(mapping: Mapping[str, Any], *keys: str) -> bool | None:
    value = _first_present(mapping, *keys)
    if value is None:
        return None
    return bool(value)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _strip_unbounded(value: Any) -> Any:
    unbounded_keys = {"full_tree", "raw_tree", "ui_tree", "window_tree", "tree"}
    if isinstance(value, Mapping):
        return {
            str(key): _strip_unbounded(item)
            for key, item in value.items()
            if str(key) not in unbounded_keys and not str(key).endswith("_tree")
        }
    if isinstance(value, list):
        return [_strip_unbounded(item) for item in value]
    return value

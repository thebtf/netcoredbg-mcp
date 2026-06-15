from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .result_envelope import compact_value

BLOCKED_DETAIL_KEYS = ("reason", "requested", "accepted", "next_step")
BACKEND_RESULT_KEY = "backend_result"
_MISSING = object()

_TREE_DUMP_KEYS = frozenset(
    {
        "children",
        "descendants",
        "raw_tree",
        "raw_window_tree",
        "tree",
        "ui_tree",
        "window_tree",
    }
)


def attach_blocked_details(
    output: dict[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    for key in BLOCKED_DETAIL_KEYS:
        if key in result:
            output[key] = result[key]
    backend_result = _backend_result(result)
    if backend_result is not _MISSING:
        output[BACKEND_RESULT_KEY] = compact_backend_result(backend_result)
    return output


def blocked_details_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {
        "reason": str(record.get("reason") or "blocked"),
        "requested": _mapping_detail(record.get("requested")),
        "accepted": _mapping_detail(record.get("accepted")),
        "next_step": str(record.get("next_step") or "Inspect the blocked transition."),
    }
    backend_result = _backend_result(record)
    if backend_result is not _MISSING:
        details[BACKEND_RESULT_KEY] = compact_backend_result(backend_result)
    return details


def compact_backend_result(value: Any) -> Any:
    return compact_value(compact_evidence(value))


def compact_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _TREE_DUMP_KEYS or key_text.endswith("_tree"):
                continue
            compact[key_text] = compact_evidence(item)
        return compact
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    return value


def _mapping_detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact = compact_value(compact_evidence(value))
    return compact if isinstance(compact, dict) else {}


def _backend_result(record: Mapping[str, Any]) -> Any:
    if BACKEND_RESULT_KEY in record:
        return record[BACKEND_RESULT_KEY]
    if "result" in record:
        return record["result"]
    return _MISSING

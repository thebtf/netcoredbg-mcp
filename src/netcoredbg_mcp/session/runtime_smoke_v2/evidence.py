from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BLOCKED_DETAIL_KEYS = ("reason", "requested", "accepted", "next_step")

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
    return output


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

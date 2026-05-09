from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._substituter import render_template_value


def render_case_id(pattern: str, record: dict[str, Any]) -> str:
    return str(render_template_value(pattern, record))


def selector_from_record(
    record: dict[str, Any],
    *,
    selector_key: str = "selector",
    automation_key: str = "control",
) -> dict[str, Any]:
    selector = record.get(selector_key)
    if isinstance(selector, dict):
        return deepcopy(selector)
    automation_id = record.get(automation_key)
    if automation_id is None:
        automation_id = record.get("automation_id")
    return {"automation_id": str(automation_id or "")}


def keyboard_action(record: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any]:
    action = {
        "kind": "ui.key_sequence",
        "selector": deepcopy(selector),
        "keys": str(record.get("keys") or "{SPACE}"),
    }
    if "realize" in record:
        action["realize"] = bool(record["realize"])
    return action

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._helpers import keyboard_action, render_case_id, selector_from_record
from ._substituter import render_template_value


def render_toggle_setting_ab(
    record: dict[str, Any],
    id_pattern: str,
) -> dict[str, Any]:
    selector = selector_from_record(record)
    consumer_selector = (
        deepcopy(record["consumer_selector"])
        if isinstance(record.get("consumer_selector"), dict)
        else deepcopy(selector)
    )
    expected = record.get("expected", record.get("value"))
    expression = render_template_value(
        str(record.get("setting_expression") or record.get("expression") or "{id}"),
        record,
    )
    return {
        "id": render_case_id(id_pattern, record),
        "transitions": [
            {
                "action": keyboard_action(record, selector),
                "probes": [
                    {
                        "kind": "debug.evaluate",
                        "name": "setting",
                        "expression": expression,
                        "expected": expected,
                    },
                    {
                        "kind": "ui.property",
                        "name": "consumer",
                        "selector": consumer_selector,
                        "property": str(record.get("property") or "ToggleState"),
                        "expected": expected,
                    },
                ],
            }
        ],
    }

from __future__ import annotations

from typing import Any

from ._helpers import keyboard_action, render_case_id, selector_from_record
from ._substituter import render_template_value


def render_setting_ab_row_effect(
    record: dict[str, Any],
    id_pattern: str,
) -> dict[str, Any]:
    selector = selector_from_record(record)
    grid_selector = selector_from_record(
        record,
        selector_key="grid_selector",
        automation_key="grid",
    )
    expected = record.get("expected", record.get("value"))
    expression = render_template_value(
        str(record.get("setting_expression") or record.get("expression") or "{id}"),
        record,
    )
    row_expected = record.get("row_expected")
    if not isinstance(row_expected, dict):
        row_expected = {
            "row_index": record.get("row_index"),
            "expected": expected,
        }
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
                        "kind": "ui.grid",
                        "name": "row_effect",
                        "selector": grid_selector,
                        "rows": [row_expected],
                        "columns": [
                            str(column)
                            for column in record.get("columns") or []
                        ],
                    },
                ],
            }
        ],
    }

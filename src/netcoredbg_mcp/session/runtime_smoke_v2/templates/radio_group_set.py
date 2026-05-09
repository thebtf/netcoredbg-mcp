from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._helpers import keyboard_action, render_case_id
from ._substituter import render_template_value


def render_radio_group_set(
    record: dict[str, Any],
    id_pattern: str,
) -> dict[str, Any]:
    controls = [
        dict(control)
        for control in record.get("controls") or []
        if isinstance(control, dict)
    ]
    target_value = record.get("target", record.get("value"))
    target = _target_control(controls, target_value)
    target_selector = _selector(target)
    expression = render_template_value(
        str(record.get("setting_expression") or record.get("expression") or "{id}"),
        record,
    )
    probes = [
        {
            "kind": "debug.evaluate",
            "name": "selected_value",
            "expression": expression,
            "expected": target_value,
        },
        {
            "kind": "ui.property",
            "name": f"{target_value}_selected",
            "selector": target_selector,
            "property": str(record.get("property") or "IsChecked"),
            "expected": True,
        },
    ]
    if bool(record.get("assert_siblings", True)):
        probes.extend(
            {
                "kind": "ui.property",
                "name": f"{control.get('value', control.get('id', index))}_selected",
                "selector": _selector(control),
                "property": str(record.get("property") or "IsChecked"),
                "expected": False,
            }
            for index, control in enumerate(controls)
            if control is not target
        )
    return {
        "id": render_case_id(id_pattern, record),
        "transitions": [
            {
                "action": keyboard_action(record, target_selector),
                "probes": probes,
            }
        ],
    }


def _target_control(
    controls: list[dict[str, Any]],
    target_value: Any,
) -> dict[str, Any]:
    for control in controls:
        if control.get("value") == target_value or control.get("id") == target_value:
            return control
    return {"value": target_value, "automation_id": target_value}


def _selector(control: dict[str, Any]) -> dict[str, Any]:
    selector = control.get("selector")
    if isinstance(selector, dict):
        return deepcopy(selector)
    return {"automation_id": str(control.get("automation_id") or control.get("id") or "")}

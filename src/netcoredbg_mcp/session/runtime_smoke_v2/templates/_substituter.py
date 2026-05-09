from __future__ import annotations

import re
from copy import deepcopy
from string import Formatter
from typing import Any

_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FULL_FIELD = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class TemplateRenderError(ValueError):
    pass


def render_template_value(value: Any, record: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, record)
    if isinstance(value, list):
        return [render_template_value(item, record) for item in value]
    if isinstance(value, dict):
        return {key: render_template_value(item, record) for key, item in value.items()}
    return deepcopy(value)


def _render_string(template: str, record: dict[str, Any]) -> Any:
    full_match = _FULL_FIELD.match(template)
    if full_match:
        field_name = full_match.group(1)
        _validate_known_field(field_name, record)
        return deepcopy(record[field_name])

    rendered = []
    formatter = Formatter()
    try:
        parsed = list(formatter.parse(template))
    except ValueError as exc:
        raise TemplateRenderError(f"malformed template {template!r}: {exc}") from exc
    for literal, field_name, format_spec, conversion in parsed:
        rendered.append(literal)
        if field_name is None:
            continue
        _validate_field_syntax(field_name, format_spec or "", conversion)
        _validate_known_field(field_name, record)
        rendered.append(_stringify(record[field_name]))
    return "".join(rendered)


def _validate_field_syntax(
    field_name: str,
    format_spec: str,
    conversion: str | None,
) -> None:
    if format_spec or conversion or not _FIELD_NAME.match(field_name):
        raise TemplateRenderError(f"unsupported placeholder syntax: {field_name}")


def _validate_known_field(field_name: str, record: dict[str, Any]) -> None:
    if field_name not in record:
        raise TemplateRenderError(f"unknown placeholder: {field_name}")


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

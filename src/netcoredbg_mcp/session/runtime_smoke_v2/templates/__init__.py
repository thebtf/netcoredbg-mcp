from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._substituter import TemplateRenderError as TemplateRenderError
from .radio_group_set import render_radio_group_set
from .setting_ab_row_effect import render_setting_ab_row_effect
from .toggle_setting_ab import render_toggle_setting_ab

TemplateRenderer = Callable[[dict[str, Any], str], dict[str, Any]]


class UnknownTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateDefinition:
    name: str
    default_id_pattern: str
    render: TemplateRenderer


_TEMPLATES: dict[str, TemplateDefinition] = {
    "radio-group-set": TemplateDefinition(
        name="radio-group-set",
        default_id_pattern="{id}.{value}",
        render=render_radio_group_set,
    ),
    "setting-ab-row-effect": TemplateDefinition(
        name="setting-ab-row-effect",
        default_id_pattern="{id}.row-{row_index}.{value}",
        render=render_setting_ab_row_effect,
    ),
    "toggle-setting-ab": TemplateDefinition(
        name="toggle-setting-ab",
        default_id_pattern="{id}.{value}",
        render=render_toggle_setting_ab,
    ),
}


def accepted_template_names() -> list[str]:
    return sorted(_TEMPLATES)


def get_template(name: str) -> TemplateDefinition:
    try:
        return _TEMPLATES[name]
    except KeyError as exc:
        raise UnknownTemplateError(name) from exc

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .templates import TemplateRenderError, UnknownTemplateError, get_template


def expand_generated_cases(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    generate = plan.get("generate")
    if generate is None:
        return [], []
    if not isinstance(generate, dict):
        return [], ["generate must be an object"]

    template_name = str(generate.get("template") or "")
    try:
        template = get_template(template_name)
    except UnknownTemplateError:
        return [], [f"generate.template is not accepted: {template_name}"]

    matrix = generate.get("matrix", [])
    if not isinstance(matrix, list):
        return [], ["generate.matrix must be a list"]

    id_pattern = str(generate.get("id_pattern") or template.default_id_pattern)
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw_record in enumerate(matrix):
        if not isinstance(raw_record, dict):
            errors.append(f"generate.matrix[{index}] must be an object")
            continue
        record = deepcopy(raw_record)
        render_record = {**record, "index": index}
        try:
            case = template.render(render_record, id_pattern)
        except TemplateRenderError as exc:
            errors.append(f"generate.matrix[{index}]: {exc}")
            continue
        case["rendered_from"] = record
        cases.append(case)
    return cases, errors

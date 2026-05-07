"""Runtime smoke plan schema metadata and normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "netcoredbg.runtime_smoke.v1"

ACCEPTED_SCHEMA_VALUES = (SCHEMA_VERSION,)
ACCEPTED_TOP_LEVEL_KEYS = (
    "schema",
    "name",
    "description",
    "preflight",
    "launch",
    "freshness",
    "steps",
    "actions",
    "assertions",
    "evidence",
    "cleanup",
    "teardown",
    "budgets",
    "stop_on_first_failed_assertion",
)


@dataclass(frozen=True)
class OperationSchema:
    """Public operation schema mapped to an internal runner operation."""

    internal_name: str
    required_fields: tuple[str, ...] = ()


OPERATION_SCHEMAS: dict[str, OperationSchema] = {
    "launch": OperationSchema("launch"),
    "debug.hygiene_preflight": OperationSchema("debug_hygiene_preflight"),
    "debug.freshness.verify": OperationSchema("verify_debug_freshness"),
    "debug.output_checkpoint": OperationSchema("output_checkpoint"),
    "debug.output_assert_since": OperationSchema(
        "output_assert_since",
        ("checkpoint",),
    ),
    "instrumentation.group_clear": OperationSchema(
        "instrumentation_group_clear",
        ("name",),
    ),
    "ui.key_sequence": OperationSchema(
        "ui_key_sequence",
        ("selector", "keys"),
    ),
    "ui.grid.snapshot": OperationSchema(
        "ui.grid.snapshot",
        ("selector",),
    ),
    "ui.grid.select_range": OperationSchema(
        "ui.grid.select_range",
        ("selector", "start_index", "end_index"),
    ),
    "ui.grid.assert_rows": OperationSchema(
        "ui.grid.assert_rows",
        ("selector", "rows"),
    ),
    "ui.list.invoke_item": OperationSchema(
        "ui.list.invoke_item",
        ("selector", "item"),
    ),
    "ui.list.toggle_item_child": OperationSchema(
        "ui.list.toggle_item_child",
        ("selector", "item", "child"),
    ),
    "ui.focus.assert": OperationSchema(
        "ui.focus.assert",
        ("selector",),
    ),
    "ui.text.assert": OperationSchema(
        "ui.text.assert",
        ("selector",),
    ),
    "ui.invoke": OperationSchema(
        "ui.invoke",
        ("selector",),
    ),
    "fixture.restore": OperationSchema(
        "fixture.restore",
        ("path",),
    ),
}


def schema_help_fields() -> dict[str, Any]:
    """Return additive schema-help fields for invalid-plan diagnostics."""

    return {
        "accepted_schema_values": list(ACCEPTED_SCHEMA_VALUES),
        "accepted_top_level_keys": list(ACCEPTED_TOP_LEVEL_KEYS),
        "accepted_operation_names": sorted(OPERATION_SCHEMAS),
        "operation_aliases": {
            op_name: schema.internal_name
            for op_name, schema in sorted(OPERATION_SCHEMAS.items())
        },
        "operation_required_fields": {
            op_name: list(schema.required_fields)
            for op_name, schema in sorted(OPERATION_SCHEMAS.items())
        },
    }


def validate_plan(plan: Any) -> list[str]:
    """Validate the runtime smoke plan shape without touching the target app."""

    if not isinstance(plan, dict):
        return ["plan must be an object"]

    errors: list[str] = []
    _validate_schema_value(plan, errors)
    _validate_list_fields(plan, errors)
    _validate_object_fields(plan, errors)
    _validate_budgets(plan, errors)
    _validate_step_collections(plan, errors)
    _validate_restore_configs(plan, errors)
    return errors


def normalize_plan_step(raw: Any, default_name: str | None = None) -> dict[str, Any]:
    """Normalize legacy name/args and public op-style steps for execution."""

    if not isinstance(raw, dict):
        return {"name": default_name or "invalid_step", "args": {}}

    if "op" in raw:
        op_name = str(raw["op"])
        schema = OPERATION_SCHEMAS.get(op_name)
        internal_name = schema.internal_name if schema is not None else op_name
        return {"name": internal_name, "args": _operation_args(raw)}

    if "name" in raw:
        return {"name": str(raw["name"]), "args": dict(raw.get("args") or {})}

    return {"name": default_name or "invalid_step", "args": dict(raw)}


def _validate_schema_value(plan: dict[str, Any], errors: list[str]) -> None:
    schema = plan.get("schema")
    if schema is not None and schema not in ACCEPTED_SCHEMA_VALUES:
        accepted = ", ".join(ACCEPTED_SCHEMA_VALUES)
        errors.append(f"schema must be one of: {accepted}")


def _validate_list_fields(plan: dict[str, Any], errors: list[str]) -> None:
    for field_name in ("actions", "assertions", "evidence", "steps"):
        if field_name in plan and not isinstance(plan[field_name], list):
            errors.append(f"{field_name} must be a list")


def _validate_object_fields(plan: dict[str, Any], errors: list[str]) -> None:
    if "preflight" in plan and not isinstance(plan["preflight"], (bool, dict, list)):
        errors.append("preflight must be a boolean, object, or list")
    if "launch" in plan and not isinstance(plan["launch"], dict):
        errors.append("launch must be an object")
    if "freshness" in plan and not isinstance(plan["freshness"], dict):
        errors.append("freshness must be an object")
    for field_name in ("cleanup", "teardown"):
        if field_name in plan and not isinstance(plan[field_name], dict):
            errors.append(f"{field_name} must be an object")


def _validate_budgets(plan: dict[str, Any], errors: list[str]) -> None:
    budgets = plan.get("budgets", {})
    if budgets is not None and not isinstance(budgets, dict):
        errors.append("budgets must be an object")
        return
    if not isinstance(budgets, dict):
        return
    if "max_actions" in budgets:
        try:
            max_actions = int(budgets["max_actions"])
            if max_actions < 1:
                errors.append("budgets.max_actions must be at least 1")
        except (TypeError, ValueError):
            errors.append("budgets.max_actions must be an integer")
    if "max_elapsed_seconds" in budgets:
        try:
            max_elapsed = float(budgets["max_elapsed_seconds"])
            if max_elapsed <= 0:
                errors.append("budgets.max_elapsed_seconds must be positive")
        except (TypeError, ValueError):
            errors.append("budgets.max_elapsed_seconds must be a number")


def _validate_step_collections(plan: dict[str, Any], errors: list[str]) -> None:
    for collection_name in ("steps", "actions", "assertions", "evidence"):
        raw_items = plan.get(collection_name, [])
        if not isinstance(raw_items, list):
            continue
        for index, raw in enumerate(raw_items):
            _validate_step(collection_name, index, raw, errors)


def _validate_step(
    collection_name: str,
    index: int,
    raw: Any,
    errors: list[str],
) -> None:
    prefix = f"{collection_name}[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{prefix} must be an object")
        return
    if "args" in raw and not isinstance(raw["args"], dict):
        errors.append(f"{prefix}.args must be an object")
    if "op" not in raw:
        return

    op_name = raw["op"]
    if not isinstance(op_name, str):
        errors.append(f"{prefix}.op must be a string")
        return

    schema = OPERATION_SCHEMAS.get(op_name)
    if schema is None:
        errors.append(f"{prefix}.op is not accepted: {op_name}")
        return

    args = _operation_args(raw)
    for field_name in schema.required_fields:
        if args.get(field_name) is None:
            errors.append(f"{prefix}.{field_name} is required for op {op_name}")
    if op_name == "fixture.restore":
        _validate_restore_entry(prefix, args, errors)


def _operation_args(raw: dict[str, Any]) -> dict[str, Any]:
    args = dict(raw.get("args") or {})
    for key, value in raw.items():
        if key not in {"id", "op", "args"}:
            args[key] = value
    return args


def _validate_restore_configs(plan: dict[str, Any], errors: list[str]) -> None:
    for config_name in ("cleanup", "teardown"):
        config = plan.get(config_name)
        if not isinstance(config, dict):
            continue
        restore_files = config.get("restore_files")
        if restore_files is None:
            continue
        if not isinstance(restore_files, list):
            errors.append(f"{config_name}.restore_files must be a list")
            continue
        for index, entry in enumerate(restore_files):
            _validate_restore_entry(
                f"{config_name}.restore_files[{index}]",
                entry,
                errors,
            )


def _validate_restore_entry(
    prefix: str,
    entry: Any,
    errors: list[str],
) -> None:
    if not isinstance(entry, dict):
        errors.append(f"{prefix} must be an object")
        return

    path = entry.get("path")
    if not isinstance(path, str) or not path:
        errors.append(f"{prefix}.path is required")

    has_baseline_text = "baseline_text" in entry
    has_baseline_file = "baseline_file" in entry
    if (1 if has_baseline_text else 0) + (1 if has_baseline_file else 0) != 1:
        errors.append(
            f"{prefix} requires exactly one of baseline_text or baseline_file"
        )
        return

    if has_baseline_text and not isinstance(entry.get("baseline_text"), str):
        errors.append(f"{prefix}.baseline_text must be a string")
    if has_baseline_file:
        baseline_file = entry.get("baseline_file")
        if not isinstance(baseline_file, str) or not baseline_file:
            errors.append(f"{prefix}.baseline_file must be a non-empty string")

"""Runtime smoke plan schema metadata and normalization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "netcoredbg.runtime_smoke.v1"
SCHEMA_VERSION_V2 = "netcoredbg.runtime_smoke.v2"
DIAGNOSTIC_SCHEMA_VERSION = "netcoredbg.runtime_smoke.diagnostics.v1"
MAX_DIAGNOSTIC_JSON_BYTES = 32768
APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY = ".agent/runtime-smoke/app-diagnostics"
APP_DIAGNOSTICS_DEFAULT_FILE_NAME = "app-diagnostics.json"
APP_DIAGNOSTICS_ENV_VAR_NAMES = {
    "directory": "NETCOREDBG_MCP_APP_DIAGNOSTICS_DIR",
    "path": "NETCOREDBG_MCP_APP_DIAGNOSTICS_PATH",
    "schema": "NETCOREDBG_MCP_APP_DIAGNOSTICS_SCHEMA",
}

DIAGNOSTIC_STATUS_VALUES = ("PASS", "BLOCKED", "FAIL")
DIAGNOSTIC_EVIDENCE_LIMITS = {
    "max_text_length": 240,
    "max_list_items": 8,
    "max_json_bytes": MAX_DIAGNOSTIC_JSON_BYTES,
}
DIAGNOSTIC_REDACTION = {
    "omit_fields": [
        "access_token",
        "api_key",
        "password",
        "raw_tree",
        "screenshot_base64",
        "secret",
        "ui_tree",
        "window_tree",
    ],
    "summarize_fields": [
        "backend_result",
        "exception",
        "raw_output",
        "stack",
    ],
}
DIAGNOSTIC_REQUIRED_FIELDS = {
    "oracle_pack": ("schema", "id", "status", "checks", "limits"),
    "app_diagnostics": ("schema", "app", "status", "observations", "redaction", "limits"),
    "semantic_probe": ("schema", "kind", "status", "selector", "expect", "on_blocked"),
    "tracepoint_guardrail": (
        "schema",
        "id",
        "status",
        "mode",
        "allowed_when",
        "blocked_when",
        "unsafe_when",
        "cleanup",
    ),
}
DIAGNOSTIC_ALLOWED_KINDS = tuple(DIAGNOSTIC_REQUIRED_FIELDS)
TRACEPOINT_GUARDRAIL_MODES = ("allow", "block", "unsafe")

ACCEPTED_SCHEMA_VALUES = (SCHEMA_VERSION, SCHEMA_VERSION_V2)
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
V2_ONLY_TOP_LEVEL_KEYS = (
    "baseline",
    "generate",
    "cases",
    "diagnostics",
    "metrics_thresholds",
)
ACCEPTED_TOP_LEVEL_KEYS_V2 = ACCEPTED_TOP_LEVEL_KEYS + V2_ONLY_TOP_LEVEL_KEYS
V1_EXECUTION_KEYS = ("steps", "actions", "assertions", "evidence")


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
    "ui.grid.get_state": OperationSchema(
        "ui.grid.get_state",
        ("selector",),
    ),
    "ui.grid.ensure_visible": OperationSchema(
        "ui.grid.ensure_visible",
        ("selector", "row"),
    ),
    "ui.grid.select_range": OperationSchema(
        "ui.grid.select_range",
        ("selector", "start_index", "end_index"),
    ),
    "ui.grid.assert_range": OperationSchema(
        "ui.grid.assert_range",
        ("selector", "start_index", "end_index"),
    ),
    "ui.grid.select_row": OperationSchema(
        "ui.grid.select_row",
        ("selector", "row"),
    ),
    "ui.grid.click_row": OperationSchema(
        "ui.grid.click_row",
        ("selector", "row"),
    ),
    "ui.grid.right_click_row": OperationSchema(
        "ui.grid.right_click_row",
        ("selector", "row"),
    ),
    "ui.grid.double_click_row": OperationSchema(
        "ui.grid.double_click_row",
        ("selector", "row"),
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
    "ui.text.read": OperationSchema(
        "ui.text.read",
        ("selector",),
    ),
    "ui.text.get_state": OperationSchema(
        "ui.text.get_state",
        ("selector",),
    ),
    "ui.text.set_text": OperationSchema(
        "ui.text.set_text",
        ("selector", "text"),
    ),
    "ui.text.assert_selection": OperationSchema(
        "ui.text.assert_selection",
        ("selector",),
    ),
    "ui.get_property": OperationSchema(
        "ui.get_property",
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


def schema_help_fields(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return additive schema-help fields for invalid-plan diagnostics."""

    fields: dict[str, Any] = {
        "accepted_schema_values": list(ACCEPTED_SCHEMA_VALUES),
        "accepted_top_level_keys": list(ACCEPTED_TOP_LEVEL_KEYS),
        "accepted_operation_names": sorted(OPERATION_SCHEMAS),
        "operation_aliases": {
            op_name: schema.internal_name for op_name, schema in sorted(OPERATION_SCHEMAS.items())
        },
        "operation_required_fields": {
            op_name: list(schema.required_fields)
            for op_name, schema in sorted(OPERATION_SCHEMAS.items())
        },
    }
    if _is_v2_shaped(plan):
        fields["accepted_top_level_keys_v2"] = list(ACCEPTED_TOP_LEVEL_KEYS_V2)
    return fields


def diagnostic_schema_contract() -> dict[str, Any]:
    """Return the public diagnostic schema vocabulary used by runtime-smoke docs."""

    return {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "status_values": list(DIAGNOSTIC_STATUS_VALUES),
        "evidence_limits": _diagnostic_evidence_limits(),
        "redaction": {
            key: list(value) for key, value in DIAGNOSTIC_REDACTION.items()
        },
        "oracle_pack": {
            "required_fields": list(DIAGNOSTIC_REQUIRED_FIELDS["oracle_pack"]),
            "optional_fields": [
                "description",
                "probes",
                "sources",
                "failure_modes",
                "redaction",
            ],
            "failure_modes": [
                "missing_probe",
                "unsupported_probe",
                "unsafe_evidence",
                "DISAGREEING_SOURCES",
            ],
        },
        "app_diagnostics": {
            "required_fields": list(DIAGNOSTIC_REQUIRED_FIELDS["app_diagnostics"]),
            "optional_fields": [
                "workspace",
                "artifacts",
                "process",
                "modules",
                "wait_json",
                "poll",
                "diagnostic_launch",
            ],
            "launch_contract": {
                "env_var_names": dict(APP_DIAGNOSTICS_ENV_VAR_NAMES),
                "default_evidence_directory": APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY,
                "default_file_name": APP_DIAGNOSTICS_DEFAULT_FILE_NAME,
                "redacted_env_values": True,
            },
            "freshness_contract": {
                "app_fields": [
                    "process_id",
                    "process_name",
                    "expected_modules",
                    "require_active_process",
                ],
                "top_level_fields": [
                    "workspace",
                    "artifacts",
                    "process",
                    "modules",
                    "loaded_sources",
                ],
                "aliases": {
                    "process": ["id", "name", "expected_id", "expected_name", "require_active"],
                },
                "status_policy": {
                    "FAIL": "force diagnostic FAIL when live target contradicts declared app",
                    "WARN": "preserve diagnostic status and include incomplete evidence warning",
                    "PASS": "preserve diagnostic status with freshness proof",
                },
            },
            "wait_json_condition_contract": {
                "fields": ["jsonpath", "expected"],
                "matching": "wait until the JSONPath value equals expected",
                "failure_mode": "diagnostic JSON condition not satisfied",
            },
            "failure_modes": [
                "stale_process",
                "missing_artifact",
                "redacted_evidence",
                "diagnostic JSON not observed",
                "diagnostic JSON condition not satisfied",
            ],
        },
        "semantic_probe": {
            "required_fields": list(DIAGNOSTIC_REQUIRED_FIELDS["semantic_probe"]),
            "optional_fields": ["backend_result", "metrics", "route"],
            "failure_modes": [
                "unknown_probe",
                "selector_blocked",
                "unsafe_evidence",
            ],
            "probe_kinds": _accepted_diagnostic_probe_kinds(),
        },
        "tracepoint_guardrail": {
            "required_fields": list(DIAGNOSTIC_REQUIRED_FIELDS["tracepoint_guardrail"]),
            "optional_fields": ["owner", "ttl_seconds", "evidence_refs"],
            "mode_values": list(TRACEPOINT_GUARDRAIL_MODES),
            "failure_modes": [
                "unsafe_expression",
                "unbounded_log",
                "cleanup_unowned",
            ],
            "cleanup_operations": [
                "debug.tracepoint.remove",
                "debug.trace_log.clear",
            ],
            "runtime_limits": _tracepoint_runtime_limits(),
        },
        "evidence_pack_manifest": _evidence_pack_manifest_contract(),
    }


def _evidence_pack_manifest_contract() -> dict[str, Any]:
    from .runtime_smoke_v2.evidence_manifest import (
        MANIFEST_FILE_NAME,
        MANIFEST_SCHEMA,
        MANIFEST_SCHEMA_VERSION,
        REQUIRED_ROLLUP_FIELDS,
        REQUIRED_SOURCE_FIELDS,
        SOURCE_REF_FIELDS,
    )

    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "file_name": MANIFEST_FILE_NAME,
        "required_fields": [
            "schema",
            "schema_version",
            "pack_id",
            "run_id",
            "evidence_dir",
            "sources",
            "rollups",
        ],
        "source_required_fields": list(REQUIRED_SOURCE_FIELDS),
        "source_ref_fields": list(SOURCE_REF_FIELDS),
        "rollup_fields": list(REQUIRED_ROLLUP_FIELDS),
        "ref_policy": "refs are relative to evidence_dir and must stay inside it",
    }


def validate_evidence_pack_manifest(
    manifest: dict[str, Any],
    *,
    evidence_dir: str,
) -> list[str]:
    """Return deterministic validation errors for an evidence pack manifest."""

    from .runtime_smoke_v2.evidence_manifest import validate_pack_manifest

    return validate_pack_manifest(manifest, evidence_dir=evidence_dir)


def app_diagnostics_launch_contract(
    *,
    name: str | None = None,
    evidence_dir: str | None = None,
    file_name: str | None = None,
) -> dict[str, Any]:
    """Return public app-diagnostics launch metadata without env values."""

    base_dir = _normalize_diagnostic_path(
        evidence_dir or APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY
    )
    directory = base_dir
    if name:
        directory = f"{base_dir}/{_diagnostic_path_segment(name)}"
    resolved_file = _diagnostic_path_segment(file_name or APP_DIAGNOSTICS_DEFAULT_FILE_NAME)
    return {
        "kind": "app_diagnostics",
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "env_var_names": dict(APP_DIAGNOSTICS_ENV_VAR_NAMES),
        "evidence": {
            "directory": directory,
            "path": f"{directory}/{resolved_file}",
        },
        "redacted_env_values": True,
    }


def normalize_app_diagnostics_launch_contract(value: Any) -> dict[str, Any] | None:
    """Return a safe public app-diagnostics launch contract from untrusted input."""

    if not isinstance(value, dict):
        return None

    raw_evidence = value.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    raw_directory = evidence.get("directory")
    raw_path = evidence.get("path")

    directory = _normalize_diagnostic_path(
        str(raw_directory or APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY)
    )
    file_name = APP_DIAGNOSTICS_DEFAULT_FILE_NAME
    if raw_path:
        normalized_path = _normalize_diagnostic_path(str(raw_path))
        path_directory, separator, path_file_name = normalized_path.rpartition("/")
        if not raw_directory and separator:
            directory = path_directory
        file_name = path_file_name or APP_DIAGNOSTICS_DEFAULT_FILE_NAME

    return app_diagnostics_launch_contract(
        evidence_dir=directory,
        file_name=file_name,
    )


def app_diagnostics_launch_env(contract: dict[str, Any]) -> dict[str, str]:
    """Return the private env values for a previously advertised launch contract."""

    env_names = contract.get("env_var_names")
    evidence = contract.get("evidence")
    if not isinstance(env_names, dict) or not isinstance(evidence, dict):
        return {}
    directory_name = str(env_names.get("directory") or "")
    path_name = str(env_names.get("path") or "")
    schema_name = str(env_names.get("schema") or "")
    env: dict[str, str] = {}
    if directory_name:
        env[directory_name] = str(evidence.get("directory") or "")
    if path_name:
        env[path_name] = str(evidence.get("path") or "")
    if schema_name:
        env[schema_name] = DIAGNOSTIC_SCHEMA_VERSION
    return env


def _normalize_diagnostic_path(value: str) -> str:
    raw = str(value or APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY).replace("\\", "/")
    if len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":/":
        raw = raw[3:]
    is_absolute = raw.startswith("/")
    safe_parts = [
        _diagnostic_path_part(part)
        for part in raw.split("/")
        if part and part not in {".", ".."}
    ]
    normalized = "/".join(part for part in safe_parts if part)
    if is_absolute and normalized:
        normalized = f"/{normalized}"
    return normalized or APP_DIAGNOSTICS_DEFAULT_EVIDENCE_DIRECTORY


def _diagnostic_path_part(value: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in str(value or "")
    ).strip("-")
    return "" if safe in {"", ".", ".."} else safe


def _diagnostic_path_segment(value: str) -> str:
    raw = str(value or "")
    safe = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in raw.replace("\\", "/").rsplit("/", 1)[-1]
    ).strip(".-")
    return safe or "app-diagnostics"


def _diagnostic_evidence_limits() -> dict[str, int]:
    from .runtime_smoke_v2.result_envelope import (
        MAX_COMPACT_LIST_ITEMS,
        MAX_COMPACT_TEXT_LENGTH,
    )

    return {
        "max_text_length": MAX_COMPACT_TEXT_LENGTH,
        "max_list_items": MAX_COMPACT_LIST_ITEMS,
        "max_json_bytes": MAX_DIAGNOSTIC_JSON_BYTES,
    }


def _accepted_diagnostic_probe_kinds() -> list[str]:
    from .runtime_smoke_v2.probes import accepted_probe_kinds

    return accepted_probe_kinds()


def _tracepoint_runtime_limits() -> dict[str, float | int]:
    from .tracepoints import (
        EVALUATE_TIMEOUT_SECONDS,
        MAX_TRACE_ENTRIES,
        RATE_LIMIT_INTERVAL_SECONDS,
    )

    return {
        "max_trace_entries": MAX_TRACE_ENTRIES,
        "evaluate_timeout_seconds": EVALUATE_TIMEOUT_SECONDS,
        "rate_limit_interval_seconds": RATE_LIMIT_INTERVAL_SECONDS,
    }


def validate_diagnostic_schema_example(payload: Any, *, kind: str) -> list[str]:
    """Validate a schema-first diagnostic example without touching a target app."""

    if kind not in DIAGNOSTIC_ALLOWED_KINDS:
        accepted = ", ".join(DIAGNOSTIC_ALLOWED_KINDS)
        return [f"diagnostic kind must be one of: {accepted}"]
    if not isinstance(payload, dict):
        return [f"{kind} must be an object"]

    errors: list[str] = []
    status = payload.get("status")
    if status is not None and status not in DIAGNOSTIC_STATUS_VALUES:
        accepted = ", ".join(DIAGNOSTIC_STATUS_VALUES)
        errors.append(f"{kind}.status must be one of: {accepted}")

    schema = payload.get("schema")
    if schema is not None and schema != DIAGNOSTIC_SCHEMA_VERSION:
        errors.append(f"{kind}.schema must be {DIAGNOSTIC_SCHEMA_VERSION}")

    for field_name in DIAGNOSTIC_REQUIRED_FIELDS[kind]:
        if field_name not in payload:
            errors.append(f"{kind}.{field_name} is required")

    _validate_diagnostic_field_shapes(kind, payload, errors)
    return errors


def _validate_diagnostic_field_shapes(
    kind: str,
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    object_fields = {
        "app_diagnostics": ("app", "redaction", "limits"),
        "oracle_pack": ("limits",),
        "semantic_probe": ("selector", "expect", "on_blocked"),
        "tracepoint_guardrail": ("cleanup",),
    }
    list_fields = {
        "app_diagnostics": ("observations",),
        "oracle_pack": ("checks",),
        "tracepoint_guardrail": ("allowed_when", "blocked_when", "unsafe_when"),
    }
    for field_name in object_fields.get(kind, ()):
        if field_name in payload and not isinstance(payload[field_name], dict):
            errors.append(f"{kind}.{field_name} must be an object")
    for field_name in list_fields.get(kind, ()):
        if field_name in payload and not isinstance(payload[field_name], list):
            errors.append(f"{kind}.{field_name} must be a list")
    _validate_diagnostic_limits(kind, payload, errors)
    _validate_unsafe_diagnostic_evidence(kind, payload, errors)
    if kind == "oracle_pack":
        _validate_oracle_pack_schema(payload, errors)
    elif kind == "app_diagnostics":
        _validate_app_diagnostics_schema(payload, errors)
    elif kind == "semantic_probe":
        _validate_semantic_probe_schema(payload, errors)
    elif kind == "tracepoint_guardrail":
        _validate_tracepoint_guardrail_schema(payload, errors)
    if kind == "tracepoint_guardrail" and "mode" in payload:
        mode = payload["mode"]
        if mode not in TRACEPOINT_GUARDRAIL_MODES:
            accepted = ", ".join(TRACEPOINT_GUARDRAIL_MODES)
            errors.append(f"{kind}.mode must be one of: {accepted}")


def _validate_diagnostic_limits(
    kind: str,
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    limits = payload.get("limits")
    if not isinstance(limits, dict):
        return
    for field_name, max_value in _diagnostic_evidence_limits().items():
        value = limits.get(field_name)
        if value is None:
            errors.append(f"{kind}.limits.{field_name} is required")
        elif isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{kind}.limits.{field_name} must be an integer")
        elif value < 0:
            errors.append(f"{kind}.limits.{field_name} must be >= 0")
        elif value > max_value:
            errors.append(f"{kind}.limits.{field_name} must be <= {max_value}")


def _validate_unsafe_diagnostic_evidence(
    kind: str,
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    omitted = set(DIAGNOSTIC_REDACTION["omit_fields"])

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                item_path = f"{path}.{key}"
                if key in omitted:
                    errors.append(f"{item_path} must be omitted or summarized")
                visit(item, item_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(payload, kind)


def _validate_oracle_pack_schema(payload: dict[str, Any], errors: list[str]) -> None:
    checks = payload.get("checks")
    if isinstance(checks, list):
        for index, check in enumerate(checks):
            prefix = f"oracle_pack.checks[{index}]"
            if not isinstance(check, dict):
                errors.append(f"{prefix} must be an object")
                continue
            _require_fields(prefix, check, ("id", "probe", "expect", "on_blocked"), errors)
            _validate_probe_name(f"{prefix}.probe", check.get("probe"), errors)
            if "expect" in check and not isinstance(check["expect"], dict):
                errors.append(f"{prefix}.expect must be an object")
            if "on_blocked" in check and not isinstance(check["on_blocked"], dict):
                errors.append(f"{prefix}.on_blocked must be an object")
            _validate_next_step(prefix, check.get("on_blocked"), errors)

    sources = payload.get("sources")
    if sources is None:
        return
    if not isinstance(sources, list):
        errors.append("oracle_pack.sources must be a list")
        return
    seen_source_ids: set[str] = set()
    for index, source in enumerate(sources):
        prefix = f"oracle_pack.sources[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{prefix} must be an object")
            continue
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id:
            errors.append(f"{prefix}.id is required")
        elif source_id in seen_source_ids:
            errors.append(f"{prefix}.id duplicates earlier source id: {source_id}")
        else:
            seen_source_ids.add(source_id)
        source_probe = source.get("probe")
        if not isinstance(source_probe, dict):
            errors.append(f"{prefix}.probe must be an object")
            continue
        source_kind = source_probe.get("kind")
        if source_kind is None:
            errors.append(f"{prefix}.probe.kind is required")
        elif source_kind == "oracle_pack":
            errors.append(f"{prefix}.probe.kind must not be oracle_pack")
        else:
            _validate_probe_name(f"{prefix}.probe.kind", source_kind, errors)


def _validate_app_diagnostics_schema(payload: dict[str, Any], errors: list[str]) -> None:
    observations = payload.get("observations")
    if not isinstance(observations, list):
        observations = []
    for index, observation in enumerate(observations):
        prefix = f"app_diagnostics.observations[{index}]"
        if not isinstance(observation, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _validate_optional_status(prefix, observation.get("status"), errors)
        if observation.get("status") == "BLOCKED":
            _require_blocked_diagnostics(prefix, observation, errors)
    _validate_app_diagnostics_freshness_shapes(payload, errors)
    if payload.get("wait_json") is not None and payload.get("poll") is not None:
        errors.append("app_diagnostics.wait_json and app_diagnostics.poll are mutually exclusive")
    _validate_wait_json_schema(payload, errors, field_name="wait_json")
    _validate_wait_json_schema(payload, errors, field_name="poll")


def _validate_app_diagnostics_freshness_shapes(
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    app = payload.get("app")
    if isinstance(app, dict):
        process_id = app.get("process_id") or app.get("expected_process_id")
        if process_id is not None and (
            isinstance(process_id, bool) or not isinstance(process_id, int)
        ):
            errors.append("app_diagnostics.app.process_id must be an integer")
        expected_modules = app.get("expected_modules")
        if expected_modules is not None and not _is_string_list(expected_modules):
            errors.append("app_diagnostics.app.expected_modules must be a list of strings")
        require_active_process = app.get("require_active_process")
        if require_active_process is not None and not isinstance(require_active_process, bool):
            errors.append("app_diagnostics.app.require_active_process must be a boolean")
    workspace = payload.get("workspace")
    if workspace is not None and not isinstance(workspace, (str, dict)):
        errors.append("app_diagnostics.workspace must be a string or object")
    process = payload.get("process")
    if process is not None and not isinstance(process, dict):
        errors.append("app_diagnostics.process must be an object")
    modules = payload.get("modules")
    if modules is not None and not isinstance(modules, (list, dict)):
        errors.append("app_diagnostics.modules must be a list or object")
    loaded_sources = payload.get("loaded_sources")
    if loaded_sources is not None and not _is_string_list(loaded_sources):
        errors.append("app_diagnostics.loaded_sources must be a list of strings")
    artifacts = payload.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, (list, dict)):
        errors.append("app_diagnostics.artifacts must be a list or object")
    for field_name in ("modules", "artifacts"):
        field = payload.get(field_name)
        if not isinstance(field, dict):
            continue
        expected = field.get("expected")
        if expected is not None and not _is_string_list(expected):
            errors.append(f"app_diagnostics.{field_name}.expected must be a list of strings")


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_wait_json_schema(
    payload: dict[str, Any],
    errors: list[str],
    *,
    field_name: str,
) -> None:
    wait_json = payload.get(field_name)
    if wait_json is None:
        return
    if not isinstance(wait_json, dict):
        errors.append(f"app_diagnostics.{field_name} must be an object")
        return
    path = wait_json.get("path")
    if not isinstance(path, str) or not path:
        errors.append(f"app_diagnostics.{field_name}.path is required")
    pattern = wait_json.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            errors.append(f"app_diagnostics.{field_name}.pattern must be a string")
        elif "/" in pattern or "\\" in pattern:
            errors.append(
                f"app_diagnostics.{field_name}.pattern must be a file-name pattern"
            )
    _validate_wait_json_since_schema(wait_json, errors, field_name=field_name)
    _validate_wait_json_condition_schema(wait_json, errors, field_name=field_name)
    for numeric_field in ("timeout_ms", "poll_interval_ms"):
        if numeric_field not in wait_json:
            continue
        value = wait_json[numeric_field]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(
                f"app_diagnostics.{field_name}.{numeric_field} must be an integer"
            )
        elif value < 0:
            errors.append(f"app_diagnostics.{field_name}.{numeric_field} must be >= 0")


def _validate_wait_json_since_schema(
    wait_json: dict[str, Any],
    errors: list[str],
    *,
    field_name: str,
) -> None:
    since = wait_json.get("since")
    if since is None:
        return
    if field_name != "poll":
        errors.append(f"app_diagnostics.{field_name}.since is not accepted")
        return
    if not isinstance(since, dict):
        errors.append("app_diagnostics.poll.since must be an object")
        return
    mtime_ns = since.get("mtime_ns")
    if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
        errors.append("app_diagnostics.poll.since.mtime_ns must be an integer")
    elif mtime_ns < 0:
        errors.append("app_diagnostics.poll.since.mtime_ns must be >= 0")
    name = since.get("name")
    if not isinstance(name, str) or not name:
        errors.append("app_diagnostics.poll.since.name must be a non-empty string")


def _validate_wait_json_condition_schema(
    wait_json: dict[str, Any],
    errors: list[str],
    *,
    field_name: str,
) -> None:
    condition = wait_json.get("condition")
    if condition is None:
        return
    if field_name != "wait_json":
        errors.append("app_diagnostics.poll.condition is not accepted")
        return
    if not isinstance(condition, dict):
        errors.append("app_diagnostics.wait_json.condition must be an object")
        return
    jsonpath = condition.get("jsonpath")
    if not isinstance(jsonpath, str) or not jsonpath:
        errors.append("app_diagnostics.wait_json.condition.jsonpath is required")
    if "expected" not in condition:
        errors.append("app_diagnostics.wait_json.condition.expected is required")


def _validate_semantic_probe_schema(payload: dict[str, Any], errors: list[str]) -> None:
    _validate_probe_name("semantic_probe.kind", payload.get("kind"), errors)
    blocked_payload = payload.get("on_blocked")
    if payload.get("status") == "BLOCKED":
        if isinstance(blocked_payload, dict):
            _require_blocked_diagnostics("semantic_probe.on_blocked", blocked_payload, errors)
        else:
            errors.append("semantic_probe.on_blocked must be an object for BLOCKED")


def _validate_tracepoint_guardrail_schema(payload: dict[str, Any], errors: list[str]) -> None:
    for field_name in ("allowed_when", "blocked_when", "unsafe_when"):
        value = payload.get(field_name)
        if isinstance(value, list):
            if not value:
                errors.append(f"tracepoint_guardrail.{field_name} must not be empty")
            elif any(not isinstance(item, str) for item in value):
                errors.append(f"tracepoint_guardrail.{field_name} must be a list of strings")
    cleanup = payload.get("cleanup")
    if not isinstance(cleanup, dict):
        return
    if not isinstance(cleanup.get("owner"), str) or not cleanup.get("owner"):
        errors.append("tracepoint_guardrail.cleanup.owner is required")
    operations = cleanup.get("operations")
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        errors.append("tracepoint_guardrail.cleanup.operations must be a list of strings")
    elif "debug.tracepoint.remove" not in operations:
        errors.append(
            "tracepoint_guardrail.cleanup.operations must include debug.tracepoint.remove"
        )


def _require_fields(
    prefix: str,
    payload: dict[str, Any],
    field_names: tuple[str, ...],
    errors: list[str],
) -> None:
    for field_name in field_names:
        if field_name not in payload:
            errors.append(f"{prefix}.{field_name} is required")


def _validate_probe_name(path: str, value: Any, errors: list[str]) -> None:
    if value is None:
        return
    if value not in _accepted_diagnostic_probe_kinds():
        accepted = ", ".join(_accepted_diagnostic_probe_kinds())
        errors.append(f"{path} must be one of: {accepted}")


def _validate_next_step(prefix: str, payload: Any, errors: list[str]) -> None:
    if not isinstance(payload, dict):
        return
    if not isinstance(payload.get("next_step"), str) or not payload.get("next_step"):
        errors.append(f"{prefix}.on_blocked.next_step is required")


def _validate_optional_status(prefix: str, status: Any, errors: list[str]) -> None:
    if status is None:
        return
    if status not in DIAGNOSTIC_STATUS_VALUES:
        accepted = ", ".join(DIAGNOSTIC_STATUS_VALUES)
        errors.append(f"{prefix}.status must be one of: {accepted}")


def _require_blocked_diagnostics(
    prefix: str,
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    for field_name in ("reason", "requested", "accepted", "next_step"):
        value = payload.get(field_name)
        if value is None or value == "":
            errors.append(f"{prefix}.{field_name} is required for BLOCKED")


def validate_plan(plan: Any) -> list[str]:
    """Validate the runtime smoke plan shape without touching the target app."""

    if not isinstance(plan, dict):
        return ["plan must be an object"]

    errors: list[str] = []
    _validate_top_level_keys(plan, errors)
    _validate_schema_value(plan, errors)
    _validate_list_fields(plan, errors)
    _validate_object_fields(plan, errors)
    _validate_budgets(plan, errors)
    _validate_step_collections(plan, errors)
    _validate_restore_configs(plan, errors)
    return errors


def _validate_top_level_keys(plan: dict[str, Any], errors: list[str]) -> None:
    accepted_keys = (
        ACCEPTED_TOP_LEVEL_KEYS_V2
        if plan.get("schema") == SCHEMA_VERSION_V2
        else ACCEPTED_TOP_LEVEL_KEYS
    )
    accepted = set(accepted_keys)
    for key in plan:
        if key not in accepted:
            expected = ", ".join(accepted_keys)
            errors.append(f"unexpected top-level key: {key}; expected one of: {expected}")


def _is_v2_shaped(plan: dict[str, Any] | None) -> bool:
    if not isinstance(plan, dict):
        return False
    return plan.get("schema") == SCHEMA_VERSION_V2 or bool(
        set(plan).intersection(V2_ONLY_TOP_LEVEL_KEYS)
    )


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
    v2_keys = sorted(set(plan).intersection(V2_ONLY_TOP_LEVEL_KEYS))
    if schema is None and v2_keys:
        errors.append("schema is required when using v2-only keys: " + ", ".join(v2_keys))
    if schema is not None and schema not in ACCEPTED_SCHEMA_VALUES:
        accepted = ", ".join(ACCEPTED_SCHEMA_VALUES)
        errors.append(f"schema must be one of: {accepted}")
    if schema == SCHEMA_VERSION_V2:
        mixed_keys = sorted(set(plan).intersection(V1_EXECUTION_KEYS))
        if mixed_keys and v2_keys:
            errors.append(
                "v2 plans cannot mix legacy execution keys with v2 case keys: "
                + ", ".join(mixed_keys)
            )


def _validate_list_fields(plan: dict[str, Any], errors: list[str]) -> None:
    for field_name in ("actions", "assertions", "evidence", "steps", "cases"):
        if field_name in plan and not isinstance(plan[field_name], list):
            errors.append(f"{field_name} must be a list")


def _validate_object_fields(plan: dict[str, Any], errors: list[str]) -> None:
    if "preflight" in plan and not isinstance(plan["preflight"], (bool, dict, list)):
        errors.append("preflight must be a boolean, object, or list")
    if "launch" in plan and not isinstance(plan["launch"], dict):
        errors.append("launch must be an object")
    if "freshness" in plan and not isinstance(plan["freshness"], dict):
        errors.append("freshness must be an object")
    if "baseline" in plan and not isinstance(plan["baseline"], dict):
        errors.append("baseline must be an object")
    if "generate" in plan and not isinstance(plan["generate"], dict):
        errors.append("generate must be an object")
    _validate_diagnostics_field(plan, errors)
    if "metrics_thresholds" in plan and not isinstance(plan["metrics_thresholds"], dict):
        errors.append("metrics_thresholds must be an object")
    for field_name in ("cleanup", "teardown"):
        if field_name in plan and not isinstance(plan[field_name], dict):
            errors.append(f"{field_name} must be an object")


def _validate_diagnostics_field(plan: dict[str, Any], errors: list[str]) -> None:
    if "diagnostics" not in plan:
        return
    diagnostics = plan["diagnostics"]
    if not isinstance(diagnostics, dict):
        errors.append("diagnostics must be an object")
        return

    app_diagnostics = diagnostics.get("app_diagnostics")
    if app_diagnostics is None:
        return
    if not isinstance(app_diagnostics, dict):
        errors.append("diagnostics.app_diagnostics must be an object")
        return
    if "diagnostic_launch" in app_diagnostics and not isinstance(
        app_diagnostics["diagnostic_launch"],
        dict,
    ):
        errors.append("diagnostics.app_diagnostics.diagnostic_launch must be an object")


def _validate_budgets(plan: dict[str, Any], errors: list[str]) -> None:
    budgets = plan.get("budgets", {})
    if budgets is not None and not isinstance(budgets, dict):
        errors.append("budgets must be an object")
        return
    if not isinstance(budgets, dict):
        return
    if "max_actions" in budgets:
        max_actions = budgets["max_actions"]
        if isinstance(max_actions, bool) or not isinstance(max_actions, int):
            errors.append("budgets.max_actions must be an integer")
        elif max_actions < 1:
            errors.append("budgets.max_actions must be at least 1")
    if "max_elapsed_seconds" in budgets:
        max_elapsed = budgets["max_elapsed_seconds"]
        if isinstance(max_elapsed, bool) or not isinstance(max_elapsed, (int, float)):
            errors.append("budgets.max_elapsed_seconds must be a number")
        else:
            try:
                elapsed_value = float(max_elapsed)
            except OverflowError:
                errors.append("budgets.max_elapsed_seconds must be positive")
            else:
                if not math.isfinite(elapsed_value) or elapsed_value <= 0:
                    errors.append("budgets.max_elapsed_seconds must be positive")


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
    _validate_op_args(prefix, op_name, args, errors)
    if op_name == "fixture.restore":
        _validate_restore_entry(prefix, args, errors)


def _validate_op_args(
    prefix: str,
    op_name: str,
    args: dict[str, Any],
    errors: list[str],
) -> None:
    if "selector" in args and not isinstance(args["selector"], dict):
        errors.append(f"{prefix}.selector must be an object for op {op_name}")

    if op_name in {
        "ui.grid.snapshot",
        "ui.grid.get_state",
        "ui.grid.ensure_visible",
        "ui.grid.select_row",
        "ui.grid.click_row",
        "ui.grid.right_click_row",
        "ui.grid.double_click_row",
    }:
        if "rows" in args and not isinstance(args["rows"], dict):
            errors.append(f"{prefix}.rows must be an object for op {op_name}")
        _validate_optional_string_list(prefix, op_name, args, "columns", errors)
        if "identity" in args and not isinstance(args["identity"], dict):
            errors.append(f"{prefix}.identity must be an object for op {op_name}")
        if op_name in {
            "ui.grid.ensure_visible",
            "ui.grid.select_row",
            "ui.grid.click_row",
            "ui.grid.right_click_row",
            "ui.grid.double_click_row",
        }:
            if "row" in args and not isinstance(args["row"], dict):
                errors.append(f"{prefix}.row must be an object for op {op_name}")
        if op_name == "ui.grid.ensure_visible":
            _validate_int_arg(prefix, op_name, args, "max_scrolls", errors)
            _validate_int_arg(prefix, op_name, args, "scroll_settle_ms", errors)
        if op_name in {
            "ui.grid.select_row",
            "ui.grid.click_row",
            "ui.grid.right_click_row",
            "ui.grid.double_click_row",
        }:
            if "ensure_visible" in args and not isinstance(args["ensure_visible"], bool):
                errors.append(f"{prefix}.ensure_visible must be a boolean for op {op_name}")
            _validate_int_arg(prefix, op_name, args, "max_scrolls", errors)
            _validate_int_arg(prefix, op_name, args, "scroll_settle_ms", errors)
        if op_name in {"ui.grid.click_row", "ui.grid.right_click_row", "ui.grid.double_click_row"}:
            if "column" in args and not isinstance(args["column"], str):
                errors.append(f"{prefix}.column must be a string for op {op_name}")
    elif op_name in {"ui.grid.select_range", "ui.grid.assert_range"}:
        _validate_int_arg(prefix, op_name, args, "start_index", errors)
        _validate_int_arg(prefix, op_name, args, "end_index", errors)
    elif op_name == "ui.grid.assert_rows":
        if "rows" in args and not isinstance(args["rows"], list):
            errors.append(f"{prefix}.rows must be a list for op {op_name}")
    elif op_name in {"ui.list.invoke_item", "ui.list.toggle_item_child"}:
        if "item" in args and not isinstance(args["item"], dict):
            errors.append(f"{prefix}.item must be an object for op {op_name}")
        if op_name == "ui.list.toggle_item_child":
            if "child" in args and not isinstance(args["child"], dict):
                errors.append(f"{prefix}.child must be an object for op {op_name}")
            if (
                "target_state" in args
                and args["target_state"] is not None
                and not isinstance(args["target_state"], str)
            ):
                errors.append(f"{prefix}.target_state must be a string for op {op_name}")
    elif op_name == "ui.key_sequence":
        if "keys" in args and not isinstance(args["keys"], list):
            errors.append(f"{prefix}.keys must be a list for op {op_name}")
    elif op_name == "ui.text.assert_selection":
        if args.get("selection_start") is None or args.get("selection_end") is None:
            errors.append(
                f"{prefix}.selection_start and selection_end are required for op {op_name}"
            )
        _validate_int_arg(prefix, op_name, args, "selection_start", errors)
        _validate_int_arg(prefix, op_name, args, "selection_end", errors)
    elif op_name == "ui.text.set_text":
        if "text" in args and not isinstance(args["text"], str):
            errors.append(f"{prefix}.text must be a string for op {op_name}")
    elif op_name == "ui.get_property":
        has_property_argument = args.get("property") is not None
        has_property_name_argument = args.get("property_name") is not None
        if not has_property_argument and not has_property_name_argument:
            errors.append(f"{prefix}.property or property_name is required for op {op_name}")
        for field_name in ("property", "property_name"):
            if (
                field_name in args
                and args[field_name] is not None
                and not isinstance(args[field_name], str)
            ):
                errors.append(f"{prefix}.{field_name} must be a string for op {op_name}")
            elif isinstance(args.get(field_name), str) and not args[field_name].strip():
                errors.append(
                    f"{prefix}.{field_name} must be a non-empty string for op {op_name}"
                )


def _validate_int_arg(
    prefix: str,
    op_name: str,
    args: dict[str, Any],
    field_name: str,
    errors: list[str],
) -> None:
    if field_name in args and (
        isinstance(args[field_name], bool) or not isinstance(args[field_name], int)
    ):
        errors.append(f"{prefix}.{field_name} must be an integer for op {op_name}")


def _validate_optional_string_list(
    prefix: str,
    op_name: str,
    args: dict[str, Any],
    field_name: str,
    errors: list[str],
) -> None:
    if field_name not in args:
        return
    value = args[field_name]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{prefix}.{field_name} must be a list of strings for op {op_name}")


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
        errors.append(f"{prefix} requires exactly one of baseline_text or baseline_file")
        return

    if has_baseline_text and not isinstance(entry.get("baseline_text"), str):
        errors.append(f"{prefix}.baseline_text must be a string")
    if has_baseline_file:
        baseline_file = entry.get("baseline_file")
        if not isinstance(baseline_file, str) or not baseline_file:
            errors.append(f"{prefix}.baseline_file must be a non-empty string")

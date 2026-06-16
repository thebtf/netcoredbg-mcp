from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
    validate_diagnostic_schema_example,
)
from ..blocked import build_blocked
from ..evidence import blocked_details_from_record, compact_evidence
from ..result_envelope import compact_json_size, compact_value
from . import accepted_probe_kinds
from ._common import probe_name


def diagnostic_validation_errors(probe: dict[str, Any], *, kind: str) -> list[str]:
    return validate_diagnostic_schema_example(probe, kind=kind)


def invalid_diagnostic_probe(
    probe: dict[str, Any],
    *,
    kind: str,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "BLOCKED",
        "value": None,
        "validation_errors": list(errors),
        **build_blocked(
            reason=f"invalid {kind} diagnostic",
            requested={"kind": kind, "validation_errors": list(errors)},
            accepted={
                "schema": DIAGNOSTIC_SCHEMA_VERSION,
                "probe_kinds": accepted_probe_kinds(),
            },
            next_step=f"Fix the {kind} payload before running it.",
        ),
    }


def bounded_diagnostic_value(
    value: dict[str, Any],
    *,
    limits: Mapping[str, int],
) -> dict[str, Any]:
    compact = compact_value(
        compact_evidence(value),
        max_text_length=limits["max_text_length"],
        max_list_items=limits["max_list_items"],
    )
    if not isinstance(compact, dict):
        return {"value": compact}
    if compact_json_size(compact) <= limits["max_json_bytes"]:
        return compact
    return {
        "schema": compact.get("schema"),
        "status": compact.get("status"),
        "json_bytes": compact_json_size(compact),
        "omitted_fields": ["diagnostic_value"],
    }


def diagnostic_limits(probe: Mapping[str, Any]) -> dict[str, int]:
    return {
        field_name: limit_value(probe, field_name, default)
        for field_name, default in DIAGNOSTIC_EVIDENCE_LIMITS.items()
    }


def limit_value(probe: Mapping[str, Any], field_name: str, default: int) -> int:
    limits = probe.get("limits")
    if not isinstance(limits, Mapping):
        return default
    value = limits.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0:
        return default
    return min(value, default)


def blocked_details_from_first_observation(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    for observation in observations:
        if observation.get("status") == "BLOCKED":
            return blocked_details_from_record(observation)
    return {}

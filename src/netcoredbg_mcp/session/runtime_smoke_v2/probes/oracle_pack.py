from __future__ import annotations

from typing import Any

from ...runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
)
from ._common import probe_name
from ._diagnostic_common import (
    bounded_diagnostic_value,
    diagnostic_validation_errors,
    invalid_diagnostic_probe,
    limit_value,
)


async def handle_oracle_pack(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "oracle_pack"
    errors = diagnostic_validation_errors(probe, kind=kind)
    if errors:
        return invalid_diagnostic_probe(probe, kind=kind, errors=errors)

    checks = [dict(check) for check in probe.get("checks", []) if isinstance(check, dict)]
    status = str(probe.get("status") or "PASS")
    value = {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "id": str(probe.get("id") or probe_name(probe, kind)),
        "status": status,
        "check_count": len(checks),
        "checks": checks,
        "limits": dict(probe.get("limits") or {}),
    }
    if probe.get("description"):
        value["description"] = str(probe["description"])
    max_json_bytes = limit_value(
        probe,
        "max_json_bytes",
        DIAGNOSTIC_EVIDENCE_LIMITS["max_json_bytes"],
    )
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": bounded_diagnostic_value(value, max_json_bytes=max_json_bytes),
        "evidence_ref": f"diagnostic:oracle_pack:{value['id']}",
    }
    if status == "BLOCKED":
        output["reason"] = "oracle pack reported BLOCKED"
    elif status == "FAIL":
        output["reason"] = "oracle pack reported FAIL"
    return output

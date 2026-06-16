from __future__ import annotations

from typing import Any

from ...runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
)
from ._common import probe_name
from ._diagnostic_common import (
    blocked_details_from_first_observation,
    bounded_diagnostic_value,
    diagnostic_validation_errors,
    invalid_diagnostic_probe,
    limit_value,
)


async def handle_app_diagnostics(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "app_diagnostics"
    errors = diagnostic_validation_errors(probe, kind=kind)
    if errors:
        return invalid_diagnostic_probe(probe, kind=kind, errors=errors)

    observations = [
        dict(observation)
        for observation in probe.get("observations", [])
        if isinstance(observation, dict)
    ]
    app = dict(probe.get("app") or {})
    status = str(probe.get("status") or "PASS")
    value = {
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "app": app,
        "status": status,
        "observation_count": len(observations),
        "observations": observations,
        "limits": dict(probe.get("limits") or {}),
    }
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
        "evidence_ref": f"diagnostic:app_diagnostics:{app.get('name') or 'app'}",
    }
    if status == "BLOCKED":
        output["reason"] = "app diagnostics reported BLOCKED"
        output.update(blocked_details_from_first_observation(observations))
    elif status == "FAIL":
        output["reason"] = "app diagnostics reported FAIL"
    return output

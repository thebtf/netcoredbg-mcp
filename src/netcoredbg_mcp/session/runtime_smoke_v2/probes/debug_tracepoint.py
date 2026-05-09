from __future__ import annotations

from typing import Any

from ._common import (
    attach_expected_and_status,
    blocked_probe,
    evidence_ref,
    probe_name,
    service_available,
)


async def handle_debug_tracepoint(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "debug.tracepoint"
    if not service_available(context, kind):
        return blocked_probe(
            probe,
            kind=kind,
            requested={
                "file": probe.get("file"),
                "line": probe.get("line"),
                "expression": probe.get("expression"),
            },
            next_step="Attach a tracepoint-capable debug adapter before running this probe.",
        )

    line, line_error = _optional_int(probe.get("line"), field_name="line")
    if line_error is not None:
        return _invalid_numeric_probe(probe, kind=kind, reason=line_error)

    result = await context.call_adapter(
        kind,
        file=str(probe.get("file") or ""),
        line=line,
        expression=str(probe.get("expression") or ""),
        phase=phase,
    )
    status = str(result.get("status", "PASS"))
    value = {
        "hit_count": _coerce_int(result.get("hit_count"), default=0),
        "logs": list(result.get("logs") or []),
    }
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": value,
    }
    if result.get("reason"):
        output["reason"] = result["reason"]
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    if "expected_hit_count" in probe:
        expected, expected_error = _required_int(
            probe["expected_hit_count"],
            field_name="expected_hit_count",
        )
        if expected_error is not None:
            output["status"] = "FAIL"
            output["reason"] = expected_error
            return output
        output["expected"] = {"hit_count": expected}
        if phase == "after" and status == "PASS" and value["hit_count"] != expected:
            output["status"] = "FAIL"
            output["reason"] = "tracepoint hit count did not match"
        return output
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)


def _optional_int(value: Any, *, field_name: str) -> tuple[int, str | None]:
    if value in (None, ""):
        return 0, None
    return _required_int(value, field_name=field_name)


def _required_int(value: Any, *, field_name: str) -> tuple[int, str | None]:
    try:
        return int(value), None
    except (TypeError, ValueError):
        return 0, f"invalid {field_name}"


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _invalid_numeric_probe(
    probe: dict[str, Any],
    *,
    kind: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "FAIL",
        "reason": reason,
        "value": None,
    }

from __future__ import annotations

from typing import Any

from ...runtime_smoke_correlation import (
    action_sample_provenance,
    attach_sample_correlation,
    correlation_source,
)
from ...tracepoint_policy import (
    SAFE_TRACEPOINT_EXPRESSION_GUIDANCE,
    classify_tracepoint_logs,
    tracepoint_expression_policy_error,
)
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
        return _attach_tracepoint_correlation(
            blocked_probe(
                probe,
                kind=kind,
                requested={
                    "file": probe.get("file"),
                    "line": probe.get("line"),
                    "expression": probe.get("expression"),
                },
                next_step="Attach a tracepoint-capable debug adapter before running this probe.",
            ),
            context,
        )

    line, line_error = _optional_int(probe.get("line"), field_name="line")
    if line_error is not None:
        return _attach_tracepoint_correlation(
            _invalid_numeric_probe(probe, kind=kind, reason=line_error),
            context,
        )
    expression = str(probe.get("expression") or "")
    policy_error = tracepoint_expression_policy_error(expression)
    if policy_error is not None:
        return _attach_tracepoint_correlation(
            {
                "name": probe_name(probe, kind),
                "kind": kind,
                "status": "FAIL",
                "classification": "UNSAFE_EXPRESSION",
                "reason": "unsafe tracepoint expression",
                "value": None,
                "accepted": {
                    "expression": SAFE_TRACEPOINT_EXPRESSION_GUIDANCE,
                },
            },
            context,
        )

    result = await context.call_adapter(
        kind,
        file=str(probe.get("file") or ""),
        line=line,
        expression=expression,
        phase=phase,
    )
    status = str(result.get("status", "PASS"))
    logs: list[Any] = list(result.get("logs") or [])
    value: dict[str, Any] = {
        "hit_count": _coerce_int(result.get("hit_count"), default=0),
        "logs": logs,
    }
    output: dict[str, Any] = {
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
    classification, classification_reason = classify_tracepoint_logs(value["logs"])
    source_label = correlation_source(probe, fallback="") if "correlation_source" in probe else None
    correlation = attach_sample_correlation(
        output,
        result.get("correlation"),
        provenance=action_sample_provenance(
            context.action_context,
            raw_result=result,
        ),
        source_label=source_label,
    )
    output = correlation
    if classification is not None:
        output["status"] = "BLOCKED"
        output["classification"] = classification
        output["reason"] = str(classification_reason or classification)
        return output
    if "expected_hit_count" in probe:
        expected, expected_error = _required_int(
            probe["expected_hit_count"],
            field_name="expected_hit_count",
        )
        if expected_error is not None:
            output["status"] = "FAIL"
            output["reason"] = expected_error
            return output
        expected_payload: dict[str, Any] = {"hit_count": expected}
        if probe.get("expected_route"):
            expected_payload["route"] = str(probe["expected_route"])
        output["expected"] = expected_payload
        if (
            phase == "after"
            and status == "PASS"
            and expected > 0
            and value["hit_count"] == 0
            and probe.get("expected_route")
        ):
            output["status"] = "BLOCKED"
            output["classification"] = "NO_ROUTE_HIT"
            output["reason"] = "expected tracepoint route was not hit"
            output["next_step"] = "Verify handler routing before blaming the debugger."
            return output
        if phase == "after" and status == "PASS" and value["hit_count"] != expected:
            output["status"] = "FAIL"
            output["reason"] = "tracepoint hit count did not match"
        return output
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)


def _attach_tracepoint_correlation(
    output: dict[str, Any],
    context: Any,
    *,
    raw_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return attach_sample_correlation(
        output,
        raw_result.get("correlation") if raw_result is not None else None,
        provenance=action_sample_provenance(
            context.action_context,
            raw_result=raw_result,
        ),
    )


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

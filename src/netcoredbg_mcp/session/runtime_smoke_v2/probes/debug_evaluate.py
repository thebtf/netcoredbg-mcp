from __future__ import annotations

import inspect
from typing import Any

from ...runtime_smoke_correlation import (
    action_sample_provenance,
    attach_sample_correlation,
    correlation_source,
)


async def handle_debug_evaluate(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    expression = str(probe.get("expression") or "")
    result = await _evaluate_probe_expression(context, expression)
    result = _normalize_evaluate_result(result)
    status = str(result.get("status", "PASS"))
    value = result.get("value")
    expected = probe.get("expected")
    if phase == "after" and "expected" in probe and status == "PASS" and value != expected:
        status = "FAIL"
    output = {
        "name": str(probe.get("name") or expression or "debug.evaluate"),
        "kind": "debug.evaluate",
        "status": status,
        "value": value,
    }
    if "expected" in probe:
        output["expected"] = expected
    if status == "FAIL":
        output["reason"] = result.get("reason", "expected value did not match")
    if status == "BLOCKED":
        output["reason"] = result.get("reason", "debug evaluation blocked")
    source_label = correlation_source(probe, fallback="") if "correlation_source" in probe else None
    return attach_sample_correlation(
        output,
        result.get("correlation"),
        provenance=action_sample_provenance(
            context.action_context,
            raw_result=result,
        ),
        source_label=source_label,
    )


async def _evaluate_probe_expression(context: Any, expression: str) -> Any:
    adapters = context.action_context.service_adapters
    if "debug.evaluate" in adapters:
        return await context.call_adapter("debug.evaluate", expression=expression)
    evaluate = getattr(context.session, "evaluate_expression", None)
    if evaluate is None:
        return {
            "status": "BLOCKED",
            "reason": "no stopped frame",
            "value": None,
        }
    result = evaluate(expression)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_evaluate_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "PASS", "value": result}
    normalized = dict(result)
    if "status" not in normalized:
        if "error" in normalized:
            normalized["status"] = "BLOCKED"
            normalized.setdefault("reason", str(normalized["error"]))
        else:
            normalized["status"] = "PASS"
    elif str(normalized["status"]).upper() in {"OK", "SUCCESS"}:
        normalized["status"] = "PASS"
    if "value" not in normalized:
        normalized["value"] = normalized.get("result")
    return normalized

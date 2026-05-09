from __future__ import annotations

import inspect
from typing import Any


async def handle_debug_evaluate(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    expression = str(probe.get("expression") or "")
    adapters = context.action_context.service_adapters
    if "debug.evaluate" in adapters:
        result = await context.call_adapter("debug.evaluate", expression=expression)
    else:
        evaluate = getattr(context.session, "evaluate_expression", None)
        if evaluate is None:
            result = {
                "status": "BLOCKED",
                "reason": "no stopped frame",
                "value": None,
            }
        else:
            result = evaluate(expression)
            if inspect.isawaitable(result):
                result = await result
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
    return output


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

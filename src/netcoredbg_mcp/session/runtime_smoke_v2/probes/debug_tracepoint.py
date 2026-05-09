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

    result = await context.call_adapter(
        kind,
        file=str(probe.get("file") or ""),
        line=int(probe.get("line") or 0),
        expression=str(probe.get("expression") or ""),
        phase=phase,
    )
    status = str(result.get("status", "PASS"))
    value = {
        "hit_count": int(result.get("hit_count", 0) or 0),
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
        expected = int(probe["expected_hit_count"])
        output["expected"] = {"hit_count": expected}
        if phase == "after" and status == "PASS" and value["hit_count"] != expected:
            output["status"] = "FAIL"
            output["reason"] = "tracepoint hit count did not match"
        return output
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)

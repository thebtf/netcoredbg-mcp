from __future__ import annotations

import re
from typing import Any

from ._common import (
    attach_expected_and_status,
    blocked_probe,
    evidence_ref,
    probe_name,
    service_available,
)


async def handle_output_field(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "output.field"
    checkpoint = str(probe.get("checkpoint") or "default")
    if service_available(context, "output.lines_since"):
        result = await context.call_adapter("output.lines_since", checkpoint=checkpoint)
    else:
        result = _lines_since_checkpoint(context.session, checkpoint)
    if result.get("status") == "BLOCKED":
        return blocked_probe(
            probe,
            kind=kind,
            requested={"checkpoint": checkpoint},
            next_step="Create an output checkpoint before evaluating output.field probes.",
        )
    if result.get("status") != "PASS":
        return {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": str(result.get("status", "FAIL")),
            "reason": result.get("reason", "output field read failed"),
            "value": None,
        }

    source = str(probe.get("source") or "")
    reason = str(probe.get("reason") or "")
    field = str(probe.get("field") or "")
    value = _find_field_value(list(result.get("lines") or []), source, reason, field)
    if value is None:
        return {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": "FAIL",
            "reason": "output field not found",
            "value": None,
            "requested": {"source": source, "reason": reason, "field": field},
        }

    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": "PASS",
        "value": value,
    }
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)


def _find_field_value(
    lines: list[Any],
    source: str,
    reason: str,
    field: str,
) -> str | None:
    pattern = re.compile(rf"(?:^|\s){re.escape(field)}=(?P<value>[^\s]*)")
    for raw_line in lines:
        line = str(raw_line)
        if f"source={source}" not in line or f"reason={reason}" not in line:
            continue
        match = pattern.search(line)
        if match:
            return match.group("value")
    return None


def _lines_since_checkpoint(session: Any, checkpoint: str) -> dict[str, Any]:
    runtime_smoke = getattr(session, "runtime_smoke", None)
    state = getattr(session, "state", None)
    checkpoints = getattr(runtime_smoke, "output_checkpoints", None)
    if not isinstance(checkpoints, dict) or state is None:
        return {"status": "BLOCKED", "reason": "output buffer unavailable"}
    saved = checkpoints.get(checkpoint)
    if not isinstance(saved, dict):
        return {"status": "FAIL", "reason": "output checkpoint not found"}
    entries = list(getattr(state, "output_buffer", []))
    start = int(saved.get("entry_count", 0) or 0)
    text = "".join(str(getattr(entry, "text", "")) for entry in entries[start:])
    return {"status": "PASS", "lines": text.splitlines()}

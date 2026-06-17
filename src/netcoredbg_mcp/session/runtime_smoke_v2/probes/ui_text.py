from __future__ import annotations

from typing import Any

from ..evidence import attach_blocked_details
from ._common import (
    attach_expected_and_status,
    blocked_probe,
    evidence_ref,
    probe_name,
    service_available,
)


async def handle_ui_text(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "ui.text"
    action = str(probe.get("action") or "assert")
    if action == "read":
        if not service_available(context, "ui.text.read"):
            return blocked_probe(
                probe,
                kind=kind,
                requested={"selector": dict(probe.get("selector") or {})},
                next_step="Connect a UI backend that exposes ui.text.read.",
            )
        result = await context.call_adapter(
            "ui.text.read",
            selector=dict(probe.get("selector") or {}),
        )
        status = str(result.get("status", "PASS"))
        value = result.get("text", result.get("value"))
        output = {
            "name": probe_name(probe, kind),
            "kind": kind,
            "status": status,
            "value": value,
        }
        if "source" in result:
            output["source"] = result["source"]
        if status != "PASS":
            output["reason"] = result.get("reason", "text read failed")
            attach_blocked_details(output, result)
        ref = evidence_ref(result)
        if ref:
            output["evidence_ref"] = ref
        return attach_expected_and_status(output, probe=probe, phase=phase, value=value)

    if not service_available(context, "ui.text.assert"):
        return blocked_probe(
            probe,
            kind=kind,
            requested={"selector": dict(probe.get("selector") or {})},
            next_step="Connect a UI backend that exposes ui.text.assert.",
        )
    result = await context.call_adapter(
        "ui.text.assert",
        selector=dict(probe.get("selector") or {}),
        contains=probe.get("contains"),
        equals=probe.get("equals"),
        must_exist=bool(probe.get("must_exist", True)),
    )
    status = str(result.get("status", "PASS"))
    value = result.get("text", result.get("value"))
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": value,
    }
    if status != "PASS":
        output["reason"] = result.get("reason", "text assertion failed")
        attach_blocked_details(output, result)
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)

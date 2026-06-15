from __future__ import annotations

from typing import Any

from ..evidence import attach_blocked_details
from ._common import (
    blocked_probe,
    evidence_ref,
    probe_name,
    service_available,
)


async def handle_ui_grid(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "ui.grid"
    rows = list(probe.get("rows") or [])
    columns = [str(column) for column in probe.get("columns") or []]
    selector = dict(probe.get("selector") or {})
    adapter_name = "ui.grid.assert_rows" if rows else "ui.grid.snapshot"
    if not service_available(context, adapter_name):
        return blocked_probe(
            probe,
            kind=kind,
            requested={"selector": selector, "rows": rows},
            next_step="Connect a UI backend that exposes grid snapshot/assert-row routes.",
        )
    if rows:
        result = await context.call_adapter(
            adapter_name,
            selector=selector,
            rows=rows,
            columns=columns,
        )
    else:
        result = await context.call_adapter(
            adapter_name,
            selector=selector,
            rows=dict(probe.get("row_window") or {}),
            columns=columns,
        )
    status = str(result.get("status", "PASS"))
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else result
    value = snapshot.get("visible_rows") if isinstance(snapshot, dict) else None
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": value,
    }
    if rows:
        output["expected"] = rows
    if status != "PASS":
        output["reason"] = result.get("reason", "grid assertion failed")
        attach_blocked_details(output, result)
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    return output

from __future__ import annotations

from typing import Any

from ..evidence import attach_blocked_details, compact_evidence
from ._common import blocked_probe, evidence_ref, probe_name, service_available

_ADAPTER_NAME = "ui.grid.viewport"


async def handle_ui_grid_viewport(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "ui.grid.viewport"
    name = probe_name(probe, kind)
    selector = dict(probe.get("selector") or {})
    identity = dict(probe.get("identity") or {})
    rows = dict(probe.get("rows") or {})
    expect = dict(probe.get("expect") or {})
    if not service_available(context, _ADAPTER_NAME):
        return blocked_probe(
            probe,
            kind=kind,
            requested={"selector": selector, "probe": kind},
            next_step="Connect a UI backend that exposes grid viewport evidence.",
        )

    result = await context.call_adapter(
        _ADAPTER_NAME,
        selector=selector,
        identity=identity,
        rows=rows,
        expect=expect,
        phase=phase,
        probe_name=name,
    )
    status = str(result.get("status", "PASS"))
    snapshot = _compact_snapshot(result)
    output: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "status": status,
        "value": snapshot,
    }
    if status != "PASS":
        output["reason"] = result.get("reason", "grid viewport probe failed")
        attach_blocked_details(output, result)

    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref

    scratch_key = f"{kind}.{name}"
    if phase == "before":
        context.scratch[scratch_key] = snapshot
    elif phase == "after":
        previous = context.scratch.get(scratch_key)
        if isinstance(previous, dict) and isinstance(snapshot, dict):
            comparison = _compare_snapshots(previous, snapshot)
            output["comparison"] = comparison
            if expect:
                output["expected"] = expect
                if status == "PASS" and not _expectation_matches(comparison, expect):
                    output["status"] = "FAIL"
                    output["reason"] = "grid viewport expectation failed"
    return output


def _compact_snapshot(result: dict[str, Any]) -> Any:
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else result
    return compact_evidence(snapshot)


def _compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_first = before.get("first_visible_index")
    after_first = after.get("first_visible_index")
    before_last = before.get("last_visible_index")
    after_last = after.get("last_visible_index")
    direction = "unchanged"
    if isinstance(before_first, int) and isinstance(after_first, int):
        if after_first > before_first:
            direction = "down"
        elif after_first < before_first:
            direction = "up"
    return {
        "first_visible_index_changed": before_first != after_first,
        "last_visible_index_changed": before_last != after_last,
        "direction": direction,
    }


def _expectation_matches(comparison: dict[str, Any], expect: dict[str, Any]) -> bool:
    for key, expected_value in expect.items():
        if comparison.get(key) != expected_value:
            return False
    return True

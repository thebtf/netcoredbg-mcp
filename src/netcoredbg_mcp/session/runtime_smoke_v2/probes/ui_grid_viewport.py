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
                if (
                    expect.get("selected_payload_preserved") is True
                    and comparison.get("selected_payload_preserved") is None
                ):
                    output["status"] = "BLOCKED"
                    output["reason"] = "selected row evidence unavailable"
                    output["requested"] = {"expect": {"selected_payload_preserved": True}}
                    output["accepted"] = {
                        "selected_rows": "before and after selected row identities"
                    }
                    output["next_step"] = (
                        "Use a UI backend that returns selected row evidence for viewport probes."
                    )
                    return output
                missing_expectation = _missing_expectation_capability(comparison, expect)
                if missing_expectation is not None:
                    output["status"] = "BLOCKED"
                    output["reason"] = missing_expectation["reason"]
                    output["requested"] = missing_expectation["requested"]
                    output["accepted"] = missing_expectation["accepted"]
                    output["next_step"] = missing_expectation["next_step"]
                    return output
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
    first_changed = before_first != after_first
    last_changed = before_last != after_last
    direction = "unchanged"
    if isinstance(before_first, int) and isinstance(after_first, int):
        if after_first > before_first:
            direction = "down"
        elif after_first < before_first:
            direction = "up"
    if (
        direction == "unchanged"
        and isinstance(before_last, int)
        and isinstance(after_last, int)
    ):
        if after_last > before_last:
            direction = "down"
        elif after_last < before_last:
            direction = "up"
    selected_before = _row_identity_refs(before.get("selected_rows"))
    selected_after = _row_identity_refs(after.get("selected_rows"))
    selected_duplicate_identities = _duplicate_identities(selected_before, selected_after)
    selected_lost_identities = _difference(selected_before, selected_after)
    selected_unexpected_identities = _difference(selected_after, selected_before)
    selected_payload_preserved = (
        not selected_duplicate_identities
        and not selected_lost_identities
        and not selected_unexpected_identities
    )
    before_order = _row_identity_refs(before.get("visible_rows"))
    after_order = _row_identity_refs(after.get("visible_rows"))
    before_row_count = before.get("row_count")
    after_row_count = after.get("row_count")
    return {
        "first_visible_index_changed": first_changed,
        "last_visible_index_changed": last_changed,
        "viewport_moved": first_changed or last_changed,
        "direction": direction,
        "before_order": before_order,
        "after_order": after_order,
        "identity_order_preserved": before_order == after_order
        if before_order and after_order
        else None,
        "selected_before": selected_before,
        "selected_after": selected_after,
        "selected_duplicate_identities": selected_duplicate_identities,
        "selected_lost_identities": selected_lost_identities,
        "selected_unexpected_identities": selected_unexpected_identities,
        "selected_payload_preserved": selected_payload_preserved
        if selected_before and selected_after
        else None,
        "before_row_count": before_row_count,
        "after_row_count": after_row_count,
        "row_count_preserved": before_row_count == after_row_count
        if isinstance(before_row_count, int) and isinstance(after_row_count, int)
        else None,
    }


def _expectation_matches(comparison: dict[str, Any], expect: dict[str, Any]) -> bool:
    for key, expected_value in expect.items():
        if comparison.get(key) != expected_value:
            return False
    return True


def _missing_expectation_capability(
    comparison: dict[str, Any],
    expect: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        expect.get("identity_order_preserved") is True
        and comparison.get("identity_order_preserved") is None
    ):
        return {
            "reason": "visible row identity evidence unavailable",
            "requested": {"expect": {"identity_order_preserved": True}},
            "accepted": {
                "visible_rows": "before and after visible row identities"
            },
            "next_step": (
                "Use a UI backend that returns visible row identities for viewport probes."
            ),
        }
    if (
        expect.get("row_count_preserved") is True
        and comparison.get("row_count_preserved") is None
    ):
        return {
            "reason": "row count evidence unavailable",
            "requested": {"expect": {"row_count_preserved": True}},
            "accepted": {"row_count": "before and after row counts"},
            "next_step": (
                "Use a UI backend that returns row count evidence for viewport probes."
            ),
        }
    return None


def _row_identity_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        identity = row.get("identity")
        if identity is not None:
            refs.append(str(identity))
    return refs


def _duplicate_identities(*groups: list[str]) -> list[str]:
    duplicates: list[str] = []
    for group in groups:
        seen: set[str] = set()
        for identity in group:
            if identity in seen and identity not in duplicates:
                duplicates.append(identity)
            seen.add(identity)
    return duplicates


def _difference(left: list[str], right: list[str]) -> list[str]:
    remaining = list(right)
    missing: list[str] = []
    for identity in left:
        if identity in remaining:
            remaining.remove(identity)
        else:
            missing.append(identity)
    return missing

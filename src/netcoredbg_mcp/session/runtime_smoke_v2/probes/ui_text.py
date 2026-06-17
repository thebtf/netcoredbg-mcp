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
    if action == "get_state":
        if not service_available(context, "ui.text.get_state"):
            return blocked_probe(
                probe,
                kind=kind,
                requested={"selector": dict(probe.get("selector") or {})},
                next_step="Connect a UI backend that exposes ui.text.get_state.",
            )
        result = await context.call_adapter(
            "ui.text.get_state",
            selector=dict(probe.get("selector") or {}),
        )
        return _state_probe_output(result, probe=probe, kind=kind, phase=phase)

    if action == "assert_selection":
        selection_start, selection_end = _selection_range_from_probe(probe)
        if selection_start is None or selection_end is None:
            return {
                "name": probe_name(probe, kind),
                "kind": kind,
                "status": "FAIL",
                "value": None,
                "reason": "selection.start and selection.end are required",
            }
        if not service_available(context, "ui.text.assert_selection"):
            return blocked_probe(
                probe,
                kind=kind,
                requested={"selector": dict(probe.get("selector") or {})},
                next_step="Connect a UI backend that exposes ui.text.assert_selection.",
            )
        result = await context.call_adapter(
            "ui.text.assert_selection",
            selector=dict(probe.get("selector") or {}),
            selection_start=selection_start,
            selection_end=selection_end,
        )
        return _selection_probe_output(result, probe=probe, kind=kind)

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


def _state_probe_output(
    result: dict[str, Any],
    *,
    probe: dict[str, Any],
    kind: str,
    phase: str,
) -> dict[str, Any]:
    status = str(result.get("status", "PASS"))
    value = result.get("text", result.get("value"))
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": value,
    }
    for key in (
        "selection",
        "caret_index",
        "focus_within",
        "enabled",
        "visible",
        "source",
    ):
        if key in result:
            output[key] = result[key]
    if status != "PASS":
        output["reason"] = result.get("reason", "text state read failed")
        attach_blocked_details(output, result)
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    return attach_expected_and_status(output, probe=probe, phase=phase, value=value)


def _selection_probe_output(
    result: dict[str, Any],
    *,
    probe: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    status = str(result.get("status", "PASS"))
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": result.get("matched"),
    }
    for key in ("matched", "expected_selection", "actual_selection"):
        if key in result:
            output[key] = result[key]
    if status != "PASS":
        output["reason"] = result.get("reason", "selection assertion failed")
        attach_blocked_details(output, result)
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    return output


def _selection_range_from_probe(probe: dict[str, Any]) -> tuple[int | None, int | None]:
    selection = probe.get("selection")
    if isinstance(selection, dict):
        return _int_or_none(selection.get("start")), _int_or_none(selection.get("end"))
    return _int_or_none(probe.get("selection_start")), _int_or_none(probe.get("selection_end"))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None

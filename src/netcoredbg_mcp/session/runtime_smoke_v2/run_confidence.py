from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INPUT_MONITOR_ADAPTER = "runtime.input_monitor.check"

CLASS_CLEAN_PROVEN = "CLEAN_PROVEN"
CLASS_DIRTY_UNPROVEN = "DIRTY_UNPROVEN"
CLASS_UNPROVEN = "UNPROVEN"


def no_operator_confidence_requested(policy: Mapping[str, Any] | None) -> bool:
    return isinstance(policy, Mapping) and policy.get("no_operator") is True


def confidence_from_monitor_result(
    monitor_result: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    status = str(monitor_result.get("status") or "PASS").upper()
    if status in {"PASS", "CLEAN"}:
        return {
            "classification": CLASS_CLEAN_PROVEN,
            "product_verdict_allowed": True,
            "basis": str(monitor_result.get("basis") or "external_input_monitor"),
        }
    if status in {"DIRTY", CLASS_DIRTY_UNPROVEN}:
        return _dirty_unproven(monitor_result, window=window)
    if status == "BLOCKED":
        reason = str(monitor_result.get("reason") or "input monitor blocked")
        basis = (
            "monitor_unavailable"
            if reason == "service adapter not available"
            else "monitor_blocked"
        )
        return _unproven(reason=reason, basis=basis)
    return _unproven(
        reason=f"input monitor returned unsupported status: {status}",
        basis="monitor_unsupported_status",
    )


def blocked_details_for_confidence(confidence: Mapping[str, Any]) -> dict[str, Any]:
    classification = str(confidence.get("classification") or CLASS_UNPROVEN)
    return {
        "reason": _blocked_reason(classification),
        "requested": {"run_confidence": {"no_operator": True}},
        "accepted": {
            "classification": classification,
            "product_verdict_allowed": False,
        },
        "next_step": str(confidence.get("restart_guidance") or _default_restart_guidance()),
    }


def aggregate_case_confidence(
    cases: list[dict[str, Any]],
    *,
    policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not no_operator_confidence_requested(policy):
        return None
    records = [
        record
        for case in cases
        if isinstance((record := case.get("run_confidence")), dict)
    ]
    for classification in (CLASS_DIRTY_UNPROVEN, CLASS_UNPROVEN):
        selected = _first_classification(records, classification)
        if selected is not None:
            return dict(selected)
    selected = _first_classification(records, CLASS_CLEAN_PROVEN)
    if selected is not None:
        return dict(selected)
    return _unproven(
        reason="input monitor did not produce confidence evidence",
        basis="monitor_not_observed",
    )


def aggregate_transition_confidence(
    transitions: list[dict[str, Any]],
    *,
    policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not no_operator_confidence_requested(policy):
        return None
    records = [
        record
        for transition in transitions
        if isinstance((record := transition.get("run_confidence")), dict)
    ]
    for classification in (CLASS_DIRTY_UNPROVEN, CLASS_UNPROVEN):
        selected = _first_classification(records, classification)
        if selected is not None:
            return dict(selected)
    selected = _first_classification(records, CLASS_CLEAN_PROVEN)
    if selected is not None:
        return dict(selected)
    return _unproven(
        reason="input monitor did not produce confidence evidence",
        basis="monitor_not_observed",
    )


def _dirty_unproven(
    monitor_result: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    contamination = {
        "source": str(monitor_result.get("source") or "external_input"),
        "window": str(monitor_result.get("window") or window),
    }
    if monitor_result.get("summary"):
        contamination["summary"] = str(monitor_result["summary"])
    return {
        "classification": CLASS_DIRTY_UNPROVEN,
        "product_verdict_allowed": False,
        "basis": str(monitor_result.get("basis") or "external_input_monitor"),
        "contamination": contamination,
        "restart_guidance": (
            "Restart the scenario from the beginning after external operator input stops."
        ),
    }


def _unproven(*, reason: str, basis: str) -> dict[str, Any]:
    return {
        "classification": CLASS_UNPROVEN,
        "product_verdict_allowed": False,
        "basis": basis,
        "reason": reason,
        "restart_guidance": (
            f"Connect {INPUT_MONITOR_ADAPTER} or rerun the scenario in a controlled "
            "operator-free window before treating the product verdict as proven."
        ),
    }


def _first_classification(
    records: list[dict[str, Any]],
    classification: str,
) -> dict[str, Any] | None:
    for record in records:
        if record.get("classification") == classification:
            return record
    return None


def _blocked_reason(classification: str) -> str:
    if classification == CLASS_DIRTY_UNPROVEN:
        return "operator input contaminated the scenario"
    return "operator-free scenario confidence is unproven"


def _default_restart_guidance() -> str:
    return (
        f"Connect {INPUT_MONITOR_ADAPTER} or restart the scenario after removing "
        "external operator input."
    )

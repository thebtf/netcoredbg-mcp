from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INPUT_MONITOR_ADAPTER = "runtime.input_monitor.check"

CLASS_CLEAN_PROVEN = "CLEAN_PROVEN"
CLASS_DIRTY_UNPROVEN = "DIRTY_UNPROVEN"
CLASS_UNPROVEN = "UNPROVEN"
CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS = "RUNNER_GLOBAL_INPUT_AMBIGUOUS"
REASON_RUNNER_INPUT_AMBIGUOUS = (
    "input monitor evidence is ambiguous after runner-generated global input"
)


def no_operator_confidence_requested(policy: Mapping[str, Any] | None) -> bool:
    return isinstance(policy, Mapping) and policy.get("no_operator") is True


def confidence_from_monitor_result(
    monitor_result: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    raw_status = monitor_result.get("status")
    if raw_status is None or str(raw_status).strip() == "":
        return _unproven(
            reason="input monitor returned no status",
            basis="monitor_malformed_result",
        )
    status = str(raw_status).upper()
    if status in {"PASS", "CLEAN"}:
        return {
            "classification": CLASS_CLEAN_PROVEN,
            "product_verdict_allowed": True,
            "basis": str(monitor_result.get("basis") or "external_input_monitor"),
        }
    if status in {"DIRTY", CLASS_DIRTY_UNPROVEN}:
        if _has_runner_emulated_input(monitor_result):
            return _runner_global_input_ambiguous(monitor_result, window=window)
        return _dirty_unproven(monitor_result, window=window)
    if status == CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS:
        return _runner_global_input_ambiguous(monitor_result, window=window)
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
        "next_step": str(
            confidence.get("restart_guidance") or _default_restart_guidance()
        ),
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
    for classification in (
        CLASS_DIRTY_UNPROVEN,
        CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS,
        CLASS_UNPROVEN,
    ):
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
    for classification in (
        CLASS_DIRTY_UNPROVEN,
        CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS,
        CLASS_UNPROVEN,
    ):
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


def _has_runner_emulated_input(monitor_result: Mapping[str, Any]) -> bool:
    runner_input = monitor_result.get("runner_input")
    return (
        isinstance(runner_input, Mapping)
        and str(runner_input.get("source") or "") == "runner_emulated_input"
        and str(runner_input.get("kind") or "") == "ui.drag"
        and _monitor_source_allows_runner_ambiguity(monitor_result)
    )


def _monitor_source_allows_runner_ambiguity(
    monitor_result: Mapping[str, Any],
) -> bool:
    source = str(monitor_result.get("source") or "").strip().lower()
    return source in {"", "global_input", "runner_emulated_input"}


def _runner_global_input_ambiguous(
    monitor_result: Mapping[str, Any],
    *,
    window: str,
) -> dict[str, Any]:
    action = monitor_result.get("action")
    runner_input = monitor_result.get("runner_input")
    ambiguity: dict[str, Any] = {
        "action": dict(action) if isinstance(action, Mapping) else {},
        "monitor_source": str(monitor_result.get("source") or "runner_emulated_input"),
        "window": str(monitor_result.get("window") or window),
    }
    if isinstance(runner_input, Mapping):
        ambiguity["runner_input"] = dict(runner_input)
    if monitor_result.get("summary"):
        ambiguity["summary"] = str(monitor_result["summary"])
    return {
        "classification": CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS,
        "product_verdict_allowed": False,
        "basis": str(monitor_result.get("basis") or "runner_input_separation"),
        "reason": REASON_RUNNER_INPUT_AMBIGUOUS,
        "ambiguity": ambiguity,
        "restart_guidance": (
            "Use input_policy.no_global_input=true or a supported runner-input "
            "separation monitor before treating the product verdict as no-operator evidence."
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
    if classification == CLASS_RUNNER_GLOBAL_INPUT_AMBIGUOUS:
        return REASON_RUNNER_INPUT_AMBIGUOUS
    return "operator-free scenario confidence is unproven"


def _default_restart_guidance() -> str:
    return (
        f"Connect {INPUT_MONITOR_ADAPTER} or restart the scenario after removing "
        "external operator input."
    )

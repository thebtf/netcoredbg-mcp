from __future__ import annotations

from dataclasses import replace
from typing import Any

from .actions import ActionContext
from .cleanup import cleanup_steps_from_case, run_cleanup
from .metrics import evaluate_metric_thresholds, merge_case_metrics
from .transition_executor import execute_transition


async def execute_case(
    case: dict[str, Any],
    context: ActionContext,
    *,
    metrics_thresholds: dict[str, Any] | None = None,
    max_actions: int | None = None,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], int]:
    transitions: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    diff: dict[str, Any] = {}
    blocked: dict[str, Any] | None = None
    metrics_records: list[dict[str, Any]] = []
    failed_assertions: list[dict[str, Any]] = []
    action_count = 0
    status = "PASS"
    reason = "case passed"
    deadline = None if timeout_seconds is None else context.clock() + timeout_seconds

    for transition_index, transition in enumerate(case.get("transitions", [])):
        if max_actions is not None and action_count >= max_actions:
            status = "IMPASSE"
            reason = "action budget exhausted"
            break
        remaining = None if deadline is None else deadline - context.clock()
        if remaining is not None and remaining <= 0:
            status = "IMPASSE"
            reason = "elapsed time budget exhausted"
            break
        transition_context = replace(
            context,
            case_id=str(case.get("id") or ""),
            transition_index=transition_index,
        )
        transition_result, transition_actions = await execute_transition(
            dict(transition),
            transition_context,
            timeout_seconds=remaining,
        )
        action_count += transition_actions
        transitions.append(transition_result)
        actions.extend(transition_result.get("actions", []))
        transition_metrics = transition_result.get("metrics")
        if isinstance(transition_metrics, dict):
            metrics_records.append(transition_metrics)
        before.update(transition_result.get("before", {}))
        after.update(transition_result.get("after", {}))
        diff.update(transition_result.get("diff", {}))
        if transition_result["status"] == "FAIL":
            status = "FAIL"
            reason = str(transition_result.get("reason") or "transition failed")
            break
        if transition_result["status"] == "BLOCKED" and status != "FAIL":
            status = "BLOCKED"
            reason = str(transition_result.get("reason") or "transition blocked")
            blocked = transition_result.get("blocked")
            break
        if transition_result["status"] == "IMPASSE" and status not in {"FAIL", "BLOCKED"}:
            status = "IMPASSE"
            reason = str(transition_result.get("reason") or "transition impasse")
            break

    metrics = merge_case_metrics(metrics_records)
    metric_failures = evaluate_metric_thresholds(metrics, metrics_thresholds)
    failed_assertions.extend(metric_failures)
    if metric_failures and status == "PASS":
        status = "FAIL"
        reason = "metric threshold exceeded"

    cleanup = await run_cleanup(
        cleanup_steps_from_case(case),
        context,
        case_id=str(case.get("id") or ""),
    )

    result = {
        "id": case.get("id"),
        "status": status,
        "reason": reason,
        "actions": actions,
        "transitions": transitions,
        "before": before,
        "after": after,
        "diff": diff,
        "metrics": metrics,
        "failed_assertions": failed_assertions,
        "cleanup": cleanup,
    }
    if "rendered_from" in case:
        result["rendered_from"] = dict(case["rendered_from"])
    if blocked is not None:
        result["blocked"] = blocked
    return result, action_count

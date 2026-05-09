from __future__ import annotations

from typing import Any

from .actions import ActionContext
from .transition_executor import execute_transition


async def execute_case(
    case: dict[str, Any],
    context: ActionContext,
) -> tuple[dict[str, Any], int]:
    transitions: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    diff: dict[str, Any] = {}
    blocked: dict[str, Any] | None = None
    action_count = 0
    status = "PASS"
    reason = "case passed"

    for transition in case.get("transitions", []):
        transition_result, transition_actions = await execute_transition(
            dict(transition),
            context,
        )
        action_count += transition_actions
        transitions.append(transition_result)
        actions.extend(transition_result.get("actions", []))
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

    result = {
        "id": case.get("id"),
        "status": status,
        "reason": reason,
        "actions": actions,
        "transitions": transitions,
        "before": before,
        "after": after,
        "diff": diff,
    }
    if "rendered_from" in case:
        result["rendered_from"] = dict(case["rendered_from"])
    if blocked is not None:
        result["blocked"] = blocked
    return result, action_count

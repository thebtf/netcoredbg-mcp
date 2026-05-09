from __future__ import annotations

from typing import Any

from .actions import ActionContext, dispatch_action
from .blocked import build_blocked
from .cleanup import run_cleanup


async def execute_baseline(
    baseline: dict[str, Any] | None,
    context: ActionContext,
) -> dict[str, Any] | None:
    if not baseline:
        return None

    outcomes: list[dict[str, Any]] = []
    cleanup_steps: list[dict[str, Any]] = []
    for raw_step in baseline.get("steps", []):
        step = dict(raw_step)
        step_id = str(step.get("id") or f"baseline-{len(outcomes) + 1}")
        kind = str(step.get("kind") or "")
        result = await _execute_step(step, context)
        status = str(result.get("status", "PASS"))
        outcome = {
            "id": step_id,
            "kind": kind,
            "status": status,
            "result": result,
        }
        outcomes.append(outcome)
        if status == "PASS":
            cleanup_step = _cleanup_step_for(step)
            if cleanup_step is not None:
                cleanup_steps.append(cleanup_step)
            continue

        cleanup = await run_cleanup(cleanup_steps, context)
        blocked = build_blocked(
            reason="baseline setup failed",
            requested={"step_id": step_id, "kind": kind},
            accepted={"baseline_step_kinds": _accepted_step_kinds()},
            next_step="Fix the failing baseline step before running cases.",
        )
        return {
            "status": "BLOCKED",
            "reason": "baseline setup failed",
            "failed_step_id": step_id,
            "failed_step": outcome,
            "steps": outcomes,
            "cleanup": cleanup,
            "blocked": blocked,
        }

    return {
        "status": "PASS",
        "steps": outcomes,
        "cleanup": {"status": "PASS", "attempted": [], "failures": []},
    }


async def _execute_step(step: dict[str, Any], context: ActionContext) -> dict[str, Any]:
    kind = str(step.get("kind") or "")
    if kind == "fixture.restore":
        return await context.call_adapter(
            "fixture.restore",
            path=str(step.get("path") or ""),
            baseline_file=str(step.get("baseline_file") or ""),
        )
    if kind == "isolated_profile.launch":
        launch_args = dict(step.get("launch") or {})
        return await context.call_adapter("launch", **launch_args)
    if kind == "control_set":
        action = dict(step.get("action") or {})
        return await dispatch_action(action, context)
    return {
        "status": "BLOCKED",
        "reason": "unsupported baseline step kind",
        "accepted": {"baseline_step_kinds": _accepted_step_kinds()},
    }


def _cleanup_step_for(step: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(step.get("kind") or "")
    if kind == "fixture.restore":
        return {
            "kind": "fixture.restore",
            "path": str(step.get("path") or ""),
            "baseline_file": str(step.get("baseline_file") or ""),
        }
    return None


def _accepted_step_kinds() -> list[str]:
    return ["control_set", "fixture.restore", "isolated_profile.launch"]

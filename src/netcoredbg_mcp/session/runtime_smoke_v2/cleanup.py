from __future__ import annotations

from typing import Any

from .actions import ActionContext


async def run_cleanup(
    cleanup_steps: list[dict[str, Any]],
    context: ActionContext,
    *,
    case_id: str | None = None,
) -> dict[str, Any]:
    attempted: list[str] = []
    failures: list[dict[str, Any]] = []
    tracepoints_removed = 0
    isolated_profiles_torn_down = 0
    debug_stop: dict[str, Any] | None = None
    process_registry_after: int | None = None

    for step in _cleanup_execution_order(cleanup_steps):
        kind = str(step.get("kind") or "")
        if kind == "fixture.restore":
            path = str(step.get("path") or "")
            attempted.append(f"fixture.restore:{path}")
            result = await context.call_adapter(
                "fixture.restore",
                path=path,
                baseline_file=str(step.get("baseline_file") or ""),
            )
        elif kind == "debug.tracepoint.remove":
            tracepoint_id = str(step.get("id") or step.get("tracepoint_id") or "")
            attempted.append(f"debug.tracepoint.remove:{tracepoint_id}")
            result = await context.call_adapter(
                "debug.tracepoint.remove",
                tracepoint_id=tracepoint_id,
            )
            if str(result.get("status", "PASS")) == "PASS":
                tracepoints_removed += 1
        elif kind == "isolated_profile.teardown":
            profile = str(step.get("profile") or "auto")
            attempted.append(f"isolated_profile.teardown:{profile}")
            result = await context.call_adapter(
                "isolated_profile.teardown",
                profile=profile,
            )
            if str(result.get("status", "PASS")) == "PASS":
                isolated_profiles_torn_down += 1
        elif kind == "debug.stop":
            mode = str(step.get("mode") or "graceful")
            attempted.append(f"debug.stop:{mode}")
            result = await context.call_adapter("debug.stop", mode=mode)
            debug_stop = {
                "status": str(result.get("status", "PASS")),
                "mode": mode,
                "result": result,
            }
        elif kind == "process.registry.assert_empty":
            attempted.append("process.registry.assert_empty")
            result = await context.call_adapter("process.registry.count")
            if str(result.get("status", "PASS")) == "PASS":
                raw_count = result.get("count", 0)
                try:
                    process_registry_after = int(raw_count)
                except (TypeError, ValueError):
                    result = {
                        "status": "FAIL",
                        "reason": "invalid process registry count",
                        "count": raw_count,
                    }
                else:
                    if process_registry_after != 0:
                        result = {
                            "status": "FAIL",
                            "reason": "process registry not empty",
                            "count": process_registry_after,
                        }
        else:
            attempted.append(kind or "unknown")
            result = {
                "status": "BLOCKED",
                "reason": "cleanup step kind not supported",
                "step": step,
            }
        if str(result.get("status", "PASS")) != "PASS":
            failures.append(
                {
                    "kind": kind,
                    "reason": result.get("reason", "cleanup failed"),
                    "result": result,
                }
            )

    cleanup = {
        "status": "FAIL" if failures else "PASS",
        "attempted": attempted,
        "failures": failures,
        "tracepoints_removed": tracepoints_removed,
        "isolated_profiles_torn_down": isolated_profiles_torn_down,
        "debug_stop": debug_stop,
        "process_registry_after": process_registry_after,
    }
    if case_id is not None:
        cleanup["case_id"] = case_id
    return cleanup


def _cleanup_execution_order(cleanup_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(reversed([dict(item) for item in cleanup_steps]))
    registry_asserts = [
        step
        for step in ordered
        if str(step.get("kind") or "") == "process.registry.assert_empty"
    ]
    if not registry_asserts:
        return ordered
    non_registry_steps = [
        step
        for step in ordered
        if str(step.get("kind") or "") != "process.registry.assert_empty"
    ]
    return [*non_registry_steps, *registry_asserts]


def cleanup_steps_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    cleanup = plan.get("cleanup")
    if not isinstance(cleanup, dict):
        return []
    return [dict(step) for step in cleanup.get("steps", []) if isinstance(step, dict)]


def cleanup_steps_from_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(step) for step in case.get("cleanup", []) if isinstance(step, dict)]


def merge_cleanup_results(
    plan_cleanup: dict[str, Any],
    case_cleanups: list[dict[str, Any]],
) -> dict[str, Any]:
    failed_case_cleanups = [
        {
            "case_id": cleanup.get("case_id"),
            "failures": list(cleanup.get("failures", [])),
        }
        for cleanup in case_cleanups
        if cleanup.get("status") == "FAIL"
    ]
    failures = [
        *list(plan_cleanup.get("failures", [])),
        *[
            {
                "kind": "case.cleanup",
                "case_id": cleanup["case_id"],
                "reason": "case cleanup failed",
                "failures": cleanup["failures"],
            }
            for cleanup in failed_case_cleanups
        ],
    ]
    return {
        "status": "FAIL" if failures else "PASS",
        "attempted": [
            *[
                f"case:{cleanup.get('case_id')}:{attempt}"
                for cleanup in case_cleanups
                for attempt in cleanup.get("attempted", [])
            ],
            *list(plan_cleanup.get("attempted", [])),
        ],
        "failures": failures,
        "failed_case_cleanups": failed_case_cleanups,
        "tracepoints_removed": int(plan_cleanup.get("tracepoints_removed", 0))
        + sum(int(cleanup.get("tracepoints_removed", 0)) for cleanup in case_cleanups),
        "isolated_profiles_torn_down": int(plan_cleanup.get("isolated_profiles_torn_down", 0))
        + sum(int(cleanup.get("isolated_profiles_torn_down", 0)) for cleanup in case_cleanups),
        "debug_stop": plan_cleanup.get("debug_stop"),
        "process_registry_after": plan_cleanup.get("process_registry_after"),
    }

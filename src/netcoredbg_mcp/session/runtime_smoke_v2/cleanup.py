from __future__ import annotations

from typing import Any

from .actions import ActionContext


async def run_cleanup(
    cleanup_steps: list[dict[str, Any]],
    context: ActionContext,
) -> dict[str, Any]:
    attempted: list[str] = []
    failures: list[dict[str, Any]] = []

    for step in reversed([dict(item) for item in cleanup_steps]):
        kind = str(step.get("kind") or "")
        if kind == "fixture.restore":
            path = str(step.get("path") or "")
            attempted.append(f"fixture.restore:{path}")
            result = await context.call_adapter(
                "fixture.restore",
                path=path,
                baseline_file=str(step.get("baseline_file") or ""),
            )
        else:
            attempted.append(kind or "unknown")
            result = {
                "status": "BLOCKED",
                "reason": "cleanup step kind not supported",
                "step": step,
            }
        if str(result.get("status", "PASS")) != "PASS":
            failures.append({
                "kind": kind,
                "reason": result.get("reason", "cleanup failed"),
                "result": result,
            })

    return {
        "status": "FAIL" if failures else "PASS",
        "attempted": attempted,
        "failures": failures,
    }

from __future__ import annotations

from typing import Any

from ..runtime_smoke_schema import (
    app_diagnostics_launch_contract,
    app_diagnostics_launch_env,
)
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
        diagnostic_launch = _diagnostic_launch_for_step(step)
        if diagnostic_launch:
            launch_args["env"] = {
                **dict(launch_args.get("env") or {}),
                **app_diagnostics_launch_env(diagnostic_launch),
            }
        result = await context.call_adapter("launch", **launch_args)
        if diagnostic_launch and isinstance(result, dict):
            return {**result, "diagnostic_launch": diagnostic_launch}
        return result
    if kind == "control_set":
        action = dict(step.get("action") or {})
        return await dispatch_action(action, context)
    if kind == "debug_hygiene_preflight":
        return await context.call_adapter(
            "debug_hygiene_preflight",
            file=step.get("file"),
            clear_breakpoints=bool(step.get("clear_breakpoints", True)),
            clear_trace_log=bool(step.get("clear_trace_log", True)),
            clear_exception_filters=bool(step.get("clear_exception_filters", False)),
        )
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


def _diagnostic_launch_for_step(step: dict[str, Any]) -> dict[str, Any] | None:
    app_diagnostics = step.get("app_diagnostics")
    if not isinstance(app_diagnostics, dict):
        return None
    return app_diagnostics_launch_contract(
        evidence_dir=str(app_diagnostics.get("evidence_dir") or ""),
        file_name=str(app_diagnostics.get("file_name") or ""),
    )


def _accepted_step_kinds() -> list[str]:
    return [
        "control_set",
        "debug_hygiene_preflight",
        "fixture.restore",
        "isolated_profile.launch",
    ]

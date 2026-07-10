from __future__ import annotations

from pathlib import Path
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
    *,
    diagnostic_launch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not baseline:
        return None

    outcomes: list[dict[str, Any]] = []
    cleanup_steps: list[dict[str, Any]] = []
    for raw_step in baseline.get("steps", []):
        step = dict(raw_step)
        step_id = str(step.get("id") or f"baseline-{len(outcomes) + 1}")
        kind = str(step.get("kind") or "")
        result = await _execute_step(
            step,
            context,
            diagnostic_launch=diagnostic_launch,
        )
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


async def _execute_step(
    step: dict[str, Any],
    context: ActionContext,
    *,
    diagnostic_launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = str(step.get("kind") or "")
    if kind == "fixture.restore":
        return await context.call_adapter(
            "fixture.restore",
            path=str(step.get("path") or ""),
            baseline_file=str(step.get("baseline_file") or ""),
        )
    if kind == "isolated_profile.launch":
        launch_args = dict(step.get("launch") or {})
        effective_diagnostic_launch = _diagnostic_launch_for_step(
            step,
            fallback=diagnostic_launch,
        )
        effective_diagnostic_launch = _diagnostic_launch_with_boundary(
            effective_diagnostic_launch,
            context,
        )
        if effective_diagnostic_launch:
            launch_args["env"] = {
                **dict(launch_args.get("env") or {}),
                **app_diagnostics_launch_env(effective_diagnostic_launch),
            }
        result = await context.call_adapter("launch", **launch_args)
        if isinstance(result, dict) and str(result.get("status", "PASS")) == "PASS":
            _ensure_default_output_checkpoint(context)
        if effective_diagnostic_launch and isinstance(result, dict):
            return {**result, "diagnostic_launch": effective_diagnostic_launch}
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


def _ensure_default_output_checkpoint(context: ActionContext) -> None:
    """Anchor the implicit "default" output checkpoint at the post-launch position.

    output.since probes omit "checkpoint" and resolve to "default"; without an
    anchor every such probe fails with "output checkpoint not found" even when
    the launch succeeded. The checkpoint is created only when absent so an
    explicit debug.output_checkpoint step or a prior launch keeps ownership.
    """
    session = getattr(context, "session", None)
    runtime_smoke = getattr(session, "runtime_smoke", None)
    checkpoints = getattr(runtime_smoke, "output_checkpoints", None)
    if not isinstance(checkpoints, dict) or "default" in checkpoints:
        return
    if getattr(session, "state", None) is None:
        return
    from ..output_assertions import OutputAssertionService

    OutputAssertionService(session).create_checkpoint("default")


def _diagnostic_launch_for_step(
    step: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    app_diagnostics = step.get("app_diagnostics")
    if not isinstance(app_diagnostics, dict):
        return fallback
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


def _diagnostic_launch_with_boundary(
    contract: dict[str, Any] | None,
    context: ActionContext,
) -> dict[str, Any] | None:
    if not isinstance(contract, dict):
        return contract
    evidence = contract.get("evidence")
    if not isinstance(evidence, dict):
        return contract
    directory = evidence.get("directory")
    if not isinstance(directory, str) or not directory:
        return contract
    boundary = _diagnostic_directory_boundary(directory, context)
    if boundary is None:
        return contract
    return {
        **contract,
        "_launch_boundary_since": {
            "mtime_ns": boundary[0],
            "name": boundary[1],
        },
    }


def _diagnostic_directory_boundary(
    raw_directory: str,
    context: ActionContext,
) -> tuple[int, str] | None:
    try:
        directory = _resolve_diagnostic_launch_path(raw_directory, context)
    except ValueError:
        return None
    if not directory.is_dir():
        return None
    boundary: tuple[int, str] | None = None
    for candidate in directory.glob("*.json"):
        if not candidate.is_file():
            continue
        key = (candidate.stat().st_mtime_ns, candidate.name)
        if boundary is None or key > boundary:
            boundary = key
    return boundary


def _resolve_diagnostic_launch_path(
    raw_path: str,
    context: ActionContext,
) -> Path:
    session = getattr(context, "session", None)
    validate_path = getattr(session, "validate_path", None)
    if callable(validate_path):
        return Path(validate_path(raw_path, must_exist=False))
    return Path(raw_path).resolve()

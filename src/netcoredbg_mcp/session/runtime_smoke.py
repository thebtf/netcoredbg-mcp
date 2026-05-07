"""Session-owned runtime smoke state and bounded scenario execution."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .freshness import DebugFreshnessVerifier
from .state import EvidenceRef

TERMINAL_STATUSES = {"PASS", "FAIL", "BLOCKED", "IMPASSE"}
MAX_COMPACT_TEXT_LENGTH = 240
MAX_COMPACT_LIST_ITEMS = 8

CleanupCallback = Callable[[], None]
CleanupFailure = dict[str, str]
OperationAdapter = Callable[..., Any]


@dataclass
class RuntimeSmokeSession:
    """Mutable runtime smoke state owned by one debug session."""
    instrumentation_groups: dict[str, Any] = field(default_factory=dict)
    output_checkpoints: dict[str, Any] = field(default_factory=dict)
    freshness_evidence: dict[str, Any] = field(default_factory=dict)
    ui_snapshots: dict[str, Any] = field(default_factory=dict)
    ui_event_buffers: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    last_reset_failures: tuple[CleanupFailure, ...] = ()
    _cleanup_callbacks: dict[str, CleanupCallback] = field(default_factory=dict)

    def register_cleanup(self, name: str, callback: CleanupCallback) -> None:
        """Register an idempotent cleanup callback for session reset."""
        if not name:
            raise ValueError("cleanup name is required")
        self._cleanup_callbacks[name] = callback

    def reset(self) -> tuple[CleanupFailure, ...]:
        """Run cleanup callbacks and clear all runtime smoke state."""
        failures: list[CleanupFailure] = []
        for name, callback in list(self._cleanup_callbacks.items()):
            try:
                callback()
            except Exception as exc:
                failures.append({"name": name, "error": str(exc)})

        self.instrumentation_groups.clear()
        self.output_checkpoints.clear()
        self.freshness_evidence.clear()
        self.ui_snapshots.clear()
        self.ui_event_buffers.clear()
        self.evidence_refs.clear()
        self._cleanup_callbacks.clear()
        self.last_reset_failures = tuple(failures)
        return self.last_reset_failures


def compact_group_evidence(
    *,
    group: str,
    breakpoint_count: int,
    tracepoint_count: int,
    hit_count: int = 0,
    trace_log_count: int = 0,
) -> dict[str, int | str]:
    """Return bounded group evidence for pasteable smoke handoffs."""
    return {
        "group": group,
        "breakpoint_count": breakpoint_count,
        "tracepoint_count": tracepoint_count,
        "hit_count": hit_count,
        "trace_log_count": trace_log_count,
    }


def compact_output_evidence(
    *,
    checkpoint: str,
    matched_line_count: int,
    missing_count: int,
    forbidden_count: int,
) -> dict[str, int | str]:
    """Return bounded output assertion evidence for pasteable smoke handoffs."""
    return {
        "checkpoint": checkpoint,
        "matched_line_count": matched_line_count,
        "missing_count": missing_count,
        "forbidden_count": forbidden_count,
    }


class RuntimeSmokeRunner:
    """Run bounded runtime smoke plans with terminal evidence and cleanup."""

    def __init__(
        self,
        session: Any,
        *,
        service_adapters: dict[str, OperationAdapter] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._service_adapters = dict(service_adapters or {})
        self._clock = clock

    async def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        started = self._clock()
        completed_steps: list[dict[str, Any]] = []
        failed_assertions: list[dict[str, Any]] = []
        validation_errors = _validate_plan(plan)
        if validation_errors:
            cleanup = await self._teardown(plan if isinstance(plan, dict) else {})
            return self._finalize(
                status="FAIL",
                reason="invalid plan schema",
                started=started,
                action_count=0,
                completed_steps=completed_steps,
                failed_assertions=failed_assertions,
                cleanup=cleanup,
                extra={"validation_errors": validation_errors},
            )

        budgets = _budgets(plan)
        action_count = 0
        stop_on_first_failed_assertion = bool(
            plan.get("stop_on_first_failed_assertion", True)
        )

        async def execute_step(phase: str, step: dict[str, Any]) -> tuple[str | None, str | None]:
            nonlocal action_count
            if action_count >= budgets["max_actions"]:
                return "IMPASSE", "action budget exhausted"
            if self._clock() - started > budgets["max_elapsed_seconds"]:
                return "IMPASSE", "elapsed time budget exhausted"

            name = str(step["name"])
            args = dict(step.get("args") or {})
            result = await self._execute_operation(name, args)
            status = _terminal_status(result.get("status", "PASS"))
            action_count += 1
            record = {
                "phase": phase,
                "name": name,
                "status": status,
                "result": result,
            }
            if result.get("evidence_refs"):
                record["evidence_refs"] = list(result["evidence_refs"])
            completed_steps.append(record)

            if self._clock() - started > budgets["max_elapsed_seconds"]:
                return "IMPASSE", "elapsed time budget exhausted"
            if status == "PASS":
                return None, None
            if phase == "assertion":
                failed_assertions.append({
                    "name": name,
                    "reason": result.get("reason", "assertion failed"),
                    "result": result,
                })
                if stop_on_first_failed_assertion:
                    return "FAIL", "assertion failed"
                return None, None
            if status == "BLOCKED":
                return "BLOCKED", str(result.get("reason") or "runtime smoke action blocked")
            if status == "IMPASSE":
                return "IMPASSE", str(result.get("reason") or "runtime smoke action impasse")
            return "FAIL", str(result.get("reason") or "runtime smoke action failed")

        terminal_status: str | None = None
        terminal_reason: str | None = None
        for phase, step in _planned_steps(plan):
            terminal_status, terminal_reason = await execute_step(phase, step)
            if terminal_status is not None:
                break

        if terminal_status is None and failed_assertions:
            terminal_status = "FAIL"
            terminal_reason = "assertions failed"
        if terminal_status is None:
            terminal_status = "PASS"
            terminal_reason = "runtime smoke scenario passed"

        cleanup = await self._teardown(plan)
        return self._finalize(
            status=terminal_status,
            reason=terminal_reason,
            started=started,
            action_count=action_count,
            completed_steps=completed_steps,
            failed_assertions=failed_assertions,
            cleanup=cleanup,
        )

    async def _execute_operation(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        adapter = self._service_adapters.get(name)
        if adapter is not None:
            return _result_dict(await _call_adapter(adapter, args))

        if name == "debug_hygiene_preflight":
            service = getattr(self._session, "hygiene", None)
            if service is None:
                return _blocked(name, "hygiene service unavailable")
            return _result_dict(await service.preflight(**args))

        if name == "output_checkpoint":
            service = _output_service(self._session)
            return _result_dict(service.create_checkpoint(str(args.get("name", "default"))))

        if name == "output_assert_since":
            service = _output_service(self._session)
            checkpoint = str(args.get("checkpoint", "default"))
            options = {key: value for key, value in args.items() if key != "checkpoint"}
            return _result_dict(service.assert_since(checkpoint, **options))

        if name == "instrumentation_group_clear":
            service = getattr(self._session, "instrumentation", None)
            if service is None:
                return _blocked(name, "instrumentation service unavailable")
            return _result_dict(await service.clear_group(str(args.get("name", ""))))

        if name == "verify_debug_freshness":
            return _result_dict(DebugFreshnessVerifier(self._session).verify(**args))

        if name == "launch":
            launch = getattr(self._session, "launch", None)
            if launch is None:
                return _blocked(name, "launch service unavailable")
            return {"status": "PASS", "reason": "launch completed", "result": await launch(**args)}

        if name in {"ui_key_sequence", "ui_grid"}:
            return _blocked(name, "ui backend unsupported")

        return _blocked(name, "unsupported runtime smoke operation")

    async def _teardown(self, plan: dict[str, Any]) -> dict[str, Any]:
        teardown = plan.get("teardown") if isinstance(plan, dict) else None
        teardown_config = teardown if isinstance(teardown, dict) else {}
        group_names = [str(name) for name in teardown_config.get("instrumentation_groups", [])]
        reset_runtime_smoke = bool(teardown_config.get("reset_runtime_smoke", True))
        attempted: list[str] = []
        failures: list[dict[str, Any]] = []

        for group_name in group_names:
            attempted.append(f"instrumentation_group_clear:{group_name}")
            result = await self._execute_operation(
                "instrumentation_group_clear",
                {"name": group_name},
            )
            if _terminal_status(result.get("status", "PASS")) != "PASS":
                failures.append({
                    "operation": "instrumentation_group_clear",
                    "name": group_name,
                    "reason": result.get("reason", "instrumentation cleanup failed"),
                    "result": result,
                })

        remaining = _remaining_runtime_smoke_state(self._session)
        if remaining["instrumentation_groups"]:
            failures.append({
                "operation": "runtime_smoke_residue",
                "reason": "runtime smoke state still owns instrumentation groups",
                "remaining_runtime_smoke_state": remaining,
            })

        reset_failures: tuple[CleanupFailure, ...] = ()
        runtime_smoke = getattr(self._session, "runtime_smoke", None)
        if reset_runtime_smoke and runtime_smoke is not None:
            attempted.append("runtime_smoke_reset")
            reset_failures = runtime_smoke.reset()
            for failure in reset_failures:
                failures.append({
                    "operation": "runtime_smoke_reset",
                    "reason": failure.get("error", "runtime smoke reset failed"),
                    "result": dict(failure),
                })

        return {
            "status": "FAIL" if failures else "PASS",
            "attempted": attempted,
            "failures": failures,
            "reset_failures": [dict(failure) for failure in reset_failures],
            "remaining_runtime_smoke_state": remaining,
        }

    def _finalize(
        self,
        *,
        status: str,
        reason: str,
        started: float,
        action_count: int,
        completed_steps: list[dict[str, Any]],
        failed_assertions: list[dict[str, Any]],
        cleanup: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        terminal_status = status
        terminal_reason = reason
        if cleanup["status"] == "FAIL" and terminal_status not in {"FAIL", "IMPASSE"}:
            terminal_status = "FAIL"
            terminal_reason = "teardown failed"
        result = {
            "status": terminal_status,
            "reason": terminal_reason,
            "elapsed_ms": int(max(0.0, self._clock() - started) * 1000),
            "action_count": action_count,
            "completed_steps": completed_steps,
            "failed_assertions": failed_assertions,
            "cleanup": cleanup,
            "evidence_refs": _collect_evidence_refs(completed_steps),
        }
        if extra:
            result.update(extra)
        result["compact"] = compact_runtime_smoke_result(result)
        return result


def compact_runtime_smoke_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a pasteable bounded runtime smoke result."""
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "failed_assertions": _compact_value(result.get("failed_assertions", [])),
        "cleanup": _compact_value(result.get("cleanup", {})),
        "evidence_refs": _compact_value(result.get("evidence_refs", [])),
        "completed_steps": _compact_value(result.get("completed_steps", [])),
    }


def _validate_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be an object"]
    errors: list[str] = []
    for field_name in ("actions", "assertions", "evidence"):
        if field_name in plan and not isinstance(plan[field_name], list):
            errors.append(f"{field_name} must be a list")
    if "preflight" in plan and not isinstance(plan["preflight"], (bool, dict, list)):
        errors.append("preflight must be a boolean, object, or list")
    if "launch" in plan and not isinstance(plan["launch"], dict):
        errors.append("launch must be an object")
    budgets = plan.get("budgets", {})
    if budgets is not None and not isinstance(budgets, dict):
        errors.append("budgets must be an object")
    return errors


def _budgets(plan: dict[str, Any]) -> dict[str, float | int]:
    budgets = dict(plan.get("budgets") or {})
    max_actions = int(budgets.get("max_actions", 25))
    max_elapsed = float(budgets.get("max_elapsed_seconds", 60))
    return {
        "max_actions": max(1, max_actions),
        "max_elapsed_seconds": max(0.001, max_elapsed),
    }


def _planned_steps(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    steps: list[tuple[str, dict[str, Any]]] = []
    preflight = plan.get("preflight")
    if preflight is True:
        steps.append(("preflight", {"name": "debug_hygiene_preflight", "args": {}}))
    elif isinstance(preflight, dict):
        steps.append(("preflight", _step(preflight, "debug_hygiene_preflight")))
    elif isinstance(preflight, list):
        steps.extend(("preflight", _step(item, "debug_hygiene_preflight")) for item in preflight)

    launch = plan.get("launch")
    if isinstance(launch, dict):
        steps.append(("launch", _step(launch, "launch")))

    steps.extend(("action", _step(item)) for item in plan.get("actions", []))
    steps.extend(("assertion", _step(item)) for item in plan.get("assertions", []))
    steps.extend(("evidence", _step(item)) for item in plan.get("evidence", []))
    return steps


def _step(raw: Any, default_name: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"name": default_name or "invalid_step", "args": {}}
    if "name" in raw:
        return {"name": str(raw["name"]), "args": dict(raw.get("args") or {})}
    return {"name": default_name or "invalid_step", "args": dict(raw)}


async def _call_adapter(adapter: OperationAdapter, args: dict[str, Any]) -> Any:
    result = adapter(**args)
    if inspect.isawaitable(result):
        return await result
    return result


def _result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    if isinstance(result, dict):
        return dict(result)
    return {"status": "PASS", "reason": "operation completed", "result": result}


def _output_service(session: Any) -> Any:
    from .output_assertions import OutputAssertionService

    return getattr(session, "output_assertions", None) or OutputAssertionService(session)


def _blocked(name: str, reason: str) -> dict[str, Any]:
    return {"status": "BLOCKED", "reason": reason, "operation": name}


def _terminal_status(value: Any) -> str:
    status = str(value)
    return status if status in TERMINAL_STATUSES else "FAIL"


def _remaining_runtime_smoke_state(session: Any) -> dict[str, Any]:
    runtime_smoke = getattr(session, "runtime_smoke", None)
    if runtime_smoke is None:
        return {"instrumentation_groups": [], "output_checkpoints": []}
    return {
        "instrumentation_groups": sorted(runtime_smoke.instrumentation_groups),
        "output_checkpoints": sorted(runtime_smoke.output_checkpoints),
    }


def _collect_evidence_refs(completed_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for step in completed_steps:
        result = step.get("result", {})
        if isinstance(result, dict):
            refs.extend(dict(ref) for ref in result.get("evidence_refs", []))
    return refs


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        omitted: list[str] = []
        for key, item in value.items():
            if isinstance(item, str) and len(item) > MAX_COMPACT_TEXT_LENGTH:
                omitted.append(key)
                compact[f"{key}_length"] = len(item)
                continue
            compact[key] = _compact_value(item)
        if omitted:
            compact["omitted_fields"] = omitted
        return compact
    if isinstance(value, list):
        compact_items = [_compact_value(item) for item in value[:MAX_COMPACT_LIST_ITEMS]]
        if len(value) > MAX_COMPACT_LIST_ITEMS:
            compact_items.append({
                "omitted_count": len(value) - MAX_COMPACT_LIST_ITEMS,
            })
        return compact_items
    if isinstance(value, str) and len(value) > MAX_COMPACT_TEXT_LENGTH:
        return {
            "text_length": len(value),
            "omitted_fields": ["value"],
        }
    return value

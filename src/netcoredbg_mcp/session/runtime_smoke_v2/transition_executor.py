from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .actions import ActionContext, dispatch_action
from .diff import compute_diff
from .evidence import blocked_details_from_record
from .metrics import capture_metric_snapshot, finish_transition_metrics
from .probe_dispatcher import ProbeContext, dispatch_probe, probe_path, probe_runs_in_phase
from .timing import sleep_ms

DEFAULT_IDLE_MS = 250
DEFAULT_TRACEPOINT_TIMEOUT_MS = 2000
TRACEPOINT_POLL_MS = 50


async def execute_transition(
    transition: dict[str, Any],
    action_context: ActionContext,
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], int]:
    probes = [dict(probe) for probe in transition.get("probes", [])]
    probe_context = ProbeContext(action_context=action_context)
    before_probes = _probes_for_phase(probes, "before")
    after_probes = _probes_for_phase(probes, "after")
    deadline = None if timeout_seconds is None else action_context.clock() + timeout_seconds
    try:
        before_results = await _with_remaining_timeout(
            lambda: _collect_probe_results(before_probes, probe_context, phase="before"),
            deadline=deadline,
            context=action_context,
        )
    except asyncio.TimeoutError:
        return _timeout_transition_result(transition), 0
    before = _probe_value_map(before_probes, before_results)

    metrics_started = capture_metric_snapshot(action_context)
    raw_action = transition.get("action")
    action_count = 0
    actions: list[dict[str, Any]] = []
    if raw_action is not None:
        try:
            action_result = await _with_remaining_timeout(
                lambda: dispatch_action(dict(raw_action), action_context),
                deadline=deadline,
                context=action_context,
            )
        except asyncio.TimeoutError:
            return (
                _timeout_transition_result(
                    transition,
                    metrics=finish_transition_metrics(metrics_started, action_context),
                    before=before,
                    before_results=before_results,
                ),
                0,
            )
        action_count = 1
        actions = [action_result]
        if action_result.get("status") != "PASS":
            status = _status_from_records([action_result])
            reason = str(action_result.get("reason") or "action failed")
            if _action_status_expected(dict(raw_action), status):
                try:
                    settle = await _with_remaining_timeout(
                        lambda: _settle(dict(transition.get("settle") or {}), action_context),
                        deadline=deadline,
                        context=action_context,
                    )
                except asyncio.TimeoutError:
                    return (
                        _timeout_transition_result(
                            transition,
                            actions=actions,
                            metrics=finish_transition_metrics(metrics_started, action_context),
                            before=before,
                            before_results=before_results,
                        ),
                        action_count,
                    )
                metrics = finish_transition_metrics(metrics_started, action_context)
                try:
                    after_results = await _with_remaining_timeout(
                        lambda: _collect_probe_results(after_probes, probe_context, phase="after"),
                        deadline=deadline,
                        context=action_context,
                    )
                except asyncio.TimeoutError:
                    return (
                        _timeout_transition_result(
                            transition,
                            actions=actions,
                            metrics=metrics,
                            before=before,
                            before_results=before_results,
                            settle=settle,
                        ),
                        action_count,
                    )
                after = _probe_value_map(after_probes, after_results)
                diff = compute_diff(before=before, after=after)
                evidence_status = _status_from_records(
                    [*before_results, *after_results, settle]
                )
                if evidence_status == "FAIL":
                    status = "FAIL"
                    reason = _reason_from_records([*after_results, *before_results, settle])
                result = {
                    "id": transition.get("id"),
                    "status": status,
                    "reason": reason,
                    "actions": actions,
                    "metrics": metrics,
                    "settle": settle,
                    "before": before,
                    "after": after,
                    "diff": diff,
                    "probes": {"before": before_results, "after": after_results},
                }
                if status == "BLOCKED":
                    result["blocked"] = _blocked_from_record(action_result)
                return result, action_count
            result = {
                "id": transition.get("id"),
                "status": status,
                "reason": reason,
                "actions": actions,
                "metrics": finish_transition_metrics(metrics_started, action_context),
                "before": before,
                "after": {},
                "diff": {},
                "probes": {"before": before_results, "after": []},
            }
            if status == "BLOCKED":
                result["blocked"] = _blocked_from_record(action_result)
            return result, action_count

    try:
        settle = await _with_remaining_timeout(
            lambda: _settle(dict(transition.get("settle") or {}), action_context),
            deadline=deadline,
            context=action_context,
        )
    except asyncio.TimeoutError:
        return (
            _timeout_transition_result(
                transition,
                actions=actions,
                metrics=finish_transition_metrics(metrics_started, action_context),
                before=before,
                before_results=before_results,
            ),
            action_count,
        )
    metrics = finish_transition_metrics(metrics_started, action_context)
    if settle.get("status") != "PASS":
        status = _status_from_records([*before_results, settle])
        reason = _reason_from_records([settle, *before_results])
        return (
            {
                "id": transition.get("id"),
                "status": status,
                "reason": reason,
                "actions": actions,
                "metrics": metrics,
                "settle": settle,
                "before": before,
                "after": {},
                "diff": {},
                "probes": {"before": before_results, "after": []},
                **({"blocked": _blocked_from_record(settle)} if status == "BLOCKED" else {}),
            },
            action_count,
        )
    try:
        after_results = await _with_remaining_timeout(
            lambda: _collect_probe_results(after_probes, probe_context, phase="after"),
            deadline=deadline,
            context=action_context,
        )
    except asyncio.TimeoutError:
        return (
            _timeout_transition_result(
                transition,
                actions=actions,
                metrics=metrics,
                before=before,
                before_results=before_results,
                settle=settle,
            ),
            action_count,
        )
    after = _probe_value_map(after_probes, after_results)
    diff = compute_diff(before=before, after=after)

    status = _status_from_records([*before_results, *after_results, settle])
    reason = _reason_from_records([*after_results, *before_results, settle])
    blocked_record = _find_blocked_record([*before_results, *after_results])
    return (
        {
            "id": transition.get("id"),
            "status": status,
            "reason": reason,
            "actions": actions,
            "metrics": metrics,
            "settle": settle,
            "before": before,
            "after": after,
            "diff": diff,
            "probes": {"before": before_results, "after": after_results},
            **({"blocked": _blocked_from_record(blocked_record)} if status == "BLOCKED" else {}),
        },
        action_count,
    )


async def _with_remaining_timeout(
    awaitable_factory: Callable[[], Awaitable[Any]],
    *,
    deadline: float | None,
    context: ActionContext,
) -> Any:
    if deadline is None:
        return await awaitable_factory()
    remaining = deadline - context.clock()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return await asyncio.wait_for(awaitable_factory(), timeout=max(0.001, remaining))


def _timeout_transition_result(
    transition: dict[str, Any],
    *,
    actions: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    before_results: list[dict[str, Any]] | None = None,
    settle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": transition.get("id"),
        "status": "IMPASSE",
        "reason": "elapsed time budget exhausted",
        "actions": list(actions or []),
        "metrics": dict(metrics or {}),
        **({"settle": settle} if settle is not None else {}),
        "before": dict(before or {}),
        "after": {},
        "diff": {},
        "probes": {"before": list(before_results or []), "after": []},
    }


def _probes_for_phase(probes: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    return [probe for probe in probes if probe_runs_in_phase(probe, phase)]


async def _collect_probe_results(
    probes: list[dict[str, Any]],
    context: ProbeContext,
    *,
    phase: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for probe in probes:
        result = await dispatch_probe(probe, context, phase=phase)
        result["path"] = probe_path(probe)
        results.append(result)
    return results


def _probe_value_map(
    probes: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        probe_path(probe): result.get("value")
        for probe, result in zip(probes, results, strict=True)
    }


async def _settle(
    settle: dict[str, Any],
    context: ActionContext,
) -> dict[str, Any]:
    tracepoint_id = settle.get("await_tracepoint_id")
    if tracepoint_id:
        timeout_ms = int(settle.get("tracepoint_timeout_ms", DEFAULT_TRACEPOINT_TIMEOUT_MS))
        deadline = context.clock() + (timeout_ms / 1000)
        while context.clock() <= deadline:
            result = await context.call_adapter(
                "debug.tracepoint_status",
                tracepoint_id=str(tracepoint_id),
            )
            if result.get("status") == "PASS" and result.get("hit") is True:
                return {
                    "status": "PASS",
                    "await_tracepoint_id": str(tracepoint_id),
                    "tracepoint_timeout_ms": timeout_ms,
                }
            if context.clock() >= deadline:
                break
            await _sleep_ms(context, TRACEPOINT_POLL_MS)
        return {
            "status": "BLOCKED",
            "reason": "settle condition not met",
            "await_tracepoint_id": str(tracepoint_id),
            "tracepoint_timeout_ms": timeout_ms,
        }

    idle_ms = int(settle.get("idle_ms", DEFAULT_IDLE_MS))
    await _sleep_ms(context, idle_ms)
    return {"status": "PASS", "idle_ms": idle_ms}


async def _sleep_ms(context: ActionContext, idle_ms: int) -> None:
    await sleep_ms(context.clock, idle_ms)


def _action_status_expected(action: dict[str, Any], status: str) -> bool:
    expect = action.get("expect")
    if not isinstance(expect, dict):
        return False
    return str(expect.get("status") or "") == status


def _status_from_records(records: list[dict[str, Any]]) -> str:
    statuses = [str(record.get("status", "PASS")) for record in records]
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "IMPASSE" in statuses:
        return "IMPASSE"
    return "PASS"


def _reason_from_records(records: list[dict[str, Any]]) -> str:
    for preferred_status in ("FAIL", "BLOCKED", "IMPASSE"):
        for record in records:
            if record.get("status") == preferred_status:
                return str(record.get("reason") or preferred_status.lower())
    return "transition passed"


def _blocked_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return blocked_details_from_record(record)


def _find_blocked_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in records:
        if record.get("status") == "BLOCKED":
            return record
    return {}

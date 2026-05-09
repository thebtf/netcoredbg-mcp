from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..runtime_smoke_schema import (
    ACCEPTED_SCHEMA_VALUES,
    ACCEPTED_TOP_LEVEL_KEYS_V2,
)
from .actions import ActionContext, accepted_action_kinds, dispatch_action
from .probes import accepted_probe_kinds
from .result_envelope import finalize_result


def compact_v2_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "generated_case_count": result.get("generated_case_count", 0),
        "case_count": len(result.get("cases", [])),
        "cleanup": result.get("cleanup", {}),
    }


class RuntimeStateOracleRunner:
    def __init__(
        self,
        session: Any,
        *,
        service_adapters: dict[str, Callable[..., Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._service_adapters = dict(service_adapters or {})
        self._clock = clock

    async def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        started = self._clock()
        cleanup = {"status": "PASS", "attempted": [], "failures": []}
        validation_errors = _validate_v2_plan(plan)
        if validation_errors:
            return self._finalize(
                status="FAIL",
                reason="invalid plan schema",
                started=started,
                action_count=0,
                cases=[],
                cleanup=cleanup,
                extra={"validation_errors": validation_errors},
            )
        if not plan.get("cases"):
            return self._finalize(
                status="BLOCKED",
                reason="runtime smoke v2 plan has no cases to execute",
                started=started,
                action_count=0,
                cases=[],
                cleanup=cleanup,
                extra={
                    "blocked": {
                        "reason": "no cases declared",
                        "requested": {"cases": []},
                        "accepted": {"top_level_keys": list(ACCEPTED_TOP_LEVEL_KEYS_V2)},
                        "next_step": "Add at least one case with transitions.",
                    }
                },
            )

        context = ActionContext(
            service_adapters=self._service_adapters,
            clock=self._clock,
        )
        case_results: list[dict[str, Any]] = []
        action_count = 0
        terminal_status = "PASS"
        terminal_reason = "runtime smoke v2 scenario passed"
        blocked_payload: dict[str, Any] | None = None

        for case in plan.get("cases", []):
            case_result, executed_actions = await self._run_case(case, context)
            action_count += executed_actions
            case_results.append(case_result)
            if case_result["status"] == "BLOCKED":
                terminal_status = "BLOCKED"
                terminal_reason = case_result["reason"]
                blocked_payload = case_result.get("blocked")
                break
            if case_result["status"] == "FAIL":
                terminal_status = "FAIL"
                terminal_reason = case_result["reason"]
                break

        return self._finalize(
            status=terminal_status,
            reason=terminal_reason,
            started=started,
            action_count=action_count,
            cases=case_results,
            cleanup=cleanup,
            extra={"blocked": blocked_payload} if blocked_payload else None,
        )

    async def _run_case(
        self,
        case: dict[str, Any],
        context: ActionContext,
    ) -> tuple[dict[str, Any], int]:
        action_records: list[dict[str, Any]] = []
        action_count = 0
        for transition in case.get("transitions", []):
            action = dict(transition.get("action") or {})
            action_result = await dispatch_action(action, context)
            action_count += 1
            action_records.append(action_result)
            if action_result.get("status") == "BLOCKED":
                return (
                    {
                        "id": case.get("id"),
                        "status": "BLOCKED",
                        "reason": str(action_result.get("reason") or "action blocked"),
                        "actions": action_records,
                        "blocked": _blocked_from_action(action_result),
                    },
                    action_count,
                )
            if action_result.get("status") == "FAIL":
                return (
                    {
                        "id": case.get("id"),
                        "status": "FAIL",
                        "reason": str(action_result.get("reason") or "action failed"),
                        "actions": action_records,
                    },
                    action_count,
                )
        return (
            {
                "id": case.get("id"),
                "status": "PASS",
                "reason": "case passed",
                "actions": action_records,
            },
            action_count,
        )

    def _finalize(
        self,
        *,
        status: str,
        reason: str,
        started: float,
        action_count: int,
        cases: list[dict[str, Any]],
        cleanup: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result_extra = {
            "generated_case_count": 0,
            "cases": cases,
            "baseline": None,
            "metrics_thresholds": None,
            "accepted_schema_values": list(ACCEPTED_SCHEMA_VALUES),
            "accepted_top_level_keys_v2": list(ACCEPTED_TOP_LEVEL_KEYS_V2),
            "accepted_action_kinds": accepted_action_kinds(),
            "accepted_probe_kinds": accepted_probe_kinds(),
        }
        if extra:
            result_extra.update(extra)
        return finalize_result(
            status=status,
            reason=reason,
            elapsed_ms=int(max(0.0, self._clock() - started) * 1000),
            action_count=action_count,
            completed_steps=[],
            failed_assertions=[],
            cleanup=cleanup,
            evidence_refs=[],
            compact_builder=compact_v2_result,
            extra=result_extra,
        )


def _validate_v2_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cases = plan.get("cases", [])
    if not isinstance(cases, list):
        return ["cases must be a list"]
    known_actions = set(accepted_action_kinds())
    known_probes = set(accepted_probe_kinds())
    for case_index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{case_index}] must be an object")
            continue
        transitions = case.get("transitions", [])
        if not isinstance(transitions, list):
            errors.append(f"cases[{case_index}].transitions must be a list")
            continue
        for transition_index, transition in enumerate(transitions):
            if not isinstance(transition, dict):
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}] must be an object"
                )
                continue
            action = transition.get("action")
            if not isinstance(action, dict):
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}].action must be an object"
                )
            elif action.get("kind") not in known_actions:
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}].action.kind "
                    f"is not accepted: {action.get('kind')}"
                )
            probes = transition.get("probes", [])
            if not isinstance(probes, list):
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}].probes must be a list"
                )
                continue
            for probe_index, probe in enumerate(probes):
                if not isinstance(probe, dict):
                    errors.append(
                        f"cases[{case_index}].transitions[{transition_index}]."
                        f"probes[{probe_index}] must be an object"
                    )
                    continue
                if probe.get("kind") not in known_probes:
                    errors.append(
                        f"cases[{case_index}].transitions[{transition_index}]."
                        f"probes[{probe_index}].kind is not accepted: {probe.get('kind')}"
                    )
    return errors


def _blocked_from_action(action_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": action_result["reason"],
        "requested": action_result["requested"],
        "accepted": action_result["accepted"],
        "next_step": action_result["next_step"],
    }

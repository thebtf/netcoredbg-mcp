from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..runtime_smoke_schema import (
    ACCEPTED_SCHEMA_VALUES,
    ACCEPTED_TOP_LEVEL_KEYS_V2,
)
from .actions import ActionContext, accepted_action_kinds
from .baseline import execute_baseline
from .case_executor import execute_case
from .cleanup import cleanup_steps_from_plan, merge_cleanup_results, run_cleanup
from .generate import expand_generated_cases
from .probe_dispatcher import accepted_probe_phases, probe_path, probe_runs_in_phase
from .probes import accepted_probe_kinds
from .result_envelope import compact_value, finalize_result


def compact_v2_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "generated_case_count": result.get("generated_case_count", 0),
        "case_count": len(result.get("cases", [])),
        "cleanup": compact_value(result.get("cleanup", {})),
        "evidence_refs": compact_value(result.get("evidence_refs", [])),
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
        raw_metrics_thresholds = plan.get("metrics_thresholds")
        metrics_thresholds = (
            dict(raw_metrics_thresholds) if isinstance(raw_metrics_thresholds, dict) else None
        )
        cases, generated_case_count, generation_errors = _cases_for_execution(plan)
        validation_errors = [
            *generation_errors,
            *_validate_v2_plan(plan, cases=cases),
        ]
        if validation_errors:
            return self._finalize(
                status="INVALID_SETUP",
                reason="invalid plan schema",
                started=started,
                action_count=0,
                cases=[],
                generated_case_count=generated_case_count,
                metrics_thresholds=metrics_thresholds,
                baseline=None,
                cleanup=cleanup,
                extra={"validation_errors": validation_errors},
            )
        if not cases:
            return self._finalize(
                status="INVALID_SETUP",
                reason="runtime smoke v2 plan has no cases to execute",
                started=started,
                action_count=0,
                cases=[],
                generated_case_count=generated_case_count,
                metrics_thresholds=metrics_thresholds,
                baseline=None,
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
            session=self._session,
        )
        baseline_result = await execute_baseline(
            plan.get("baseline") if isinstance(plan.get("baseline"), dict) else None,
            context,
        )
        if baseline_result is not None and baseline_result.get("status") != "PASS":
            baseline_blocked_payload = baseline_result.get("blocked")
            plan_cleanup = await run_cleanup(cleanup_steps_from_plan(plan), context)
            cleanup = merge_cleanup_results(plan_cleanup, [])
            return self._finalize(
                status="INVALID_SETUP",
                reason="baseline setup failed",
                started=started,
                action_count=0,
                cases=[],
                generated_case_count=generated_case_count,
                metrics_thresholds=metrics_thresholds,
                baseline=baseline_result,
                cleanup=cleanup,
                extra=({"blocked": baseline_blocked_payload} if baseline_blocked_payload else None),
            )

        case_results: list[dict[str, Any]] = []
        case_cleanups: list[dict[str, Any]] = []
        action_count = 0
        terminal_status = "PASS"
        terminal_reason = "runtime smoke v2 scenario passed"
        blocked_payload: dict[str, Any] | None = None

        for case in cases:
            case_result, executed_actions = await execute_case(
                case,
                context,
                metrics_thresholds=metrics_thresholds,
            )
            action_count += executed_actions
            case_results.append(case_result)
            if isinstance(case_result.get("cleanup"), dict):
                case_cleanups.append(case_result["cleanup"])
            if case_result["status"] == "BLOCKED":
                terminal_status = "BLOCKED"
                terminal_reason = case_result["reason"]
                blocked_payload = case_result.get("blocked")
                break
            if case_result["status"] == "FAIL":
                terminal_status = "FAIL"
                terminal_reason = case_result["reason"]
                break
            if case_result.get("cleanup", {}).get("status") == "FAIL" and bool(
                plan.get("stop_on_first_failed_assertion")
            ):
                terminal_status = "FAIL"
                terminal_reason = "case cleanup failed"
                break

        plan_cleanup = await run_cleanup(cleanup_steps_from_plan(plan), context)
        cleanup = merge_cleanup_results(plan_cleanup, case_cleanups)

        return self._finalize(
            status=terminal_status,
            reason=terminal_reason,
            started=started,
            action_count=action_count,
            cases=case_results,
            generated_case_count=generated_case_count,
            metrics_thresholds=metrics_thresholds,
            baseline=baseline_result,
            cleanup=cleanup,
            extra={"blocked": blocked_payload} if blocked_payload else None,
        )

    def _finalize(
        self,
        *,
        status: str,
        reason: str,
        started: float,
        action_count: int,
        cases: list[dict[str, Any]],
        generated_case_count: int,
        metrics_thresholds: dict[str, Any] | None,
        baseline: dict[str, Any] | None,
        cleanup: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result_extra = {
            "generated_case_count": generated_case_count,
            "cases": cases,
            "baseline": baseline,
            "metrics_thresholds": metrics_thresholds,
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
            evidence_refs=_collect_v2_evidence_refs(cases),
            compact_builder=compact_v2_result,
            extra=result_extra,
        )


def _cases_for_execution(
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    raw_cases = plan.get("cases", [])
    cases = [dict(case) for case in raw_cases] if isinstance(raw_cases, list) else []
    generated_cases, generation_errors = expand_generated_cases(plan)
    return [*cases, *generated_cases], len(generated_cases), generation_errors


def _validate_v2_plan(
    plan: dict[str, Any],
    *,
    cases: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    cases = plan.get("cases", []) if cases is None else cases
    if not isinstance(cases, list):
        return ["cases must be a list"]
    seen_case_ids: set[str] = set()
    known_actions = set(accepted_action_kinds())
    known_probes = set(accepted_probe_kinds())
    for case_index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{case_index}] must be an object")
            continue
        case_id = str(case.get("id") or "")
        if case_id in seen_case_ids:
            errors.append(f"duplicate case id: {case_id}")
        seen_case_ids.add(case_id)
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
            if action is None:
                pass
            elif not isinstance(action, dict):
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}].action must be an object"
                )
            elif not action:
                errors.append(
                    f"cases[{case_index}].transitions[{transition_index}].action.kind "
                    "is required when action is present"
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
            seen_probe_paths: set[str] = set()
            for probe_index, probe in enumerate(probes):
                if not isinstance(probe, dict):
                    errors.append(
                        f"cases[{case_index}].transitions[{transition_index}]."
                        f"probes[{probe_index}] must be an object"
                    )
                    continue
                phase_error = _probe_phase_error(probe)
                if phase_error is not None:
                    errors.append(
                        f"cases[{case_index}].transitions[{transition_index}]."
                        f"probes[{probe_index}].{phase_error}"
                    )
                    continue
                if probe.get("kind") not in known_probes:
                    errors.append(
                        f"cases[{case_index}].transitions[{transition_index}]."
                        f"probes[{probe_index}].kind is not accepted: {probe.get('kind')}"
                    )
                    continue
                path = probe_path(probe)
                for phase in ("before", "after"):
                    if not probe_runs_in_phase(probe, phase):
                        continue
                    phase_path = f"{phase}:{path}"
                    if phase_path in seen_probe_paths:
                        errors.append(
                            f"cases[{case_index}].transitions[{transition_index}] "
                            f"has duplicate probe path for {phase}: {path}"
                        )
                    seen_probe_paths.add(phase_path)
    return errors


def _probe_phase_error(probe: dict[str, Any]) -> str | None:
    if "phase" in probe and "phases" in probe:
        return "phase must not be combined with phases"
    accepted = set(accepted_probe_phases())
    if "phase" in probe:
        phase = str(probe.get("phase"))
        if phase not in accepted:
            return f"phase is not accepted: {probe.get('phase')}"
    if "phases" in probe:
        raw_phases = probe.get("phases")
        if isinstance(raw_phases, str):
            phases = [raw_phases]
        elif isinstance(raw_phases, (list, tuple, set)):
            phases = [str(item) for item in raw_phases]
        else:
            return "phases must be a list of accepted phase names"
        if not phases:
            return "phases must contain at least one accepted phase name"
        invalid = [phase for phase in phases if phase not in accepted]
        if invalid:
            return f"phases contains unaccepted values: {invalid}"
    return None


def _collect_v2_evidence_refs(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for case in cases:
        case_id = case.get("id")
        for transition in case.get("transitions", []):
            probes = transition.get("probes", {})
            if not isinstance(probes, dict):
                continue
            for phase in ("before", "after"):
                for probe in probes.get(phase, []):
                    if not isinstance(probe, dict):
                        continue
                    evidence_ref = probe.get("evidence_ref")
                    if evidence_ref:
                        refs.append(
                            {
                                "case_id": case_id,
                                "phase": phase,
                                "probe": probe.get("name"),
                                "evidence_ref": evidence_ref,
                            }
                        )
    return refs

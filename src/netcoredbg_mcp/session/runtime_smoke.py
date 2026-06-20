"""Session-owned runtime smoke state and bounded scenario execution."""

from __future__ import annotations

import asyncio
import inspect
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .freshness import DebugFreshnessVerifier
from .runtime_smoke_schema import (
    SCHEMA_VERSION_V2,
    normalize_plan_step,
    schema_help_fields,
    validate_plan,
)
from .runtime_smoke_v2.result_envelope import (
    compact_runtime_smoke_result,
    compact_value,
    finalize_result,
)
from .state import EvidenceRef

TERMINAL_STATUSES = {"PASS", "FAIL", "BLOCKED", "IMPASSE", "INVALID_SETUP"}
RESTORE_RETRY_DELAYS_SECONDS = (0.1, 0.2, 0.5, 1.0, 2.0, 4.0)
UI_OPERATION_PREFIXES = ("ui.",)
UI_OPERATION_NAMES = {"ui_key_sequence", "ui_grid"}

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
    lifecycle_runs: RuntimeSmokeRunRegistry = field(
        default_factory=lambda: RuntimeSmokeRunRegistry(),
        repr=False,
    )
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
        failed_names: set[str] = set()
        for name, callback in list(self._cleanup_callbacks.items()):
            try:
                callback()
            except Exception as exc:
                # Continue teardown so one failed cleanup cannot hide later failures.
                failures.append({"name": name, "error": str(exc)})
                failed_names.add(name)

        self.instrumentation_groups.clear()
        self.output_checkpoints.clear()
        self.freshness_evidence.clear()
        self.ui_snapshots.clear()
        self.ui_event_buffers.clear()
        self.evidence_refs.clear()
        for name in list(self._cleanup_callbacks):
            if name not in failed_names:
                self._cleanup_callbacks.pop(name, None)
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
        self._case_progress_notifier: Callable[[dict[str, Any]], Any] | None = None

    def attach_case_progress_notifier(
        self,
        notifier: Callable[[dict[str, Any]], Any] | None,
    ) -> None:
        self._case_progress_notifier = notifier

    async def run(self, plan: Any) -> dict[str, Any]:
        started = self._clock()
        completed_steps: list[dict[str, Any]] = []
        failed_assertions: list[dict[str, Any]] = []
        validation_errors = validate_plan(plan)
        if not validation_errors and isinstance(plan, dict):
            validation_errors.extend(self._validate_restore_paths(plan))
        if validation_errors:
            status = (
                "INVALID_SETUP"
                if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2
                else "FAIL"
            )
            cleanup = await self._teardown(
                plan if isinstance(plan, dict) else {},
                allow_restore=False,
                allow_plan_cleanup=False,
            )
            return self._finalize(
                status=status,
                reason="invalid plan schema",
                started=started,
                action_count=0,
                completed_steps=completed_steps,
                failed_assertions=failed_assertions,
                cleanup=cleanup,
                extra={
                    "validation_errors": validation_errors,
                    **schema_help_fields(plan if isinstance(plan, dict) else None),
                },
            )

        if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2:
            from .runtime_smoke_v2 import RuntimeStateOracleRunner

            return await RuntimeStateOracleRunner(
                self._session,
                service_adapters=self._service_adapters,
                clock=self._clock,
                case_progress_notifier=self._case_progress_notifier,
            ).run(plan)

        budgets = _budgets(plan)
        action_count = 0
        stop_on_first_failed_assertion = bool(plan.get("stop_on_first_failed_assertion", True))

        async def execute_step(phase: str, step: dict[str, Any]) -> tuple[str | None, str | None]:
            nonlocal action_count
            if action_count >= budgets["max_actions"]:
                return "IMPASSE", "action budget exhausted"
            if self._clock() - started > budgets["max_elapsed_seconds"]:
                return "IMPASSE", "elapsed time budget exhausted"

            name = str(step["name"])
            args = dict(step.get("args") or {})
            try:
                result = await self._execute_operation(name, args)
            except Exception as exc:
                result = {
                    "status": "FAIL",
                    "reason": "runtime smoke operation raised exception",
                    "operation": name,
                    "exception": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
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
                failed_assertions.append(
                    {
                        "name": name,
                        "reason": result.get("reason", "assertion failed"),
                        "result": result,
                    }
                )
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
        assert terminal_reason is not None
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

        if name == "fixture.restore":
            return self._restore_file(args)

        if name in {"ui_key_sequence", "ui_grid"}:
            return _blocked(name, "ui backend unsupported")

        return _blocked(name, "unsupported runtime smoke operation")

    async def _teardown(
        self,
        plan: dict[str, Any],
        *,
        allow_restore: bool = True,
        allow_plan_cleanup: bool = True,
    ) -> dict[str, Any]:
        cleanup_config = (
            _merged_cleanup_config(plan)
            if allow_plan_cleanup
            else {
                "instrumentation_groups": [],
                "restore_files": [],
                "reset_runtime_smoke": True,
            }
        )
        group_names = [str(name) for name in cleanup_config.get("instrumentation_groups", [])]
        restore_entries = list(cleanup_config.get("restore_files") or [])
        reset_runtime_smoke = bool(cleanup_config.get("reset_runtime_smoke", True))
        stop_debug_mode = cleanup_config.get("stop_debug")
        debug_hygiene = bool(cleanup_config.get("debug_hygiene", False))
        attempted: list[str] = []
        failures: list[dict[str, Any]] = []
        restored_files: list[dict[str, Any]] = []
        debug_stop: dict[str, Any] | None = None

        for group_name in group_names:
            attempted.append(f"instrumentation_group_clear:{group_name}")
            result = await self._execute_operation(
                "instrumentation_group_clear",
                {"name": group_name},
            )
            if _terminal_status(result.get("status", "PASS")) != "PASS":
                failures.append(
                    {
                        "operation": "instrumentation_group_clear",
                        "name": group_name,
                        "reason": result.get("reason", "instrumentation cleanup failed"),
                        "result": result,
                    }
                )

        pre_reset_state = _remaining_runtime_smoke_state(self._session)
        if pre_reset_state["instrumentation_groups"]:
            failures.append(
                {
                    "operation": "runtime_smoke_residue",
                    "reason": "runtime smoke state still owns instrumentation groups",
                    "remaining_runtime_smoke_state": pre_reset_state,
                }
            )

        if stop_debug_mode:
            mode = "graceful" if stop_debug_mode is True else str(stop_debug_mode)
            attempted.append(f"stop_debug:{mode}")
            try:
                stop_result = await self._stop_debug(mode)
                debug_stop = {
                    "status": "PASS",
                    "mode": mode,
                    "result": stop_result,
                }
            except Exception as exc:
                debug_stop = {
                    "status": "FAIL",
                    "mode": mode,
                    "reason": str(exc),
                }
                failures.append(
                    {
                        "operation": "stop_debug",
                        "mode": mode,
                        "reason": str(exc),
                        "result": dict(debug_stop),
                    }
                )

        if allow_restore:
            for entry in restore_entries:
                raw_path = (
                    str(entry.get("path", "<missing>")) if isinstance(entry, dict) else "<invalid>"
                )
                attempted.append(f"restore_file:{raw_path}")
                try:
                    restored_files.append(await self._restore_file_with_retries(entry))
                except Exception as exc:
                    failure = {
                        "operation": "fixture.restore",
                        "path": _safe_validated_path(self._session, raw_path),
                        "reason": str(exc),
                    }
                    failures.append(failure)

        if debug_hygiene:
            attempted.append("debug_hygiene_preflight")
            result = await self._execute_operation("debug_hygiene_preflight", {})
            if _terminal_status(result.get("status", "PASS")) != "PASS":
                failures.append(
                    {
                        "operation": "debug_hygiene_preflight",
                        "reason": result.get("reason", "debug hygiene cleanup failed"),
                        "result": result,
                    }
                )

        reset_failures: tuple[CleanupFailure, ...] = ()
        runtime_smoke = getattr(self._session, "runtime_smoke", None)
        if reset_runtime_smoke and runtime_smoke is not None:
            attempted.append("runtime_smoke_reset")
            reset_failures = runtime_smoke.reset()
            for failure in reset_failures:
                failures.append(
                    {
                        "operation": "runtime_smoke_reset",
                        "reason": failure.get("error", "runtime smoke reset failed"),
                        "result": dict(failure),
                    }
                )
        remaining = _remaining_runtime_smoke_state(self._session)

        return {
            "status": "FAIL" if failures else "PASS",
            "attempted": attempted,
            "failures": failures,
            "restored_files": restored_files,
            "debug_stop": debug_stop,
            "reset_failures": [dict(failure) for failure in reset_failures],
            "remaining_runtime_smoke_state": remaining,
        }

    async def _restore_file_with_retries(self, entry: Any) -> dict[str, Any]:
        attempts = 0
        last_error: Exception | None = None
        for delay in (*RESTORE_RETRY_DELAYS_SECONDS, None):
            attempts += 1
            try:
                result = self._restore_file(entry)
                if attempts > 1:
                    result["attempts"] = attempts
                return result
            except PermissionError as exc:
                last_error = exc
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                last_error = exc

            if delay is not None:
                await asyncio.sleep(delay)

        if last_error is not None:
            matched = self._restore_file_if_already_matches(entry)
            if matched is not None:
                matched["attempts"] = attempts
                matched["already_matched_after_error"] = str(last_error)
                return matched
            raise last_error
        raise RuntimeError("restore failed without an exception")

    def _validate_restore_paths(self, plan: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for prefix, entry in _iter_restore_entries(plan):
            if not isinstance(entry, dict):
                continue
            try:
                self._validate_restore_entry_paths(entry)
            except Exception as exc:
                errors.append(f"{prefix}.path validation failed: {exc}")
        return errors

    def _validate_restore_entry_paths(self, entry: dict[str, Any]) -> None:
        self._validate_path(str(entry["path"]), must_exist=False)
        baseline_file = entry.get("baseline_file")
        if baseline_file is not None:
            self._validate_path(str(baseline_file), must_exist=True)

    def _validate_path(self, path: str, *, must_exist: bool) -> str:
        validate_path = getattr(self._session, "validate_path", None)
        if validate_path is None:
            raise RuntimeError("path validation service unavailable")
        return str(validate_path(path, must_exist=must_exist))

    def _restore_file(self, entry: dict[str, Any]) -> dict[str, Any]:
        target_path, source, baseline_file, content = self._restore_file_inputs(entry)

        target = Path(target_path)
        parent = target.parent
        if not parent.is_dir():
            raise ValueError(f"Restore parent directory does not exist: {parent}")
        original_attributes = _clear_windows_hidden_attribute(target)
        try:
            target.write_text(content, encoding="utf-8")
        finally:
            if original_attributes is not None:
                _set_windows_file_attributes(target, original_attributes)

        result = self._restore_file_result(
            target_path,
            source,
            baseline_file,
            content,
        )
        return result

    def _restore_file_if_already_matches(
        self,
        entry: dict[str, Any],
    ) -> dict[str, Any] | None:
        target_path, source, baseline_file, content = self._restore_file_inputs(entry)
        target = Path(target_path)
        if not target.is_file() or target.read_text(encoding="utf-8") != content:
            return None
        result = self._restore_file_result(
            target_path,
            source,
            baseline_file,
            content,
        )
        result["already_matched"] = True
        return result

    def _restore_file_inputs(
        self,
        entry: dict[str, Any],
    ) -> tuple[str, str, str | None, str]:
        target_path = self._validate_path(str(entry["path"]), must_exist=False)
        source = "baseline_text"
        baseline_file = None
        content = entry.get("baseline_text")
        if content is None:
            source = "baseline_file"
            baseline_file = self._validate_path(str(entry["baseline_file"]), must_exist=True)
            content = Path(baseline_file).read_text(encoding="utf-8")
        if not isinstance(content, str):
            raise ValueError("restore baseline content must be text")
        return target_path, source, baseline_file, content

    @staticmethod
    def _restore_file_result(
        target_path: str,
        source: str,
        baseline_file: str | None,
        content: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "PASS",
            "path": target_path,
            "source": source,
            "char_count": len(content),
            "byte_count": len(content.encode("utf-8")),
        }
        if baseline_file is not None:
            result["baseline_file"] = baseline_file
        return result

    async def _stop_debug(self, mode: str) -> dict[str, Any]:
        if mode != "graceful":
            raise ValueError(f"Unsupported stop_debug mode: {mode}")

        client = getattr(self._session, "client", None)
        capabilities = getattr(client, "capabilities", {}) if client is not None else {}
        if client is not None and capabilities.get("supportsTerminateRequest", False):
            await client.terminate()
            wait_for_stopped = getattr(self._session, "wait_for_stopped", None)
            if wait_for_stopped is not None:
                snapshot = await wait_for_stopped(timeout=10.0)
                if getattr(snapshot, "timed_out", False):
                    raise RuntimeError("Terminate sent but program did not exit within 10s")

        stop = getattr(self._session, "stop", None)
        if stop is None:
            raise RuntimeError("debug stop service unavailable")
        result = stop()
        if inspect.isawaitable(result):
            result = await result
        return _result_dict(result)

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
        return finalize_result(
            status=status,
            reason=reason,
            elapsed_ms=int(max(0.0, self._clock() - started) * 1000),
            action_count=action_count,
            completed_steps=completed_steps,
            failed_assertions=failed_assertions,
            cleanup=cleanup,
            evidence_refs=_collect_evidence_refs(completed_steps),
            compact_builder=compact_runtime_smoke_result,
            extra=extra,
        )


@dataclass
class RuntimeSmokeRunRecord:
    """Bounded state for one durable runtime-smoke lifecycle run."""

    run_id: str
    plan_name: str
    created_at: float
    max_events: int
    status: str = "RUNNING"
    task: asyncio.Task | None = None
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    app_diagnostics_source_enabled: bool = False
    app_diagnostics_entries: list[dict[str, Any]] = field(default_factory=list)
    app_diagnostics_dropped_count: int = 0
    next_cursor: int = 1
    dropped_count: int = 0
    stop_requested: bool = False
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        self.updated_at = self.created_at

    @property
    def oldest_cursor(self) -> int:
        if self.events:
            return int(self.events[0]["cursor"])
        return self.next_cursor

    def append_event(self, kind: str, clock: Callable[[], float], **payload: Any) -> None:
        event = {
            "cursor": self.next_cursor,
            "kind": kind,
            "status": self.status,
            "run_id": self.run_id,
            "timestamp": int(clock() * 1000),
            **payload,
        }
        self.next_cursor += 1
        self.updated_at = clock()
        self.events.append(event)
        if len(self.events) > self.max_events:
            excess = len(self.events) - self.max_events
            del self.events[:excess]
            self.dropped_count += excess

    def tail(self, after_cursor: int, limit: int) -> dict[str, Any]:
        bounded_limit = max(0, min(limit, self.max_events))
        events = [
            dict(event)
            for event in self.events
            if int(event.get("cursor", 0)) > after_cursor
        ][:bounded_limit]
        stale_cursor = bool(self.events) and after_cursor < self.oldest_cursor - 1
        return {
            "status": self.status,
            "run_id": self.run_id,
            "events": events,
            "next_cursor": self.next_cursor - 1,
            "oldest_cursor": self.oldest_cursor,
            "dropped_count": self.dropped_count,
            "stale_cursor": stale_cursor,
            "final": self.result is not None,
        }

    def append_app_diagnostics_entries(self, entries: list[dict[str, Any]]) -> None:
        self.app_diagnostics_source_enabled = True
        if not entries:
            return
        self.app_diagnostics_entries.extend(compact_value(entry) for entry in entries)
        if len(self.app_diagnostics_entries) > self.max_events:
            excess = len(self.app_diagnostics_entries) - self.max_events
            del self.app_diagnostics_entries[:excess]
            self.app_diagnostics_dropped_count += excess

    def app_diagnostics_cursor(
        self,
        *,
        from_start: bool,
        allow_empty: bool,
    ) -> dict[str, int] | None:
        if not self.app_diagnostics_source_enabled:
            return None
        total_entries = self.app_diagnostics_dropped_count + len(
            self.app_diagnostics_entries
        )
        if total_entries == 0 and not allow_empty:
            return None
        return {
            "after_index": 0 if from_start else total_entries,
            "entry_count": total_entries,
        }

    def app_diagnostics_delta(
        self,
        *,
        after_index: int,
        entry_count: int,
        limit: int,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        total_entries = self.app_diagnostics_dropped_count + len(
            self.app_diagnostics_entries
        )
        bounded_limit = max(0, int(limit))
        bounded_after_index = max(0, int(after_index))
        start_index = min(
            max(bounded_after_index, self.app_diagnostics_dropped_count),
            total_entries,
        )
        start_offset = start_index - self.app_diagnostics_dropped_count
        stale_cursor = (
            int(entry_count) > total_entries
            or bounded_after_index > total_entries
            or bounded_after_index < self.app_diagnostics_dropped_count
        )
        available_entries = self.app_diagnostics_entries[start_offset:]
        available = len(available_entries)
        bounded_entries = available_entries[:bounded_limit]
        next_after_index = start_index + len(bounded_entries)
        return (
            {
                "entries": compact_value(bounded_entries),
                "available": available,
                "limit": bounded_limit,
                "limited": available > bounded_limit,
                "stale_cursor": stale_cursor,
                "dropped_count": self.app_diagnostics_dropped_count,
            },
            {
                "after_index": next_after_index,
                "entry_count": total_entries,
            },
        )


def _collect_case_app_diagnostics_entries(
    case_result: dict[str, Any],
) -> list[dict[str, Any]]:
    case_id = case_result.get("id")
    transitions = case_result.get("transitions")
    if not isinstance(transitions, list):
        return []

    entries: list[dict[str, Any]] = []
    for transition_index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        probes = transition.get("probes")
        if not isinstance(probes, dict):
            continue
        for phase in ("before", "after"):
            phase_probes = probes.get(phase, [])
            if not isinstance(phase_probes, list):
                continue
            for probe in phase_probes:
                if (
                    not isinstance(probe, dict)
                    or probe.get("kind") != "app_diagnostics"
                ):
                    continue
                entry: dict[str, Any] = {
                    "case_id": case_id,
                    "transition_index": transition_index,
                    "phase": phase,
                    "probe": str(probe.get("name") or probe.get("kind") or ""),
                    "status": probe.get("status"),
                }
                if "reason" in probe:
                    entry["reason"] = probe.get("reason")
                if "value" in probe:
                    entry["value"] = compact_value(probe.get("value"))
                if "evidence_ref" in probe:
                    entry["evidence_ref"] = probe.get("evidence_ref")
                entries.append(compact_value(entry))
    return entries


class RuntimeSmokeRunRegistry:
    """Per-session durable runtime-smoke lifecycle run registry."""

    def __init__(
        self,
        *,
        max_runs: int = 8,
        max_events_per_run: int = 128,
        stop_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_runs = max(1, max_runs)
        self._max_events_per_run = max(1, max_events_per_run)
        self._stop_timeout_seconds = max(0.1, stop_timeout_seconds)
        self._clock = clock
        self._runs: dict[str, RuntimeSmokeRunRecord] = {}
        self._lock = asyncio.Lock()
        self._contamination: dict[str, Any] | None = None

    async def start(
        self,
        plan: Any,
        runner_factory: Callable[[], RuntimeSmokeRunner],
    ) -> dict[str, Any]:
        async with self._lock:
            contamination = self._contamination
            if contamination is not None:
                return _contamination_blocked_payload(contamination)

            active = self._active_run_locked()
            if active is not None:
                return self._active_blocked_payload(active)

            run_id = uuid.uuid4().hex
            record = RuntimeSmokeRunRecord(
                run_id=run_id,
                plan_name=_plan_name(plan),
                created_at=self._clock(),
                max_events=self._max_events_per_run,
                app_diagnostics_source_enabled=(
                    isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2
                ),
            )
            record.append_event(
                "started",
                self._clock,
                reason="runtime smoke run started",
                plan_name=record.plan_name,
            )
            record.task = asyncio.create_task(
                self._execute(record, plan, runner_factory),
                name=f"runtime-smoke:{run_id}",
            )
            self._runs[run_id] = record
            self._prune_locked()
            return self._running_payload(record)

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return _run_not_found(run_id)
            payload = record.tail(max(0, int(after_cursor)), max(0, int(limit)))
            if self._contamination is not None:
                payload.update(_contamination_metadata(self._contamination))
            return payload

    async def get_result(self, run_id: str) -> dict[str, Any]:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return _run_not_found(run_id)
            if record.result is None:
                return self._running_payload(record)
            return self._final_payload(record)

    async def record_case_progress(
        self,
        run_id: str,
        case_result: dict[str, Any],
    ) -> None:
        entries = _collect_case_app_diagnostics_entries(case_result)
        if not entries:
            return
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            record.append_app_diagnostics_entries(entries)

    async def get_app_diagnostics_source_cursor(
        self,
        run_id: str,
    ) -> dict[str, int] | None:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.result is None:
                return record.app_diagnostics_cursor(from_start=False, allow_empty=True)
            return record.app_diagnostics_cursor(from_start=True, allow_empty=False)

    async def get_app_diagnostics_source_delta(
        self,
        run_id: str,
        *,
        after_index: int,
        entry_count: int,
        limit: int,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.result is not None and not record.app_diagnostics_entries:
                return None
            return record.app_diagnostics_delta(
                after_index=after_index,
                entry_count=entry_count,
                limit=limit,
            )

    async def stop(
        self,
        run_id: str,
        *,
        reason: str = "runtime smoke stop requested",
    ) -> dict[str, Any]:
        task: asyncio.Task | None = None
        should_cancel = False
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return _run_not_found(run_id)
            if record.result is not None:
                return self._final_payload(record)
            if not record.stop_requested:
                record.stop_requested = True
                record.status = "STOPPING"
                record.append_event("stop_requested", self._clock, reason=reason)
                should_cancel = True
            task = record.task

        if task is not None:
            if not task.done():
                await asyncio.sleep(0)
            if should_cancel and not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._stop_timeout_seconds)
            except asyncio.TimeoutError:
                async with self._lock:
                    record = self._runs.get(run_id)
                    if record is None:
                        return _run_not_found(run_id)
                    record.status = "STOPPING"
                    contamination = self._mark_contaminated_locked(
                        reason="runtime smoke stop timed out during cleanup",
                        run_id=record.run_id,
                    )
                    record.append_event(
                        "stop_timeout",
                        self._clock,
                        reason="runtime smoke stop is still cleaning up",
                    )
                    payload = self._running_payload(record)
                    payload.update(_contamination_metadata(contamination))
                    return payload

        return await self.get_result(run_id)

    async def stop_all(
        self,
        *,
        reason: str = "runtime smoke session stopped",
    ) -> list[dict[str, Any]]:
        current_task = asyncio.current_task()
        async with self._lock:
            run_ids = [
                run_id
                for run_id, record in self._runs.items()
                if record.result is None and record.task is not current_task
            ]
        return [await self.stop(run_id, reason=reason) for run_id in run_ids]

    def active_run_ids(self) -> list[str]:
        return [
            run_id
            for run_id, record in self._runs.items()
            if record.result is None
        ]

    def retained_run_ids(self) -> list[str]:
        return list(self._runs)

    def contamination(self) -> dict[str, Any] | None:
        return dict(self._contamination) if self._contamination is not None else None

    def mark_contaminated(
        self,
        *,
        reason: str,
        run_id: str | None = None,
        cleanup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._contamination = _contamination_payload(
            reason=reason,
            run_id=run_id,
            cleanup=cleanup,
            observed_at_ms=int(self._clock() * 1000),
        )
        return dict(self._contamination)

    async def cleanup_contract(
        self,
        *,
        reset: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            active = self._active_run_locked()
            contamination = self.contamination()
            if active is not None:
                if contamination is not None:
                    return {
                        "status": "BLOCKED",
                        "reason": "runtime smoke run is still active",
                        **_contamination_metadata(contamination),
                        "cleanup_contract": {
                            **_cleanup_contract_required(contamination),
                            "status": "BLOCKED",
                        },
                    }
                return {
                    "status": "BLOCKED",
                    "reason": "runtime smoke run is still active",
                    "run_id": active.run_id,
                    "contaminated": False,
                    "cleanup_contract": {
                        "status": "BLOCKED",
                        "required_before": False,
                        "attempted": [],
                        "failures": [],
                    },
                }
            required_before = contamination is not None

        if not required_before:
            return {
                "status": "PASS",
                "reason": "runtime smoke cleanup contract already clean",
                "contaminated": False,
                "cleanup_contract": {
                    "status": "PASS",
                    "required_before": False,
                    "attempted": [],
                    "failures": [],
                },
            }

        attempted: list[str] = []
        failures: list[dict[str, Any]] = []
        if reset is not None:
            attempted.append("runtime_smoke_reset")
            try:
                reset_result = reset()
                if inspect.isawaitable(reset_result):
                    reset_result = await reset_result
                failures.extend(_reset_failures(reset_result))
            except Exception as exc:
                failures.append(
                    {
                        "operation": "runtime_smoke_reset",
                        "reason": str(exc),
                        "exception": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )

        if failures:
            async with self._lock:
                contamination = self._mark_contaminated_locked(
                    reason="runtime smoke cleanup contract failed",
                    cleanup={"status": "FAIL", "failures": failures},
                )
            return {
                "status": "FAIL",
                "reason": "runtime smoke cleanup contract failed",
                **_contamination_metadata(contamination),
                "cleanup_contract": {
                    "status": "FAIL",
                    "required_before": True,
                    "attempted": attempted,
                    "failures": failures,
                    "next_action": "runtime_smoke_cleanup_contract",
                },
            }

        async with self._lock:
            if self._contamination == contamination:
                self._contamination = None
            elif self._contamination is not None:
                latest = dict(self._contamination)
                return {
                    "status": "BLOCKED",
                    "reason": "runtime smoke cleanup contract changed during cleanup",
                    **_contamination_metadata(latest),
                    "cleanup_contract": {
                        **_cleanup_contract_required(latest),
                        "status": "REQUIRED",
                        "required_before": True,
                        "attempted": attempted,
                        "failures": [],
                    },
                }
        return {
            "status": "PASS",
            "reason": "runtime smoke cleanup contract satisfied",
            "contaminated": False,
            "cleanup_contract": {
                "status": "PASS",
                "required_before": True,
                "attempted": attempted,
                "failures": [],
            },
        }

    async def _execute(
        self,
        record: RuntimeSmokeRunRecord,
        plan: Any,
        runner_factory: Callable[[], RuntimeSmokeRunner],
    ) -> None:
        runner = runner_factory()
        attach_case_progress_notifier = getattr(
            runner,
            "attach_case_progress_notifier",
            None,
        )
        if callable(attach_case_progress_notifier):
            attach_case_progress_notifier(
                lambda case_result: self.record_case_progress(
                    record.run_id,
                    case_result,
                )
            )
        try:
            result = await runner.run(plan)
            status = str(result.get("status") or "FAIL")
            event_kind = "completed"
        except asyncio.CancelledError:
            try:
                result = await self._stopped_result(record, plan, runner)
            except Exception as exc:
                result = self._stop_cleanup_exception_result(record, plan, runner, exc)
            status = "STOPPED"
            event_kind = "stopped"
        except Exception as exc:
            result = await self._failure_result(record, plan, runner, exc)
            status = str(result.get("status") or "FAIL")
            event_kind = "failed"

        async with self._lock:
            if _result_requires_cleanup_contract(result):
                cleanup = result.get("cleanup")
                contamination = self._mark_contaminated_locked(
                    reason=str(result.get("reason") or "runtime smoke cleanup failed"),
                    run_id=record.run_id,
                    cleanup=cleanup if isinstance(cleanup, dict) else None,
                )
                result.update(_contamination_metadata(contamination))
            record.result = result
            record.status = status
            record.append_event(
                event_kind,
                self._clock,
                reason=result.get("reason"),
                result_compact=result.get("compact"),
            )
            self._prune_locked()

    async def _stopped_result(
        self,
        record: RuntimeSmokeRunRecord,
        plan: Any,
        runner: RuntimeSmokeRunner,
    ) -> dict[str, Any]:
        if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2:
            from .runtime_smoke_v2.actions import ActionContext
            from .runtime_smoke_v2.cleanup import (
                cleanup_steps_from_case,
                cleanup_steps_from_plan,
                merge_cleanup_results,
                run_cleanup,
            )
            from .runtime_smoke_v2.runner import RuntimeStateOracleRunner, _cases_for_execution

            context = ActionContext(
                service_adapters=runner._service_adapters,
                clock=runner._clock,
                session=runner._session,
            )
            v2_runner = RuntimeStateOracleRunner(
                runner._session,
                service_adapters=runner._service_adapters,
                clock=runner._clock,
            )
            v2_runner.capture_plan_metadata(plan)
            try:
                cases, generated_case_count, _generation_errors = _cases_for_execution(plan)
                case_cleanups = []
                for case in cases:
                    case_cleanup_steps = cleanup_steps_from_case(case)
                    if not case_cleanup_steps:
                        continue
                    case_cleanups.append(
                        await run_cleanup(
                            case_cleanup_steps,
                            context,
                            case_id=str(case.get("id") or ""),
                        )
                    )
                plan_cleanup = await run_cleanup(cleanup_steps_from_plan(plan), context)
                cleanup = merge_cleanup_results(plan_cleanup, case_cleanups)
            except Exception as exc:
                return v2_runner._finalize(
                    status="FAIL",
                    reason="runtime smoke stop cleanup failed",
                    started=record.created_at,
                    action_count=0,
                    cases=[],
                    generated_case_count=0,
                    metrics_thresholds=None,
                    baseline=None,
                    cleanup=_cleanup_exception_payload("runtime_smoke_stop_cleanup", exc),
                    extra={"stopped": True},
                )
            return v2_runner._finalize(
                status="IMPASSE",
                reason="runtime smoke run stopped",
                started=record.created_at,
                action_count=0,
                cases=[],
                generated_case_count=generated_case_count,
                metrics_thresholds=None,
                baseline=None,
                cleanup=cleanup,
                extra={"stopped": True},
            )

        try:
            cleanup = await runner._teardown(
                plan if isinstance(plan, dict) else {},
                allow_restore=isinstance(plan, dict),
                allow_plan_cleanup=isinstance(plan, dict),
            )
        except Exception as exc:
            return runner._finalize(
                status="FAIL",
                reason="runtime smoke stop cleanup failed",
                started=record.created_at,
                action_count=0,
                completed_steps=[],
                failed_assertions=[],
                cleanup=_cleanup_exception_payload("runtime_smoke_stop_cleanup", exc),
                extra={"stopped": True},
            )
        return runner._finalize(
            status="IMPASSE",
            reason="runtime smoke run stopped",
            started=record.created_at,
            action_count=0,
            completed_steps=[],
            failed_assertions=[],
            cleanup=cleanup,
            extra={"stopped": True},
        )

    def _stop_cleanup_exception_result(
        self,
        record: RuntimeSmokeRunRecord,
        plan: Any,
        runner: RuntimeSmokeRunner,
        exc: Exception,
    ) -> dict[str, Any]:
        if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2:
            from .runtime_smoke_v2.runner import RuntimeStateOracleRunner

            v2_runner = RuntimeStateOracleRunner(
                runner._session,
                service_adapters=runner._service_adapters,
                clock=runner._clock,
            )
            v2_runner.capture_plan_metadata(plan)
            return v2_runner._finalize(
                status="FAIL",
                reason="runtime smoke stop cleanup failed",
                started=record.created_at,
                action_count=0,
                cases=[],
                generated_case_count=0,
                metrics_thresholds=None,
                baseline=None,
                cleanup=_cleanup_exception_payload("runtime_smoke_stop_cleanup", exc),
                extra={"stopped": True},
            )
        return runner._finalize(
            status="FAIL",
            reason="runtime smoke stop cleanup failed",
            started=record.created_at,
            action_count=0,
            completed_steps=[],
            failed_assertions=[],
            cleanup=_cleanup_exception_payload("runtime_smoke_stop_cleanup", exc),
            extra={"stopped": True},
        )

    async def _failure_result(
        self,
        record: RuntimeSmokeRunRecord,
        plan: Any,
        runner: RuntimeSmokeRunner,
        exc: Exception,
    ) -> dict[str, Any]:
        if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2:
            return await self._v2_failure_result(record, plan, runner, exc)

        try:
            cleanup = await runner._teardown(
                plan if isinstance(plan, dict) else {},
                allow_restore=isinstance(plan, dict),
                allow_plan_cleanup=isinstance(plan, dict),
            )
        except Exception as cleanup_exc:
            cleanup = {
                "status": "FAIL",
                "attempted": ["runtime_smoke_failure_cleanup"],
                "failures": [
                    {
                        "operation": "runtime_smoke_failure_cleanup",
                        "reason": str(cleanup_exc),
                    }
                ],
            }
        return runner._finalize(
            status="FAIL",
            reason="runtime smoke runner raised exception",
            started=record.created_at,
            action_count=0,
            completed_steps=[],
            failed_assertions=[],
            cleanup=cleanup,
            extra={
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            },
        )

    async def _v2_failure_result(
        self,
        record: RuntimeSmokeRunRecord,
        plan: dict[str, Any],
        runner: RuntimeSmokeRunner,
        exc: Exception,
    ) -> dict[str, Any]:
        from .runtime_smoke_v2.actions import ActionContext
        from .runtime_smoke_v2.cleanup import (
            cleanup_steps_from_case,
            cleanup_steps_from_plan,
            merge_cleanup_results,
            run_cleanup,
        )
        from .runtime_smoke_v2.runner import RuntimeStateOracleRunner, _cases_for_execution

        v2_runner = RuntimeStateOracleRunner(
            runner._session,
            service_adapters=runner._service_adapters,
            clock=runner._clock,
        )
        v2_runner.capture_plan_metadata(plan)
        raw_metrics_thresholds = plan.get("metrics_thresholds")
        metrics_thresholds = (
            dict(raw_metrics_thresholds) if isinstance(raw_metrics_thresholds, dict) else None
        )
        generated_case_count = 0
        try:
            cases, generated_case_count, _generation_errors = _cases_for_execution(plan)
            context = ActionContext(
                service_adapters=runner._service_adapters,
                clock=runner._clock,
                session=runner._session,
                diagnostic_launch=getattr(v2_runner, "_diagnostic_launch", None),
            )
            case_cleanups = []
            for case in cases:
                case_cleanup_steps = cleanup_steps_from_case(case)
                if not case_cleanup_steps:
                    continue
                case_cleanups.append(
                    await run_cleanup(
                        case_cleanup_steps,
                        context,
                        case_id=str(case.get("id") or ""),
                    )
                )
            plan_cleanup = await run_cleanup(cleanup_steps_from_plan(plan), context)
            cleanup = merge_cleanup_results(plan_cleanup, case_cleanups)
        except Exception as cleanup_exc:
            cleanup = _cleanup_exception_payload("runtime_smoke_failure_cleanup", cleanup_exc)

        return v2_runner._finalize(
            status="FAIL",
            reason="runtime smoke runner raised exception",
            started=record.created_at,
            action_count=0,
            cases=[],
            generated_case_count=generated_case_count,
            metrics_thresholds=metrics_thresholds,
            baseline=None,
            cleanup=cleanup,
            extra={
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            },
        )

    def _active_run_locked(self) -> RuntimeSmokeRunRecord | None:
        for record in self._runs.values():
            if record.result is None:
                return record
        return None

    def _mark_contaminated_locked(
        self,
        *,
        reason: str,
        run_id: str | None = None,
        cleanup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._contamination = _contamination_payload(
            reason=reason,
            run_id=run_id,
            cleanup=cleanup,
            observed_at_ms=int(self._clock() * 1000),
        )
        return dict(self._contamination)

    def _prune_locked(self) -> None:
        for run_id, record in list(self._runs.items()):
            if len(self._runs) <= self._max_runs:
                break
            if record.result is None:
                continue
            del self._runs[run_id]

    def _running_payload(self, record: RuntimeSmokeRunRecord) -> dict[str, Any]:
        payload = {
            "status": record.status,
            "reason": "runtime smoke run is still running",
            "run_id": record.run_id,
            "plan_name": record.plan_name,
            "next_cursor": record.next_cursor - 1,
            "oldest_cursor": record.oldest_cursor,
            "dropped_count": record.dropped_count,
            "final": False,
        }
        if record.app_diagnostics_entries:
            payload["app_diagnostics_history"] = compact_value(
                record.app_diagnostics_entries
            )
        if self._contamination is not None:
            payload.update(_contamination_metadata(self._contamination))
        return payload

    def _active_blocked_payload(self, record: RuntimeSmokeRunRecord) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "reason": "runtime smoke run already active",
            "active_run_id": record.run_id,
            "active_status": record.status,
            "run_created": False,
            "next_cursor": record.next_cursor - 1,
            "oldest_cursor": record.oldest_cursor,
            "dropped_count": record.dropped_count,
            "final": False,
            "next_actions": [
                "runtime_smoke_evidence_bundle",
                "runtime_smoke_wait_for_result",
                "runtime_smoke_tail_events",
                "runtime_smoke_get_result",
                "runtime_smoke_stop",
            ],
        }

    def _final_payload(self, record: RuntimeSmokeRunRecord) -> dict[str, Any]:
        assert record.result is not None
        payload = dict(record.result)
        payload.update(
            {
                "run_id": record.run_id,
                "lifecycle_status": record.status,
                "next_cursor": record.next_cursor - 1,
                "oldest_cursor": record.oldest_cursor,
                "dropped_count": record.dropped_count,
                "final": True,
            }
        )
        if self._contamination is not None:
            payload.update(_contamination_metadata(self._contamination))
        return payload


def _plan_name(plan: Any) -> str:
    if isinstance(plan, dict):
        return str(plan.get("name") or plan.get("id") or "runtime-smoke")
    return "runtime-smoke"


def _run_not_found(run_id: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "reason": "runtime smoke run not found",
        "run_id": run_id,
    }


def _contamination_payload(
    *,
    reason: str,
    run_id: str | None,
    cleanup: dict[str, Any] | None,
    observed_at_ms: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": reason,
        "observed_at_ms": observed_at_ms,
    }
    if run_id:
        payload["run_id"] = run_id
    if cleanup is not None:
        payload["cleanup"] = dict(cleanup)
    return payload


def _cleanup_contract_required(contamination: dict[str, Any]) -> dict[str, Any]:
    return {
        "required": True,
        "status": "REQUIRED",
        "reason": contamination.get("reason"),
        "run_id": contamination.get("run_id"),
        "next_action": "runtime_smoke_cleanup_contract",
    }


def _contamination_metadata(contamination: dict[str, Any]) -> dict[str, Any]:
    return {
        "contaminated": True,
        "cleanup_contract": _cleanup_contract_required(contamination),
    }


def _contamination_blocked_payload(contamination: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "runtime smoke cleanup contract required",
        **_contamination_metadata(contamination),
    }


def _result_requires_cleanup_contract(result: dict[str, Any]) -> bool:
    cleanup = result.get("cleanup")
    return isinstance(cleanup, dict) and cleanup.get("status") == "FAIL"


def _reset_failures(reset_result: Any) -> list[dict[str, Any]]:
    if not isinstance(reset_result, (list, tuple)):
        return []
    failures: list[dict[str, Any]] = []
    for failure in reset_result:
        if isinstance(failure, dict):
            operation = failure.get("name") or failure.get("operation")
            failures.append(
                {
                    "operation": str(operation or "runtime_smoke_reset"),
                    "reason": str(failure.get("error") or failure.get("reason") or failure),
                }
            )
        elif failure:
            failures.append(
                {
                    "operation": "runtime_smoke_reset",
                    "reason": str(failure),
                }
            )
    return failures


def _cleanup_exception_payload(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "attempted": [operation],
        "failures": [
            {
                "operation": operation,
                "reason": str(exc),
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        ],
    }


def _clear_windows_hidden_attribute(target: Path) -> int | None:
    if os.name != "nt" or not target.exists():
        return None

    attributes = getattr(target.stat(), "st_file_attributes", None)
    if attributes is None or not attributes & stat.FILE_ATTRIBUTE_HIDDEN:
        return None

    _set_windows_file_attributes(target, attributes & ~stat.FILE_ATTRIBUTE_HIDDEN)
    return int(attributes)


def _set_windows_file_attributes(target: Path, attributes: int) -> None:
    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    kernel32.SetFileAttributesW.restype = wintypes.BOOL
    if not kernel32.SetFileAttributesW(str(target), attributes):
        error = ctypes.GetLastError()
        raise OSError(error, f"SetFileAttributesW failed for {target}")


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
        if _plan_has_ui_operations(plan):
            steps.append(("ui", {"name": "ui.ensure_connected", "args": {}}))

    freshness = plan.get("freshness")
    if isinstance(freshness, dict):
        steps.append(("freshness", _step(freshness, "verify_debug_freshness")))

    steps.extend(("step", _step(item)) for item in plan.get("steps", []))
    steps.extend(("action", _step(item)) for item in plan.get("actions", []))
    steps.extend(("assertion", _step(item)) for item in plan.get("assertions", []))
    steps.extend(("evidence", _step(item)) for item in plan.get("evidence", []))
    return steps


def _step(raw: Any, default_name: str | None = None) -> dict[str, Any]:
    return normalize_plan_step(raw, default_name)


def _plan_has_ui_operations(plan: dict[str, Any]) -> bool:
    for section_name in ("steps", "actions", "assertions", "evidence"):
        for item in plan.get(section_name, []) or []:
            name = _raw_step_name(item)
            if name in UI_OPERATION_NAMES or name.startswith(UI_OPERATION_PREFIXES):
                return True
    return False


def _raw_step_name(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    value = raw.get("name") or raw.get("op") or ""
    return str(value)


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
    if status == "WARN":
        return "PASS"
    return status if status in TERMINAL_STATUSES else "FAIL"


def _remaining_runtime_smoke_state(session: Any) -> dict[str, Any]:
    runtime_smoke = getattr(session, "runtime_smoke", None)
    if runtime_smoke is None:
        return {"instrumentation_groups": [], "output_checkpoints": []}
    return {
        "instrumentation_groups": sorted(runtime_smoke.instrumentation_groups),
        "output_checkpoints": sorted(runtime_smoke.output_checkpoints),
    }


def _merged_cleanup_config(plan: dict[str, Any]) -> dict[str, Any]:
    teardown = plan.get("teardown") if isinstance(plan, dict) else None
    cleanup = plan.get("cleanup") if isinstance(plan, dict) else None
    configs = [config for config in (teardown, cleanup) if isinstance(config, dict)]
    merged: dict[str, Any] = {
        "instrumentation_groups": [],
        "restore_files": [],
        "reset_runtime_smoke": True,
    }
    for config in configs:
        merged["instrumentation_groups"].extend(config.get("instrumentation_groups", []))
        merged["restore_files"].extend(config.get("restore_files", []))
        if "reset_runtime_smoke" in config:
            merged["reset_runtime_smoke"] = bool(config["reset_runtime_smoke"])
        if "stop_debug" in config:
            merged["stop_debug"] = config["stop_debug"]
        if "debug_hygiene" in config:
            merged["debug_hygiene"] = config["debug_hygiene"]
    return merged


def _iter_restore_entries(plan: dict[str, Any]) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for config_name in ("teardown", "cleanup"):
        config = plan.get(config_name)
        if not isinstance(config, dict):
            continue
        restore_files = config.get("restore_files")
        if not isinstance(restore_files, list):
            continue
        entries.extend(
            (f"{config_name}.restore_files[{index}]", entry)
            for index, entry in enumerate(restore_files)
        )
    for collection_name in ("steps", "actions", "assertions", "evidence"):
        raw_items = plan.get(collection_name, [])
        if not isinstance(raw_items, list):
            continue
        for index, raw in enumerate(raw_items):
            if isinstance(raw, dict) and raw.get("op") == "fixture.restore":
                entries.append((f"{collection_name}[{index}]", _public_operation_args(raw)))
    return entries


def _public_operation_args(raw: dict[str, Any]) -> dict[str, Any]:
    args = dict(raw.get("args") or {})
    for key, value in raw.items():
        if key not in {"id", "op", "args"}:
            args[key] = value
    return args


def _safe_validated_path(session: Any, path: str) -> str:
    validate_path = getattr(session, "validate_path", None)
    if validate_path is None:
        return path
    try:
        return str(validate_path(path, must_exist=False))
    except Exception:
        return path


def _collect_evidence_refs(completed_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for step in completed_steps:
        result = step.get("result", {})
        if isinstance(result, dict):
            refs.extend(dict(ref) for ref in result.get("evidence_refs", []))
    return refs

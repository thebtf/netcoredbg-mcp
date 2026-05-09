"""Session-owned runtime smoke state and bounded scenario execution."""

from __future__ import annotations

import asyncio
import inspect
import os
import stat
import time
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
from .runtime_smoke_v2.result_envelope import finalize_result
from .state import EvidenceRef

TERMINAL_STATUSES = {"PASS", "FAIL", "BLOCKED", "IMPASSE"}
MAX_COMPACT_TEXT_LENGTH = 240
MAX_COMPACT_LIST_ITEMS = 8
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
                # Continue teardown so one failed cleanup cannot hide later failures.
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

    async def run(self, plan: Any) -> dict[str, Any]:
        started = self._clock()
        completed_steps: list[dict[str, Any]] = []
        failed_assertions: list[dict[str, Any]] = []
        validation_errors = validate_plan(plan)
        if not validation_errors and isinstance(plan, dict):
            validation_errors.extend(self._validate_restore_paths(plan))
        if validation_errors:
            cleanup = await self._teardown(
                plan if isinstance(plan, dict) else {},
                allow_restore=False,
                allow_plan_cleanup=False,
            )
            return self._finalize(
                status="FAIL",
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
                clock=self._clock,
            ).run(plan)

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
                failures.append({
                    "operation": "instrumentation_group_clear",
                    "name": group_name,
                    "reason": result.get("reason", "instrumentation cleanup failed"),
                    "result": result,
                })

        pre_reset_state = _remaining_runtime_smoke_state(self._session)
        if pre_reset_state["instrumentation_groups"]:
            failures.append({
                "operation": "runtime_smoke_residue",
                "reason": "runtime smoke state still owns instrumentation groups",
                "remaining_runtime_smoke_state": pre_reset_state,
            })

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
                failures.append({
                    "operation": "stop_debug",
                    "mode": mode,
                    "reason": str(exc),
                    "result": dict(debug_stop),
                })

        if allow_restore:
            for entry in restore_entries:
                raw_path = (
                    str(entry.get("path", "<missing>"))
                    if isinstance(entry, dict)
                    else "<invalid>"
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
                failures.append({
                    "operation": "debug_hygiene_preflight",
                    "reason": result.get("reason", "debug hygiene cleanup failed"),
                    "result": result,
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
        if capabilities.get("supportsTerminateRequest", False):
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

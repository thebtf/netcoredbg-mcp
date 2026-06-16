"""Runtime smoke composite tools."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..response import build_error_response, build_response, extend_next_actions
from ..session import SessionManager
from ..session.freshness import DebugFreshnessVerifier
from ..session.hygiene import HygienePreflightResult, RuntimeHygieneService
from ..session.instrumentation import InstrumentationGroupService
from ..session.output_assertions import OutputAssertionService
from ..session.runtime_smoke import RuntimeSmokeRunner
from ..session.runtime_smoke_operations import ui_operation_adapters
from ..session.runtime_smoke_schema import (
    SCHEMA_VERSION_V2,
    diagnostic_schema_contract,
    schema_help_fields,
    validate_plan,
)
from ..session.runtime_smoke_v2.result_envelope import compact_value
from ..session.state import DebugState

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def register_runtime_smoke_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
    resolve_project_root: Callable[..., Awaitable[Any]],
) -> None:
    """Register runtime smoke composite tools on the MCP server."""
    from mcp.types import ToolAnnotations

    backend_holder: dict[str, Any] = {"instance": None}

    def _get_backend() -> Any:
        if backend_holder["instance"] is None:
            from ..ui.backend import create_backend

            backend_holder["instance"] = create_backend(
                process_registry=session.process_registry,
            )
        return backend_holder["instance"]

    async def _ensure_ui_connected() -> Any:
        from ..ui import NoActiveSessionError, NoProcessIdError

        if session.state.state == DebugState.IDLE:
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        backend = _get_backend()
        if backend.process_id != process_id:
            from ..ui.backend import connect_backend

            await connect_backend(
                backend,
                process_id,
                stealth_mode=getattr(session, "stealth_mode", False),
            )
        return backend

    def _runner() -> RuntimeSmokeRunner:
        return RuntimeSmokeRunner(
            session,
            service_adapters=ui_operation_adapters(
                _ensure_ui_connected,
                session=session,
            ),
        )

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def debug_hygiene_preflight(
        ctx: Context,
        file: str | None = None,
        clear_breakpoints: bool = True,
        clear_trace_log: bool = True,
        clear_exception_filters: bool = False,
    ) -> dict:
        """Clear stale debugger state and report a compact hygiene result."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            validated_file = None
            if file:
                await resolve_project_root(ctx, session)
                try:
                    validated_file = session.validate_path(file)
                except Exception as exc:
                    return _build_hygiene_response(
                        session,
                        HygienePreflightResult.validation_failed(str(exc)),
                    )

            service = getattr(session, "hygiene", None) or RuntimeHygieneService(session)
            result = await service.preflight(
                file=validated_file,
                clear_breakpoints=clear_breakpoints,
                clear_trace_log=clear_trace_log,
                clear_exception_filters=clear_exception_filters,
            )
            return _build_hygiene_response(session, result)
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def instrumentation_group_create(
        ctx: Context,
        name: str,
        breakpoints: list[dict[str, Any]] | None = None,
        tracepoints: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Create a named breakpoint/tracepoint group for smoke evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            if not _valid_name(name):
                return _build_runtime_smoke_response(
                    session,
                    {
                        "status": "FAIL",
                        "reason": "invalid instrumentation group name",
                        "group": name,
                    },
                    ["instrumentation_group_create"],
                )

            validated_breakpoints = await _validate_instrumentation_items(
                ctx,
                session,
                resolve_project_root,
                breakpoints or [],
            )
            validated_tracepoints = await _validate_instrumentation_items(
                ctx,
                session,
                resolve_project_root,
                tracepoints or [],
            )
            service = _instrumentation_service(session)
            result = await service.create_group(
                name,
                breakpoints=validated_breakpoints,
                tracepoints=validated_tracepoints,
            )
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                [
                    "instrumentation_group_inspect",
                    "instrumentation_group_clear",
                ],
            )
        except ValueError as exc:
            return _build_runtime_smoke_response(
                session,
                {"status": "FAIL", "reason": "invalid instrumentation item", "error": str(exc)},
                ["instrumentation_group_create"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def instrumentation_group_inspect(ctx: Context, name: str) -> dict:
        """Inspect grouped breakpoint hits and trace logs."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = await _instrumentation_service(session).inspect_group(name)
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                ["instrumentation_group_clear", "output_assert_since"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def instrumentation_group_clear(ctx: Context, name: str) -> dict:
        """Remove a named instrumentation group with leak detection."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = await _instrumentation_service(session).clear_group(name)
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                ["instrumentation_group_create", "debug_hygiene_preflight"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def output_checkpoint(ctx: Context, name: str) -> dict:
        """Mark the current output buffer position for later assertions."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            if not _valid_name(name):
                return _build_runtime_smoke_response(
                    session,
                    {
                        "status": "FAIL",
                        "reason": "invalid output checkpoint name",
                        "checkpoint": name,
                    },
                    ["output_checkpoint"],
                )
            result = _output_assertion_service(session).create_checkpoint(name)
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                ["output_assert_since"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def output_assert_since(
        ctx: Context,
        checkpoint: str,
        required: list[str] | None = None,
        forbidden: list[str] | None = None,
        regex: bool = True,
        max_matches: int = 20,
    ) -> dict:
        """Assert required and forbidden output patterns since a checkpoint."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = _output_assertion_service(session).assert_since(
                checkpoint,
                required=required,
                forbidden=forbidden,
                regex=regex,
                max_matches=max_matches,
            )
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                ["output_checkpoint", "instrumentation_group_inspect"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def verify_debug_freshness(
        ctx: Context,
        expected_process_id: int | None = None,
        expected_process_name: str | None = None,
        expected_workspace: str | None = None,
        expected_sources: list[str] | None = None,
        expected_modules: list[str] | None = None,
        expected_artifacts: list[str] | None = None,
        require_active_process: bool = False,
    ) -> dict:
        """Verify that the debug session matches expected runtime evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            result = DebugFreshnessVerifier(session).verify(
                expected_process_id=expected_process_id,
                expected_process_name=expected_process_name,
                expected_workspace=expected_workspace,
                expected_sources=expected_sources,
                expected_modules=expected_modules,
                expected_artifacts=expected_artifacts,
                require_active_process=require_active_process,
            )
            return _build_runtime_smoke_response(
                session,
                result.to_dict(),
                ["run_runtime_smoke", "output_checkpoint"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def runtime_smoke_start(ctx: Context, plan: dict[str, Any]) -> dict:
        """Start a durable runtime smoke run and return a run id."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await session.runtime_smoke.lifecycle_runs.start(plan, _runner)
            return _build_runtime_smoke_response(
                session,
                data,
                [
                    "runtime_smoke_tail_events",
                    "runtime_smoke_get_result",
                    "runtime_smoke_stop",
                ],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_validate_plan(ctx: Context, plan: dict[str, Any]) -> dict:
        """Validate a runtime-smoke plan without launching or touching a target app."""
        try:
            data = validate_runtime_smoke_plan_contract(plan)
            return _build_runtime_smoke_response(
                session,
                data,
                ["runtime_smoke_run_plan", "runtime_smoke_start", "run_runtime_smoke"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def runtime_smoke_run_plan(
        ctx: Context,
        plan: dict[str, Any],
        agent_mode: bool = False,
    ) -> dict:
        """Validate then start a durable runtime smoke run."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            validation = validate_runtime_smoke_plan_contract(plan)
            if not validation["can_run"]:
                if agent_mode:
                    _apply_runtime_smoke_agent_mode(validation, "runtime_smoke_run_plan")
                return _build_runtime_smoke_response(
                    session,
                    validation,
                    ["runtime_smoke_validate_plan", "runtime_smoke_run_plan"],
                )

            data = await session.runtime_smoke.lifecycle_runs.start(plan, _runner)
            data["validation"] = _runtime_smoke_validation_summary(validation)
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, "runtime_smoke_evidence_bundle")
            return _build_runtime_smoke_response(
                session,
                data,
                [
                    "runtime_smoke_evidence_bundle",
                    "runtime_smoke_tail_events",
                    "runtime_smoke_get_result",
                    "runtime_smoke_stop",
                ],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def runtime_smoke_run_probe(
        ctx: Context,
        probe: dict[str, Any],
        name: str | None = None,
        phase: str = "after",
        budgets: dict[str, Any] | None = None,
        agent_mode: bool = False,
    ) -> dict:
        """Validate then start a durable runtime-smoke v2 run for one probe."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            plan, generated = _runtime_smoke_probe_plan(
                probe,
                name=name,
                phase=phase,
                budgets=budgets,
            )
            validation = validate_runtime_smoke_plan_contract(plan)
            if not validation["can_run"]:
                data = {
                    **validation,
                    "run_created": False,
                    "probe": _runtime_smoke_probe_summary(probe),
                    "generated_plan": generated,
                }
                if agent_mode:
                    _apply_runtime_smoke_agent_mode(data, "runtime_smoke_run_plan")
                return _build_runtime_smoke_response(
                    session,
                    data,
                    ["runtime_smoke_validate_plan", "runtime_smoke_run_probe"],
                )

            data = await session.runtime_smoke.lifecycle_runs.start(plan, _runner)
            data["validation"] = _runtime_smoke_validation_summary(validation)
            data["probe"] = _runtime_smoke_probe_summary(probe)
            data["generated_plan"] = generated
            data["run_created"] = bool(data.get("run_id"))
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, "runtime_smoke_evidence_bundle")
            return _build_runtime_smoke_response(
                session,
                data,
                [
                    "runtime_smoke_evidence_bundle",
                    "runtime_smoke_tail_events",
                    "runtime_smoke_get_result",
                    "runtime_smoke_stop",
                ],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_evidence_bundle(
        ctx: Context,
        run_id: str,
        after_cursor: int = 0,
        event_limit: int = 50,
        agent_mode: bool = False,
    ) -> dict:
        """Return a compact evidence packet for a durable runtime smoke run."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runtime_smoke_evidence_bundle(
                session.runtime_smoke.lifecycle_runs,
                run_id,
                after_cursor=after_cursor,
                event_limit=event_limit,
            )
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, "runtime_smoke_get_event_delta")
            return _build_runtime_smoke_response(
                session,
                data,
                list(data.get("next_actions", ["runtime_smoke_run_plan"])),
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_mark_event_cursor(
        ctx: Context,
        run_id: str,
        agent_mode: bool = False,
    ) -> dict:
        """Return a compact cursor token for the current durable run event position."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runtime_smoke_mark_event_cursor(
                session.runtime_smoke.lifecycle_runs,
                run_id,
            )
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, "runtime_smoke_get_event_delta")
            return _build_runtime_smoke_response(
                session,
                data,
                list(data.get("next_actions", ["runtime_smoke_run_plan"])),
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_get_event_delta(
        ctx: Context,
        cursor: dict[str, Any],
        event_limit: int = 50,
        agent_mode: bool = False,
    ) -> dict:
        """Return bounded lifecycle events after a cursor token."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runtime_smoke_get_event_delta(
                session.runtime_smoke.lifecycle_runs,
                cursor,
                event_limit=event_limit,
            )
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, "runtime_smoke_get_event_delta")
            return _build_runtime_smoke_response(
                session,
                data,
                list(data.get("next_actions", ["runtime_smoke_run_plan"])),
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_tail_events(
        ctx: Context,
        run_id: str,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict:
        """Tail bounded lifecycle events for a durable runtime smoke run."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await session.runtime_smoke.lifecycle_runs.tail_events(
                run_id,
                after_cursor=after_cursor,
                limit=limit,
            )
            return _build_runtime_smoke_response(
                session,
                data,
                [
                    "runtime_smoke_tail_events",
                    "runtime_smoke_get_result",
                    "runtime_smoke_stop",
                ],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_get_result(ctx: Context, run_id: str) -> dict:
        """Return the final runtime smoke envelope when a durable run completes."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await session.runtime_smoke.lifecycle_runs.get_result(run_id)
            return _build_runtime_smoke_response(
                session,
                data,
                [
                    "runtime_smoke_tail_events",
                    "runtime_smoke_stop",
                    "runtime_smoke_start",
                ],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(
        annotations=ToolAnnotations(
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def runtime_smoke_stop(ctx: Context, run_id: str) -> dict:
        """Idempotently stop a durable runtime smoke run and return cleanup evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await session.runtime_smoke.lifecycle_runs.stop(run_id)
            return _build_runtime_smoke_response(
                session,
                data,
                ["runtime_smoke_get_result", "runtime_smoke_tail_events"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def run_runtime_smoke(ctx: Context, plan: dict[str, Any]) -> dict:
        """Run a bounded runtime smoke scenario plan with cleanup evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runner().run(plan)
            return _build_runtime_smoke_response(
                session,
                data,
                ["verify_debug_freshness", "debug_hygiene_preflight"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)


def validate_runtime_smoke_plan_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Return validate-only runtime-smoke plan readiness and evidence metadata."""
    validation_errors = validate_plan(plan)
    result: dict[str, Any] = {
        "validation_errors": list(validation_errors),
        **schema_help_fields(plan if isinstance(plan, dict) else None),
    }

    if isinstance(plan, dict) and plan.get("schema") == SCHEMA_VERSION_V2:
        from ..session.runtime_smoke_v2.runner import validate_v2_plan_contract

        v2_contract = validate_v2_plan_contract(plan)
        result.update(
            {
                key: value
                for key, value in v2_contract.items()
                if key != "validation_errors"
            }
        )
        result["validation_errors"].extend(v2_contract["validation_errors"])
    else:
        result.setdefault("case_count", 0)
        result.setdefault("generated_case_count", 0)

    result["can_run"] = not result["validation_errors"]
    result["status"] = "PASS" if result["can_run"] else "INVALID_SETUP"
    result["evidence_contract"] = _runtime_smoke_evidence_contract()
    return result


def _runtime_smoke_validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "can_run",
        "validation_errors",
        "case_count",
        "generated_case_count",
        "evidence_contract",
    )
    return {key: validation[key] for key in keys if key in validation}


def _runtime_smoke_probe_plan(
    probe: dict[str, Any],
    *,
    name: str | None = None,
    phase: str = "after",
    budgets: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wrap a single probe in the smallest executable v2 runtime-smoke plan."""
    probe_payload = dict(probe) if isinstance(probe, dict) else {"kind": ""}
    if "phase" not in probe_payload and "phases" not in probe_payload:
        probe_payload["phase"] = str(phase or "after")

    kind = str(probe_payload.get("kind") or "")
    probe_name = str(probe_payload.get("name") or kind or "probe")
    plan_name = str(name or f"run-probe-{probe_name}")
    plan = {
        "schema": SCHEMA_VERSION_V2,
        "name": plan_name,
        "cases": [
            {
                "id": "run_probe",
                "transitions": [
                    {
                        "id": "probe",
                        "action": {"kind": "ui.noop"},
                        "settle": {"idle_ms": 0},
                        "probes": [probe_payload],
                    }
                ],
            }
        ],
        "budgets": dict(budgets or {"max_actions": 1, "max_elapsed_seconds": 5}),
    }
    generated = {
        "schema": SCHEMA_VERSION_V2,
        "plan_name": plan_name,
        "case_count": 1,
        "transition_count": 1,
        "action_kind": "ui.noop",
        "probe_kind": kind,
        "probe_name": probe_name,
        "probe_phase": _runtime_smoke_probe_phase(probe_payload),
    }
    return plan, generated


def _runtime_smoke_probe_phase(probe: dict[str, Any]) -> str:
    if "phases" in probe:
        raw = probe["phases"]
        if isinstance(raw, str):
            return raw
        if isinstance(raw, (list, tuple, set)):
            return ",".join(str(item) for item in raw)
        return str(raw)
    return str(probe.get("phase") or "both")


def _runtime_smoke_probe_summary(probe: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(probe, dict):
        return {"kind": "", "name": ""}
    return {
        "kind": str(probe.get("kind") or ""),
        "name": str(probe.get("name") or probe.get("kind") or ""),
    }


async def _runtime_smoke_evidence_bundle(
    registry: Any,
    run_id: str,
    *,
    after_cursor: int = 0,
    event_limit: int = 50,
) -> dict[str, Any]:
    result = await registry.get_result(run_id)
    if _runtime_smoke_run_missing(result):
        return {
            "status": "FAIL",
            "reason": result.get("reason", "runtime smoke run not found"),
            "run_id": run_id,
            "final": True,
            "events": [],
            "event_cursor": {
                "after_cursor": max(0, int(after_cursor)),
                "next_cursor": max(0, int(after_cursor)),
                "oldest_cursor": None,
                "dropped_count": 0,
                "stale_cursor": False,
                "limit": max(0, int(event_limit)),
            },
            "result": None,
            "evidence_refs": [],
            "cleanup": None,
            "next_actions": ["runtime_smoke_run_plan"],
        }

    tail = await registry.tail_events(
        run_id,
        after_cursor=max(0, int(after_cursor)),
        limit=max(0, int(event_limit)),
    )
    final = bool(result.get("final"))
    next_actions = [
        "runtime_smoke_evidence_bundle",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
    ]
    next_actions.append("runtime_smoke_run_plan" if final else "runtime_smoke_stop")

    return {
        "status": result.get("status", tail.get("status", "UNKNOWN")),
        "reason": result.get("reason"),
        "run_id": run_id,
        "plan_name": result.get("plan_name"),
        "lifecycle_status": result.get("lifecycle_status", tail.get("status")),
        "final": final,
        "events": compact_value(tail.get("events", [])),
        "event_cursor": {
            "after_cursor": max(0, int(after_cursor)),
            "next_cursor": tail.get("next_cursor"),
            "oldest_cursor": tail.get("oldest_cursor"),
            "dropped_count": tail.get("dropped_count", 0),
            "stale_cursor": tail.get("stale_cursor", False),
            "limit": max(0, int(event_limit)),
        },
        "result": _bounded_runtime_smoke_result(result) if final else None,
        "evidence_refs": compact_value(result.get("evidence_refs", [])),
        "cleanup": compact_value(result.get("cleanup")),
        "next_actions": next_actions,
    }


async def _runtime_smoke_mark_event_cursor(registry: Any, run_id: str) -> dict[str, Any]:
    tail = await registry.tail_events(run_id, after_cursor=0, limit=0)
    if _runtime_smoke_tail_missing(tail):
        return _runtime_smoke_missing_event_delta(run_id, 0, tail, limit=0)

    next_cursor = _runtime_smoke_tail_next_cursor(tail, 0)
    cursor = _runtime_smoke_cursor_token(
        run_id,
        after_cursor=next_cursor,
        tail=tail,
    )
    return {
        "status": "PASS",
        "reason": "runtime smoke event cursor marked",
        "run_id": run_id,
        "cursor": cursor,
        "final": bool(tail.get("final")),
        "next_actions": [
            "runtime_smoke_get_event_delta",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_tail_events",
        ],
    }


async def _runtime_smoke_get_event_delta(
    registry: Any,
    cursor: dict[str, Any],
    *,
    event_limit: int = 50,
) -> dict[str, Any]:
    if not isinstance(cursor, dict):
        return _runtime_smoke_invalid_event_delta(
            "cursor token must be an object",
            after_cursor=0,
            limit=event_limit,
        )

    parsed_after_cursor = _runtime_smoke_parse_nonnegative_int(
        cursor.get("after_cursor", cursor.get("next_cursor", 0))
    )
    if parsed_after_cursor is None:
        return _runtime_smoke_invalid_event_delta(
            "cursor token after_cursor must be an integer",
            after_cursor=0,
            limit=event_limit,
            run_id=str(cursor.get("run_id") or ""),
        )
    parsed_limit = _runtime_smoke_parse_nonnegative_int(event_limit)
    if parsed_limit is None:
        return _runtime_smoke_invalid_event_delta(
            "event_limit must be an integer",
            after_cursor=parsed_after_cursor,
            limit=0,
            run_id=str(cursor.get("run_id") or ""),
        )

    run_id = str(cursor.get("run_id") or "")
    after_cursor = parsed_after_cursor
    limit = parsed_limit
    if not run_id:
        return _runtime_smoke_invalid_event_delta(
            "cursor token requires run_id",
            after_cursor=after_cursor,
            limit=limit,
            run_id=run_id,
        )

    tail = await registry.tail_events(
        run_id,
        after_cursor=after_cursor,
        limit=limit,
    )
    if _runtime_smoke_tail_missing(tail):
        return _runtime_smoke_missing_event_delta(run_id, after_cursor, tail, limit=limit)

    continuation_cursor = _runtime_smoke_tail_continuation_cursor(tail, after_cursor)
    return {
        "status": "PASS",
        "reason": "runtime smoke event delta read",
        "run_id": run_id,
        "events": _runtime_smoke_compact_event_delta(tail.get("events", [])),
        "event_cursor": _runtime_smoke_event_cursor(
            after_cursor=after_cursor,
            tail=tail,
            limit=limit,
        ),
        "cursor": _runtime_smoke_cursor_token(
            run_id,
            after_cursor=continuation_cursor,
            tail=tail,
        ),
        "final": bool(tail.get("final")),
        "next_actions": [
            "runtime_smoke_get_event_delta",
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_tail_events",
        ],
    }


def _runtime_smoke_invalid_event_delta(
    reason: str,
    *,
    after_cursor: int,
    limit: Any,
    run_id: str = "",
) -> dict[str, Any]:
    parsed_limit = _runtime_smoke_parse_nonnegative_int(limit)
    return {
        "status": "INVALID_SETUP",
        "reason": reason,
        "run_id": run_id,
        "events": [],
        "event_cursor": _runtime_smoke_event_cursor(
            after_cursor=after_cursor,
            tail={},
            limit=parsed_limit if parsed_limit is not None else 0,
        ),
        "final": True,
        "next_actions": ["runtime_smoke_mark_event_cursor", "runtime_smoke_run_plan"],
    }


def _runtime_smoke_compact_event_delta(events: Any) -> Any:
    if isinstance(events, list):
        return [compact_value(event) for event in events]
    return compact_value(events)


def _runtime_smoke_missing_event_delta(
    run_id: str,
    after_cursor: int,
    tail: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "reason": tail.get("reason", "runtime smoke run not found"),
        "run_id": run_id,
        "events": [],
        "event_cursor": _runtime_smoke_event_cursor(
            after_cursor=after_cursor,
            tail=tail,
            limit=limit,
        ),
        "final": True,
        "next_actions": ["runtime_smoke_run_plan"],
    }


def _runtime_smoke_tail_missing(tail: dict[str, Any]) -> bool:
    return (
        tail.get("status") == "FAIL"
        and not any(
            key in tail
            for key in (
                "events",
                "next_cursor",
                "oldest_cursor",
                "dropped_count",
                "stale_cursor",
                "final",
            )
        )
    )


def _runtime_smoke_tail_next_cursor(tail: dict[str, Any], fallback: int) -> int:
    return max(0, int(tail.get("next_cursor", fallback)))


def _runtime_smoke_tail_continuation_cursor(tail: dict[str, Any], fallback: int) -> int:
    events = tail.get("events")
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            cursor = _runtime_smoke_parse_nonnegative_int(event.get("cursor"))
            if cursor is not None:
                return max(0, cursor)
        return _runtime_smoke_tail_next_cursor(tail, fallback)
    return max(0, int(fallback))


def _runtime_smoke_parse_nonnegative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _runtime_smoke_event_cursor(
    *,
    after_cursor: int,
    tail: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    return {
        "after_cursor": max(0, int(after_cursor)),
        "next_cursor": _runtime_smoke_tail_next_cursor(tail, after_cursor),
        "oldest_cursor": tail.get("oldest_cursor"),
        "dropped_count": tail.get("dropped_count", 0),
        "stale_cursor": tail.get("stale_cursor", False),
        "limit": max(0, int(limit)),
    }


def _runtime_smoke_cursor_token(
    run_id: str,
    *,
    after_cursor: int,
    tail: dict[str, Any],
) -> dict[str, Any]:
    cursor = max(0, int(after_cursor))
    return {
        "run_id": run_id,
        "after_cursor": cursor,
        "next_cursor": cursor,
        "oldest_cursor": tail.get("oldest_cursor"),
        "dropped_count": tail.get("dropped_count", 0),
        "stale_cursor": tail.get("stale_cursor", False),
    }


def _apply_runtime_smoke_agent_mode(
    data: dict[str, Any],
    primary_next_action: str,
) -> dict[str, Any]:
    if _runtime_smoke_agent_fail_closed(data):
        data["agent_mode"] = _runtime_smoke_agent_mode_payload(
            "runtime_smoke_run_plan",
        )
        return data

    cursor = _runtime_smoke_agent_cursor(data)
    run_id = _runtime_smoke_agent_run_id(data)
    next_request: dict[str, Any] | None = None
    if primary_next_action == "runtime_smoke_get_event_delta":
        if cursor:
            next_request = {
                "tool": primary_next_action,
                "arguments": {"cursor": cursor, "agent_mode": True},
            }
        else:
            primary_next_action = "runtime_smoke_run_plan"
    elif run_id:
        next_request = {
            "tool": primary_next_action,
            "arguments": {"run_id": run_id, "agent_mode": True},
        }
    else:
        primary_next_action = "runtime_smoke_run_plan"

    data["agent_mode"] = _runtime_smoke_agent_mode_payload(
        primary_next_action,
        next_request=next_request,
        cursor=cursor,
    )
    return data


def _runtime_smoke_agent_mode_payload(
    primary_next_action: str,
    *,
    next_request: dict[str, Any] | None = None,
    cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "profile": "compact",
        "primary_next_action": primary_next_action,
        "event_cursor_tools": [
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_get_event_delta",
        ],
    }
    if next_request is not None:
        payload["next_request"] = next_request
    if cursor:
        payload["cursor"] = cursor
    return payload


def _runtime_smoke_agent_fail_closed(data: dict[str, Any]) -> bool:
    if data.get("status") == "INVALID_SETUP":
        return True
    if data.get("reason") == "runtime smoke run not found":
        return True
    return (
        data.get("status") == "FAIL"
        and data.get("result") is None
        and not data.get("events")
        and data.get("next_actions") == ["runtime_smoke_run_plan"]
    )


def _runtime_smoke_agent_run_id(data: dict[str, Any]) -> str:
    return str(data.get("run_id") or data.get("active_run_id") or "")


def _runtime_smoke_agent_cursor(data: dict[str, Any]) -> dict[str, Any]:
    cursor = data.get("cursor")
    if isinstance(cursor, dict):
        return dict(cursor)

    run_id = _runtime_smoke_agent_run_id(data)
    if not run_id:
        return {}

    if isinstance(data.get("event_cursor"), dict):
        event_cursor = data["event_cursor"]
        next_cursor = _runtime_smoke_tail_next_cursor(
            event_cursor,
            event_cursor.get("after_cursor", 0),
        )
        return {
            "run_id": run_id,
            "after_cursor": next_cursor,
            "next_cursor": next_cursor,
            "oldest_cursor": event_cursor.get("oldest_cursor"),
            "dropped_count": event_cursor.get("dropped_count", 0),
            "stale_cursor": event_cursor.get("stale_cursor", False),
        }

    next_cursor = _runtime_smoke_tail_next_cursor(data, 0)
    return {
        "run_id": run_id,
        "after_cursor": next_cursor,
        "next_cursor": next_cursor,
        "oldest_cursor": data.get("oldest_cursor"),
        "dropped_count": data.get("dropped_count", 0),
        "stale_cursor": data.get("stale_cursor", False),
    }


def _runtime_smoke_run_missing(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "FAIL"
        and "final" not in result
        and "next_cursor" not in result
        and "oldest_cursor" not in result
    )


def _bounded_runtime_smoke_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return bounded final result metadata for evidence bundles."""
    bounded = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "action_count": result.get("action_count", 0),
        "cleanup": compact_value(result.get("cleanup", {})),
        "evidence_refs": compact_value(result.get("evidence_refs", [])),
    }
    if "failed_assertions" in result:
        bounded["failed_assertions"] = compact_value(result["failed_assertions"])
    if "compact" in result:
        bounded["compact"] = compact_value(result["compact"])
    return bounded


def _runtime_smoke_evidence_contract() -> dict[str, Any]:
    diagnostics = diagnostic_schema_contract()
    return {
        "result_keys": [
            "status",
            "reason",
            "elapsed_ms",
            "action_count",
            "cleanup",
            "evidence_refs",
            "compact",
        ],
        "compact_limits": dict(diagnostics["evidence_limits"]),
        "diagnostics": diagnostics,
    }


def _build_hygiene_response(
    session: SessionManager,
    result: HygienePreflightResult,
) -> dict:
    message = (
        "Hygiene preflight passed."
        if result.status.value == "PASS"
        else "Hygiene preflight failed."
    )
    return build_response(
        data=result.to_dict(),
        state=session.state.state,
        next_actions=extend_next_actions(
            session.state.state,
            ["debug_hygiene_preflight"],
        ),
        message=message,
    )


def _build_runtime_smoke_response(
    session: SessionManager,
    data: dict[str, Any],
    actions: list[str],
) -> dict:
    return build_response(
        data=data,
        state=session.state.state,
        next_actions=extend_next_actions(session.state.state, actions),
    )


def _instrumentation_service(session: SessionManager) -> InstrumentationGroupService:
    return getattr(session, "instrumentation", None) or InstrumentationGroupService(session)


def _output_assertion_service(session: SessionManager) -> OutputAssertionService:
    return getattr(session, "output_assertions", None) or OutputAssertionService(session)


def _valid_name(name: str) -> bool:
    return bool(_NAME_PATTERN.fullmatch(name))


async def _validate_instrumentation_items(
    ctx: Context,
    session: SessionManager,
    resolve_project_root: Callable[..., Awaitable[Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validated = []
    if items:
        await resolve_project_root(ctx, session)
    for item in items:
        copy = dict(item)
        if "file" not in copy:
            raise ValueError("instrumentation item requires file")
        copy["file"] = session.validate_path(str(copy["file"]))
        validated.append(copy)
    return validated

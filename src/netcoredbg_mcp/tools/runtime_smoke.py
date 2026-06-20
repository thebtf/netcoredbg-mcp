"""Runtime smoke composite tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml
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
    app_diagnostics_launch_contract,
    diagnostic_schema_contract,
    schema_help_fields,
    validate_plan,
)
from ..session.runtime_smoke_v2.result_envelope import compact_value
from ..session.state import DebugState

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RUNTIME_SMOKE_AGENT_DEFAULT_TIMEOUT_MS = 5000
_RUNTIME_SMOKE_AGENT_DEFAULT_POLL_INTERVAL_MS = 500
_RUNTIME_SMOKE_AGENT_DEFAULT_EVENT_LIMIT = 20
_RUNTIME_SMOKE_AGENT_EVENT_LIMIT_TOOLS = frozenset(
    {
        "runtime_smoke_evidence_bundle",
        "runtime_smoke_get_event_delta",
        "runtime_smoke_wait_for_result",
    }
)


def register_runtime_smoke_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
    resolve_project_root: Callable[..., Awaitable[Any]],
    resolve_project_root_readonly: Callable[..., Awaitable[Any]] | None = None,
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
                _runtime_smoke_lifecycle_next_actions(data),
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_validate_plan(
        ctx: Context,
        plan: dict[str, Any] | None = None,
        plan_path: str | None = None,
    ) -> dict:
        """Validate a runtime-smoke plan without launching or touching a target app."""
        try:
            loaded_plan, plan_source, input_error = await _runtime_smoke_resolve_plan_input(
                ctx,
                session,
                resolve_project_root,
                resolve_project_root_readonly=resolve_project_root_readonly,
                plan=plan,
                plan_path=plan_path,
            )
            if input_error is not None:
                return _build_runtime_smoke_response(
                    session,
                    input_error,
                    ["runtime_smoke_validate_plan", "runtime_smoke_run_plan"],
                )
            data = validate_runtime_smoke_plan_contract(loaded_plan)
            if plan_source is not None:
                data["plan_source"] = plan_source
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
        plan: dict[str, Any] | None = None,
        plan_path: str | None = None,
        agent_mode: bool = False,
    ) -> dict:
        """Validate then start a durable runtime smoke run."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            loaded_plan, plan_source, input_error = await _runtime_smoke_resolve_plan_input(
                ctx,
                session,
                resolve_project_root,
                plan=plan,
                plan_path=plan_path,
            )
            if input_error is not None:
                if agent_mode:
                    _apply_runtime_smoke_agent_mode(input_error, "runtime_smoke_run_plan")
                return _build_runtime_smoke_response(
                    session,
                    input_error,
                    ["runtime_smoke_validate_plan", "runtime_smoke_run_plan"],
                )

            validation = validate_runtime_smoke_plan_contract(loaded_plan)
            if plan_source is not None:
                validation["plan_source"] = plan_source
            if not validation["can_run"]:
                if agent_mode:
                    _apply_runtime_smoke_agent_mode(validation, "runtime_smoke_run_plan")
                return _build_runtime_smoke_response(
                    session,
                    validation,
                    ["runtime_smoke_validate_plan", "runtime_smoke_run_plan"],
                )

            data = await session.runtime_smoke.lifecycle_runs.start(loaded_plan, _runner)
            data["validation"] = _runtime_smoke_validation_summary(validation)
            next_actions = _runtime_smoke_lifecycle_next_actions(data)
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, next_actions[0])
            return _build_runtime_smoke_response(
                session,
                data,
                next_actions,
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def runtime_smoke_validate_probe(
        ctx: Context,
        probe: dict[str, Any],
        name: str | None = None,
        phase: str = "after",
        budgets: dict[str, Any] | None = None,
        debug_preflight: bool = False,
        tracepoint_guard: dict[str, Any] | None = None,
        agent_mode: bool = False,
    ) -> dict:
        """Validate one generated probe plan and return agent-mode run guidance."""
        try:
            plan, generated = _runtime_smoke_probe_plan(
                probe,
                name=name,
                phase=phase,
                budgets=budgets,
                debug_preflight=debug_preflight,
                tracepoint_guard=tracepoint_guard,
            )
            data = {
                **validate_runtime_smoke_plan_contract(plan),
                "run_created": False,
                "probe": _runtime_smoke_probe_summary(probe),
                "generated_plan": generated,
            }
            if agent_mode:
                _apply_runtime_smoke_validate_probe_agent_mode(
                    data,
                    probe=probe,
                    name=name,
                    phase=phase,
                    budgets=budgets,
                    debug_preflight=debug_preflight,
                    tracepoint_guard=tracepoint_guard,
                )
            next_actions = (
                ["runtime_smoke_run_probe", "runtime_smoke_validate_plan"]
                if data.get("can_run") is True
                else ["runtime_smoke_validate_probe", "runtime_smoke_validate_plan"]
            )
            return _build_runtime_smoke_response(
                session,
                data,
                next_actions,
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
        debug_preflight: bool = False,
        tracepoint_guard: dict[str, Any] | None = None,
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
                debug_preflight=debug_preflight,
                tracepoint_guard=tracepoint_guard,
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
                    _apply_runtime_smoke_validate_probe_agent_mode(
                        data,
                        probe=probe,
                        name=name,
                        phase=phase,
                        budgets=budgets,
                        debug_preflight=debug_preflight,
                        tracepoint_guard=tracepoint_guard,
                    )
                return _build_runtime_smoke_response(
                    session,
                    data,
                    ["runtime_smoke_validate_probe", "runtime_smoke_validate_plan"],
                )

            data = await session.runtime_smoke.lifecycle_runs.start(plan, _runner)
            data["validation"] = _runtime_smoke_validation_summary(validation)
            data["probe"] = _runtime_smoke_probe_summary(probe)
            data["generated_plan"] = generated
            data["run_created"] = bool(data.get("run_id"))
            next_actions = _runtime_smoke_lifecycle_next_actions(data)
            if agent_mode:
                _apply_runtime_smoke_agent_mode(data, next_actions[0])
            return _build_runtime_smoke_response(
                session,
                data,
                next_actions,
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
    async def runtime_smoke_wait_for_result(
        ctx: Context,
        run_id: str,
        timeout_ms: int = 1000,
        poll_interval_ms: int = 100,
        after_cursor: int = 0,
        event_limit: int = 50,
        agent_mode: bool = False,
    ) -> dict:
        """Wait for a durable runtime smoke run and return compact evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runtime_smoke_wait_for_result(
                session.runtime_smoke.lifecycle_runs,
                run_id,
                timeout_ms=timeout_ms,
                poll_interval_ms=poll_interval_ms,
                after_cursor=after_cursor,
                event_limit=event_limit,
            )
            if agent_mode:
                primary = (
                    "runtime_smoke_get_event_delta"
                    if data.get("final")
                    else "runtime_smoke_wait_for_result"
                )
                _apply_runtime_smoke_agent_mode(data, primary)
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
        include_debug_output: bool = False,
    ) -> dict:
        """Return a compact cursor token for the current durable run event position."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await _runtime_smoke_mark_event_cursor(
                session.runtime_smoke.lifecycle_runs,
                run_id,
                session_state=session.state,
                include_debug_output=include_debug_output,
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
                session_state=session.state,
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
            next_actions = (
                _runtime_smoke_lifecycle_next_actions(data)
                if data.get("contaminated") is True
                else [
                    "runtime_smoke_tail_events",
                    "runtime_smoke_get_result",
                    "runtime_smoke_stop",
                ]
            )
            return _build_runtime_smoke_response(
                session,
                data,
                next_actions,
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
            next_actions = (
                _runtime_smoke_lifecycle_next_actions(data)
                if data.get("contaminated") is True
                else [
                    "runtime_smoke_tail_events",
                    "runtime_smoke_stop",
                    "runtime_smoke_start",
                ]
            )
            return _build_runtime_smoke_response(
                session,
                data,
                next_actions,
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
                _runtime_smoke_lifecycle_next_actions(data),
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
    async def runtime_smoke_cleanup_contract(ctx: Context) -> dict:
        """Clear runtime-smoke contamination after failed or timed-out cleanup."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            data = await session.runtime_smoke.lifecycle_runs.cleanup_contract(
                reset=session.runtime_smoke.reset,
            )
            return _build_runtime_smoke_response(
                session,
                data,
                _runtime_smoke_cleanup_contract_next_actions(data),
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


async def _runtime_smoke_resolve_plan_input(
    ctx: Context,
    session: SessionManager,
    resolve_project_root: Callable[..., Awaitable[Any]],
    *,
    resolve_project_root_readonly: Callable[..., Awaitable[Any]] | None = None,
    plan: Any,
    plan_path: str | None,
) -> tuple[Any, dict[str, str] | None, dict[str, Any] | None]:
    has_inline_plan = plan is not None
    has_plan_path = bool(plan_path)
    if has_inline_plan == has_plan_path:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                "provide exactly one runtime smoke plan input: plan or plan_path"
            ),
        )
    if has_inline_plan:
        return plan, None, None

    assert plan_path is not None
    if resolve_project_root_readonly is None:
        await resolve_project_root(ctx, session)
        project_path = session.project_path
    else:
        project_root = await resolve_project_root_readonly(ctx, session)
        project_path = str(project_root) if project_root is not None else session.project_path

    if not project_path:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                "plan_path validation failed: project root is not resolved",
                plan_source=_runtime_smoke_plan_source(plan_path),
            ),
        )
    try:
        if resolve_project_root_readonly is None:
            validated_path = session.validate_path(plan_path)
        else:
            validated_path = session.validate_path_for_project(plan_path, project_path)
    except ValueError as exc:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path validation failed: {exc}",
                plan_source=_runtime_smoke_plan_source(plan_path),
            ),
        )

    plan_source = _runtime_smoke_plan_source(validated_path)
    plan_format = plan_source["format"]
    try:
        plan_text = Path(validated_path).read_text(encoding="utf-8")
        loaded = _runtime_smoke_parse_plan_text(plan_text, plan_format)
    except json.JSONDecodeError as exc:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path JSON parse failed: {exc.msg}",
                plan_source=plan_source,
            ),
        )
    except yaml.YAMLError as exc:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path YAML parse failed: {exc}",
                plan_source=plan_source,
            ),
        )
    except UnicodeDecodeError as exc:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path UTF-8 decode failed: {exc}",
                plan_source=plan_source,
            ),
        )
    except OSError as exc:
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path read failed: {exc}",
                plan_source=plan_source,
            ),
        )
    if not isinstance(loaded, dict):
        return (
            None,
            None,
            _runtime_smoke_plan_input_error(
                f"plan_path {plan_format.upper()} root must be an object",
                plan_source=plan_source,
            ),
        )
    return loaded, plan_source, None


def _runtime_smoke_parse_plan_text(plan_text: str, plan_format: str) -> Any:
    if plan_format == "yaml":
        return yaml.safe_load(plan_text)
    return json.loads(plan_text)


def _runtime_smoke_plan_format(plan_path: str) -> str:
    suffix = Path(plan_path).suffix.lower()
    return "yaml" if suffix in {".yaml", ".yml"} else "json"


def _runtime_smoke_plan_source(plan_path: str) -> dict[str, str]:
    return {"kind": "file", "path": str(plan_path), "format": _runtime_smoke_plan_format(plan_path)}


def _runtime_smoke_plan_input_error(
    reason: str,
    *,
    plan_source: dict[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "INVALID_SETUP",
        "can_run": False,
        "validation_errors": [reason],
        "accepted_input": {
            "plan": "inline JSON object",
            "plan_path": "path to a UTF-8 JSON or YAML object plan file",
        },
        **schema_help_fields(None),
        "evidence_contract": _runtime_smoke_evidence_contract(),
    }
    if plan_source is not None:
        result["plan_source"] = plan_source
    return result


def _runtime_smoke_validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "can_run",
        "validation_errors",
        "case_count",
        "generated_case_count",
        "evidence_contract",
        "plan_source",
    )
    return {key: validation[key] for key in keys if key in validation}


def _runtime_smoke_probe_plan(
    probe: dict[str, Any],
    *,
    name: str | None = None,
    phase: str = "after",
    budgets: dict[str, Any] | None = None,
    debug_preflight: bool = False,
    tracepoint_guard: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wrap a single probe in the smallest executable v2 runtime-smoke plan."""
    probe_payload = dict(probe) if isinstance(probe, dict) else {"kind": ""}
    if "phase" not in probe_payload and "phases" not in probe_payload:
        probe_payload["phase"] = str(phase or "after")

    kind = str(probe_payload.get("kind") or "")
    probe_name = str(probe_payload.get("name") or kind or "probe")
    plan_name = str(name or f"run-probe-{probe_name}")
    diagnostic_launch = (
        app_diagnostics_launch_contract(name=plan_name)
        if kind == "app_diagnostics"
        else None
    )
    case: dict[str, Any] = {
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
    guard_cleanup = _runtime_smoke_tracepoint_guard_cleanup(
        probe_payload,
        tracepoint_guard=tracepoint_guard,
    )
    if guard_cleanup:
        case["cleanup"] = guard_cleanup

    plan = {
        "schema": SCHEMA_VERSION_V2,
        "name": plan_name,
        "cases": [case],
        "budgets": dict(budgets or {"max_actions": 1, "max_elapsed_seconds": 5}),
    }
    if diagnostic_launch is not None:
        plan["diagnostics"] = {
            "app_diagnostics": {"diagnostic_launch": diagnostic_launch}
        }
    if debug_preflight:
        plan["baseline"] = {
            "steps": [
                {
                    "id": "debug_preflight",
                    "kind": "debug_hygiene_preflight",
                    "file": probe_payload.get("file"),
                    "clear_breakpoints": True,
                    "clear_trace_log": True,
                    "clear_exception_filters": False,
                }
            ]
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
    if debug_preflight:
        generated["debug_preflight"] = True
    if diagnostic_launch is not None:
        generated["diagnostic_launch"] = diagnostic_launch
    if guard_cleanup:
        generated["tracepoint_guard"] = {
            "cleanup_operations": _runtime_smoke_tracepoint_cleanup_operations(
                guard_cleanup
            )
        }
    return plan, generated


def _runtime_smoke_tracepoint_guard_cleanup(
    probe_payload: dict[str, Any],
    *,
    tracepoint_guard: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if str(probe_payload.get("kind") or "") != "debug.tracepoint":
        return []

    cleanup = dict((tracepoint_guard or {}).get("cleanup") or {})
    raw_operations = cleanup.get("operations")
    if not isinstance(raw_operations, list):
        return []
    operations = [str(operation) for operation in raw_operations]
    guard_steps: list[dict[str, Any]] = []
    if "debug.trace_log.clear" in operations:
        guard_steps.append({"kind": "debug.trace_log.clear"})
    if "debug.tracepoint.remove" in operations:
        guard_steps.append(
            {
                "kind": "debug.tracepoint.remove",
                "id": _runtime_smoke_tracepoint_cleanup_id(probe_payload),
                "file": probe_payload.get("file"),
                "line": probe_payload.get("line"),
            }
        )
    return guard_steps


def _runtime_smoke_tracepoint_cleanup_id(probe_payload: dict[str, Any]) -> str:
    file_name = str(probe_payload.get("file") or "tracepoint").replace("\\", "/")
    file_name = file_name.rsplit("/", 1)[-1] or "tracepoint"
    line = probe_payload.get("line")
    return f"{file_name}:{line}"


def _runtime_smoke_tracepoint_cleanup_operations(
    cleanup_steps: list[dict[str, Any]],
) -> list[str]:
    return [str(step.get("kind") or "") for step in reversed(cleanup_steps)]


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
    if tail.get("final") and not result.get("final"):
        result = await registry.get_result(run_id)
    final = bool(result.get("final"))
    if result.get("contaminated") is True:
        next_actions = _runtime_smoke_lifecycle_next_actions(result)
    else:
        next_actions = [
            "runtime_smoke_wait_for_result",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_tail_events",
            "runtime_smoke_get_result",
        ]
        next_actions.append("runtime_smoke_run_plan" if final else "runtime_smoke_stop")
    diagnostic_launch = result.get("diagnostic_launch")

    bundle = {
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
    if isinstance(diagnostic_launch, dict):
        bundle["diagnostic_launch"] = compact_value(diagnostic_launch)
    if result.get("contaminated") is True:
        bundle["contaminated"] = True
        cleanup_contract = result.get("cleanup_contract")
        if isinstance(cleanup_contract, dict):
            bundle["cleanup_contract"] = compact_value(cleanup_contract)
    return bundle


async def _runtime_smoke_wait_for_result(
    registry: Any,
    run_id: str,
    *,
    timeout_ms: int = 1000,
    poll_interval_ms: int = 100,
    after_cursor: int = 0,
    event_limit: int = 50,
) -> dict[str, Any]:
    timeout = _runtime_smoke_parse_nonnegative_int(timeout_ms)
    if timeout is None:
        return _runtime_smoke_invalid_wait(
            "timeout_ms must be a non-negative integer",
            run_id=run_id,
            after_cursor=after_cursor,
            event_limit=event_limit,
        )
    interval = _runtime_smoke_parse_nonnegative_int(poll_interval_ms)
    if interval is None:
        return _runtime_smoke_invalid_wait(
            "poll_interval_ms must be a non-negative integer",
            run_id=run_id,
            after_cursor=after_cursor,
            event_limit=event_limit,
        )
    parsed_after_cursor = _runtime_smoke_parse_nonnegative_int(after_cursor)
    if parsed_after_cursor is None:
        return _runtime_smoke_invalid_wait(
            "after_cursor must be a non-negative integer",
            run_id=run_id,
            after_cursor=0,
            event_limit=event_limit,
        )
    parsed_event_limit = _runtime_smoke_parse_nonnegative_int(event_limit)
    if parsed_event_limit is None:
        return _runtime_smoke_invalid_wait(
            "event_limit must be a non-negative integer",
            run_id=run_id,
            after_cursor=parsed_after_cursor,
            event_limit=0,
        )

    deadline = time.monotonic() + timeout / 1000
    sleep_s = max(1, interval) / 1000
    data = await _runtime_smoke_evidence_bundle(
        registry,
        run_id,
        after_cursor=parsed_after_cursor,
        event_limit=parsed_event_limit,
    )
    if data.get("final"):
        return _runtime_smoke_wait_ready(data)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _runtime_smoke_wait_timed_out(data)
        await asyncio.sleep(min(sleep_s, max(0.0, remaining)))
        data = await _runtime_smoke_evidence_bundle(
            registry,
            run_id,
            after_cursor=parsed_after_cursor,
            event_limit=parsed_event_limit,
        )
        if data.get("final"):
            return _runtime_smoke_wait_ready(data)


def _runtime_smoke_wait_ready(data: dict[str, Any]) -> dict[str, Any]:
    if _runtime_smoke_wait_missing(data):
        return data
    if data.get("contaminated") is True:
        return data
    return _runtime_smoke_wait_with_next_actions(data)


def _runtime_smoke_wait_timed_out(data: dict[str, Any]) -> dict[str, Any]:
    return _runtime_smoke_wait_with_next_actions(
        {
            **data,
            "status": "BLOCKED",
            "reason": "runtime smoke wait timed out",
            "final": False,
            "next_step": (
                "Poll again with runtime_smoke_evidence_bundle or increase timeout_ms."
            ),
        },
        include_stop=True,
    )


def _runtime_smoke_invalid_wait(
    reason: str,
    *,
    run_id: str,
    after_cursor: Any,
    event_limit: Any,
) -> dict[str, Any]:
    safe_after_cursor = _runtime_smoke_parse_nonnegative_int(after_cursor) or 0
    safe_event_limit = _runtime_smoke_parse_nonnegative_int(event_limit) or 0
    return _runtime_smoke_wait_with_next_actions(
        {
            "status": "FAIL",
            "reason": reason,
            "run_id": run_id,
            "final": True,
            "events": [],
            "event_cursor": {
                "after_cursor": safe_after_cursor,
                "next_cursor": safe_after_cursor,
                "oldest_cursor": None,
                "dropped_count": 0,
                "stale_cursor": False,
                "limit": safe_event_limit,
            },
            "result": None,
            "evidence_refs": [],
            "cleanup": None,
        }
    )


def _runtime_smoke_wait_missing(data: dict[str, Any]) -> bool:
    return (
        data.get("status") == "FAIL"
        and data.get("reason") == "runtime smoke run not found"
        and data.get("events") == []
        and data.get("result") is None
    )


def _runtime_smoke_wait_with_next_actions(
    data: dict[str, Any],
    *,
    include_stop: bool = False,
) -> dict[str, Any]:
    actions = list(data.get("next_actions") or [])
    for action in (
        "runtime_smoke_wait_for_result",
        "runtime_smoke_evidence_bundle",
        "runtime_smoke_tail_events",
    ):
        if action not in actions:
            actions.append(action)
    if include_stop and "runtime_smoke_stop" not in actions:
        actions.append("runtime_smoke_stop")
    return {**data, "next_actions": actions}


async def _runtime_smoke_mark_event_cursor(
    registry: Any,
    run_id: str,
    *,
    session_state: Any | None = None,
    include_debug_output: bool = False,
) -> dict[str, Any]:
    tail = await registry.tail_events(run_id, after_cursor=0, limit=0)
    if _runtime_smoke_tail_missing(tail):
        return _runtime_smoke_missing_event_delta(run_id, 0, tail, limit=0)

    next_cursor = _runtime_smoke_tail_next_cursor(tail, 0)
    cursor = _runtime_smoke_cursor_token(
        run_id,
        after_cursor=next_cursor,
        tail=tail,
    )
    if include_debug_output:
        _runtime_smoke_attach_debug_output_cursor(cursor, session_state)
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
    session_state: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(cursor, dict):
        return _runtime_smoke_invalid_event_delta(
            "cursor token must be an object",
            after_cursor=0,
            limit=event_limit,
        )
    parsed_debug_output_cursor = _runtime_smoke_parse_debug_output_source_cursor(cursor)
    if parsed_debug_output_cursor is False:
        return _runtime_smoke_invalid_event_delta(
            "cursor token debug_output cursor is invalid",
            after_cursor=0,
            limit=event_limit,
            run_id=str(cursor.get("run_id") or ""),
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
    next_cursor = _runtime_smoke_cursor_token(
        run_id,
        after_cursor=continuation_cursor,
        tail=tail,
    )
    source_deltas: dict[str, Any] = {}
    if parsed_debug_output_cursor is not None:
        debug_output_delta, debug_output_cursor = _runtime_smoke_debug_output_delta(
            session_state,
            after_sequence=parsed_debug_output_cursor["after_sequence"],
            trimmed_before=parsed_debug_output_cursor["trimmed_before"],
            limit=limit,
        )
        source_deltas["debug_output"] = debug_output_delta
        next_cursor["sources"] = {"debug_output": debug_output_cursor}
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
        "cursor": next_cursor,
        "final": bool(tail.get("final")),
        "next_actions": [
            "runtime_smoke_get_event_delta",
            "runtime_smoke_mark_event_cursor",
            "runtime_smoke_evidence_bundle",
            "runtime_smoke_tail_events",
        ],
        **({"source_deltas": source_deltas} if source_deltas else {}),
    }


def _runtime_smoke_invalid_event_delta(
    reason: str,
    *,
    after_cursor: int,
    limit: Any,
    run_id: str = "",
) -> dict[str, Any]:
    parsed_limit = _runtime_smoke_parse_nonnegative_int(limit)
    next_actions = ["runtime_smoke_run_plan"]
    if run_id:
        next_actions.insert(0, "runtime_smoke_mark_event_cursor")
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
        "next_actions": next_actions,
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


def _runtime_smoke_attach_debug_output_cursor(
    cursor: dict[str, Any],
    session_state: Any | None,
) -> None:
    debug_output_cursor = _runtime_smoke_current_debug_output_cursor(session_state)
    if debug_output_cursor is None:
        return
    sources = dict(cursor.get("sources") or {})
    sources["debug_output"] = debug_output_cursor
    cursor["sources"] = sources


def _runtime_smoke_current_debug_output_cursor(
    session_state: Any | None,
) -> dict[str, int] | None:
    if session_state is None:
        return None
    return {
        "after_sequence": max(0, int(getattr(session_state, "output_sequence", 0) or 0)),
        "trimmed_before": max(
            0, int(getattr(session_state, "output_trimmed_before", 0) or 0)
        ),
    }


def _runtime_smoke_parse_debug_output_source_cursor(
    cursor: dict[str, Any],
) -> dict[str, int] | None | bool:
    sources = cursor.get("sources")
    if sources is None:
        return None
    if not isinstance(sources, dict):
        return False
    debug_output = sources.get("debug_output")
    if debug_output is None:
        return None
    if not isinstance(debug_output, dict):
        return False
    after_sequence = _runtime_smoke_parse_nonnegative_int(
        debug_output.get("after_sequence")
    )
    trimmed_before = _runtime_smoke_parse_nonnegative_int(
        debug_output.get("trimmed_before")
    )
    if after_sequence is None or trimmed_before is None:
        return False
    return {
        "after_sequence": after_sequence,
        "trimmed_before": trimmed_before,
    }


def _runtime_smoke_debug_output_delta(
    session_state: Any | None,
    *,
    after_sequence: int,
    trimmed_before: int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    entries = (
        list(getattr(session_state, "output_buffer", []))
        if session_state is not None
        else []
    )
    current_sequence = (
        max(0, int(getattr(session_state, "output_sequence", 0) or 0))
        if session_state is not None
        else 0
    )
    current_trimmed_before = (
        max(0, int(getattr(session_state, "output_trimmed_before", 0) or 0))
        if session_state is not None
        else 0
    )
    bounded_limit = max(0, int(limit))
    filtered_entries = [
        entry
        for entry in entries
        if int(getattr(entry, "sequence", 0) or 0) > after_sequence
    ]
    available = len(filtered_entries)
    bounded_entries = filtered_entries[:bounded_limit]
    first_retained_sequence = (
        int(getattr(filtered_entries[0], "sequence", 0) or 0)
        if filtered_entries
        else None
    )
    cleared_gap = available == 0 and current_sequence > after_sequence
    retained_gap = (
        first_retained_sequence is not None
        and first_retained_sequence > max(after_sequence + 1, trimmed_before + 1)
    )
    stale_cursor = (
        current_trimmed_before > max(after_sequence, trimmed_before)
        or cleared_gap
        or retained_gap
    )
    dropped_count = max(0, current_trimmed_before - max(after_sequence, trimmed_before))
    if cleared_gap:
        dropped_count = max(dropped_count, current_sequence - after_sequence)
    if retained_gap and first_retained_sequence is not None:
        dropped_count = max(dropped_count, first_retained_sequence - after_sequence - 1)
    if bounded_entries:
        next_after_sequence = max(
            int(getattr(entry, "sequence", 0) or 0) for entry in bounded_entries
        )
    elif available == 0 and not stale_cursor:
        next_after_sequence = current_sequence
    else:
        next_after_sequence = after_sequence
    return (
        {
            "entries": [
                _runtime_smoke_output_entry_to_dict(entry) for entry in bounded_entries
            ],
            "available": available,
            "limit": bounded_limit,
            "limited": available > bounded_limit,
            "stale_cursor": stale_cursor,
            "dropped_count": dropped_count,
        },
        {
            "after_sequence": max(0, int(next_after_sequence)),
            "trimmed_before": current_trimmed_before,
        },
    )


def _runtime_smoke_output_entry_to_dict(entry: Any) -> dict[str, Any]:
    return compact_value(
        {
        "text": str(getattr(entry, "text", "")),
        "category": str(getattr(entry, "category", "console") or "console"),
        "variables_reference": max(
            0, int(getattr(entry, "variables_reference", 0) or 0)
        ),
        "sequence": max(0, int(getattr(entry, "sequence", 0) or 0)),
        }
    )


def _apply_runtime_smoke_agent_mode(
    data: dict[str, Any],
    primary_next_action: str,
) -> dict[str, Any]:
    if (
        primary_next_action == "runtime_smoke_get_event_delta"
        and data.get("status") == "INVALID_SETUP"
        and isinstance(data.get("run_id"), str)
        and data.get("run_id")
    ):
        data["agent_mode"] = _runtime_smoke_agent_mode_payload(
            "runtime_smoke_mark_event_cursor",
            next_request={
                "tool": "runtime_smoke_mark_event_cursor",
                "arguments": {
                    "run_id": data["run_id"],
                    "agent_mode": True,
                },
            },
            metrics=_runtime_smoke_agent_metrics(data),
        )
        return data
    if (
        data.get("contaminated") is True
        and data.get("final") is True
        and isinstance(data.get("cleanup_contract"), dict)
        and data["cleanup_contract"].get("next_action") == "runtime_smoke_cleanup_contract"
    ):
        data["agent_mode"] = _runtime_smoke_agent_mode_payload(
            "runtime_smoke_cleanup_contract",
            next_request={
                "tool": "runtime_smoke_cleanup_contract",
                "arguments": {},
            },
            cursor=_runtime_smoke_agent_cursor(data),
            metrics=_runtime_smoke_agent_metrics(data),
        )
        return data
    if _runtime_smoke_agent_fail_closed(data):
        data["agent_mode"] = _runtime_smoke_agent_mode_payload(
            "runtime_smoke_run_plan",
            metrics=_runtime_smoke_agent_metrics(data),
        )
        return data

    cursor = _runtime_smoke_agent_cursor(data)
    run_id = _runtime_smoke_agent_run_id(data)
    next_request: dict[str, Any] | None = None
    if primary_next_action == "runtime_smoke_get_event_delta":
        if cursor:
            next_request = {
                "tool": primary_next_action,
                "arguments": _runtime_smoke_agent_next_arguments(
                    primary_next_action,
                    {"cursor": cursor, "agent_mode": True},
                ),
            }
        else:
            primary_next_action = "runtime_smoke_run_plan"
    elif primary_next_action == "runtime_smoke_wait_for_result":
        if run_id:
            arguments: dict[str, Any] = {"run_id": run_id, "agent_mode": True}
            if cursor:
                arguments["after_cursor"] = _runtime_smoke_tail_next_cursor(
                    cursor,
                    cursor.get("after_cursor", 0),
                )
            next_request = {
                "tool": primary_next_action,
                "arguments": _runtime_smoke_agent_next_arguments(
                    primary_next_action,
                    arguments,
                ),
            }
        else:
            primary_next_action = "runtime_smoke_run_plan"
    elif primary_next_action == "runtime_smoke_cleanup_contract":
        next_request = {
            "tool": primary_next_action,
            "arguments": {},
        }
    elif run_id:
        next_request = {
            "tool": primary_next_action,
            "arguments": _runtime_smoke_agent_next_arguments(
                primary_next_action,
                {"run_id": run_id, "agent_mode": True},
            ),
        }
    else:
        primary_next_action = "runtime_smoke_run_plan"

    data["agent_mode"] = _runtime_smoke_agent_mode_payload(
        primary_next_action,
        next_request=next_request,
        cursor=cursor,
        metrics=_runtime_smoke_agent_metrics(data),
    )
    return data


def _apply_runtime_smoke_validate_probe_agent_mode(
    data: dict[str, Any],
    *,
    probe: dict[str, Any],
    name: str | None,
    phase: str,
    budgets: dict[str, Any] | None,
    debug_preflight: bool,
    tracepoint_guard: dict[str, Any] | None,
) -> None:
    arguments = _runtime_smoke_validate_probe_arguments(
        probe=probe,
        name=name,
        phase=phase,
        budgets=budgets,
        debug_preflight=debug_preflight,
        tracepoint_guard=tracepoint_guard,
        agent_mode=True,
    )
    if data.get("can_run") is not True:
        data["agent_mode"] = _runtime_smoke_agent_mode_payload(
            "runtime_smoke_validate_probe",
            next_request={
                "tool": "runtime_smoke_validate_probe",
                "arguments": arguments,
            },
            metrics=_runtime_smoke_agent_metrics(data),
        )
        return

    data["agent_mode"] = _runtime_smoke_agent_mode_payload(
        "runtime_smoke_run_probe",
        next_request={
            "tool": "runtime_smoke_run_probe",
            "arguments": arguments,
        },
        metrics=_runtime_smoke_agent_metrics(data),
    )


def _runtime_smoke_validate_probe_arguments(
    *,
    probe: dict[str, Any],
    name: str | None,
    phase: str,
    budgets: dict[str, Any] | None,
    debug_preflight: bool,
    tracepoint_guard: dict[str, Any] | None,
    agent_mode: bool,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "probe": dict(probe) if isinstance(probe, dict) else probe,
        "agent_mode": agent_mode,
    }
    if name is not None:
        arguments["name"] = name
    if phase != "after":
        arguments["phase"] = phase
    if budgets is not None:
        arguments["budgets"] = dict(budgets) if isinstance(budgets, dict) else budgets
    if debug_preflight:
        arguments["debug_preflight"] = True
    if tracepoint_guard is not None:
        arguments["tracepoint_guard"] = (
            dict(tracepoint_guard)
            if isinstance(tracepoint_guard, dict)
            else tracepoint_guard
        )
    return arguments


def _runtime_smoke_agent_mode_payload(
    primary_next_action: str,
    *,
    next_request: dict[str, Any] | None = None,
    cursor: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "profile": "compact",
        "defaults": _runtime_smoke_agent_defaults(),
        "primary_next_action": primary_next_action,
        "metrics_contract": _runtime_smoke_agent_metrics_contract(),
        "metrics": metrics or _runtime_smoke_agent_metrics({}),
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


def _runtime_smoke_agent_defaults() -> dict[str, Any]:
    return {
        "profile": "agent",
        "timeout_ms": _RUNTIME_SMOKE_AGENT_DEFAULT_TIMEOUT_MS,
        "poll_interval_ms": _RUNTIME_SMOKE_AGENT_DEFAULT_POLL_INTERVAL_MS,
        "event_limit": _RUNTIME_SMOKE_AGENT_DEFAULT_EVENT_LIMIT,
        "raw_evidence": {
            "include_raw_dumps": False,
            "tree_dump_policy": "omit",
            "screenshot_policy": "artifact-reference",
        },
        "verdicts": ["PASS", "FAIL", "BLOCKED", "INVALID_SETUP", "ERROR"],
        "single_flight": {
            "overlap": "reject",
            "returns_active_run": True,
        },
        "cleanup": {
            "require_cleanup_contract": True,
            "surface_contamination": True,
        },
    }


def _runtime_smoke_agent_next_arguments(
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    compact_arguments = dict(arguments)
    if tool in _RUNTIME_SMOKE_AGENT_EVENT_LIMIT_TOOLS:
        compact_arguments.setdefault(
            "event_limit",
            _RUNTIME_SMOKE_AGENT_DEFAULT_EVENT_LIMIT,
        )
    return compact_arguments


def _runtime_smoke_agent_metrics_contract() -> dict[str, Any]:
    return {
        "fields": [
            "time_to_verdict_ms",
            "retry_count",
            "evidence_completeness",
            "wrong_target_prevention",
            "focus_foreground_checks",
        ],
        "missing_state": "NO DATA",
    }


def _runtime_smoke_agent_metrics(data: dict[str, Any]) -> dict[str, Any]:
    missing_reason = _runtime_smoke_agent_metric_missing_reason(data)
    if missing_reason in {
        "runtime smoke run not found",
        "runtime smoke run not started",
    }:
        return {
            "time_to_verdict_ms": _runtime_smoke_agent_no_data(missing_reason),
            "retry_count": _runtime_smoke_agent_no_data(missing_reason),
            "evidence_completeness": _runtime_smoke_agent_no_data(missing_reason),
            "wrong_target_prevention": _runtime_smoke_agent_no_data(missing_reason),
            "focus_foreground_checks": _runtime_smoke_agent_no_data(missing_reason),
        }

    return {
        "time_to_verdict_ms": _runtime_smoke_agent_time_to_verdict_metric(
            data,
            missing_reason,
        ),
        "retry_count": _runtime_smoke_agent_retry_count_metric(data),
        "evidence_completeness": _runtime_smoke_agent_evidence_completeness(data),
        "wrong_target_prevention": _runtime_smoke_agent_no_data(
            _runtime_smoke_agent_source_evidence_reason(
                missing_reason,
                "wrong-target prevention evidence is not present",
            ),
        ),
        "focus_foreground_checks": _runtime_smoke_agent_no_data(
            _runtime_smoke_agent_source_evidence_reason(
                missing_reason,
                "focus/foreground evidence is not present",
            ),
        ),
    }


def _runtime_smoke_agent_metric_missing_reason(data: dict[str, Any]) -> str:
    if data.get("status") == "FAIL" and data.get("result") is None:
        reason = data.get("reason")
        if reason:
            return str(reason)
    if not _runtime_smoke_agent_run_id(data):
        return "runtime smoke run not started"
    if not data.get("final"):
        return "run is not final"
    return "source evidence is absent"


def _runtime_smoke_agent_time_to_verdict_metric(
    data: dict[str, Any],
    missing_reason: str,
) -> dict[str, Any]:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    elapsed_ms = result.get("elapsed_ms") if isinstance(result, dict) else None
    if data.get("final") and isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool):
        return {"status": "MEASURED", "value": max(0, elapsed_ms)}
    return _runtime_smoke_agent_no_data(missing_reason)


def _runtime_smoke_agent_evidence_completeness(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("final"):
        return _runtime_smoke_agent_no_data("run is not final")

    signals = {
        "result": isinstance(data.get("result"), dict),
        "events": bool(data.get("events")),
        "event_cursor": isinstance(data.get("event_cursor"), dict),
    }
    if not any(signals.values()):
        return _runtime_smoke_agent_no_data("source evidence is absent")

    return {
        "status": "COMPLETE" if all(signals.values()) else "PARTIAL",
        "signals": signals,
    }


def _runtime_smoke_agent_retry_count_metric(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    retry_count = result.get("retry_count") if isinstance(result, dict) else None
    if isinstance(retry_count, int) and not isinstance(retry_count, bool):
        return {"status": "MEASURED", "value": max(0, retry_count)}
    return _runtime_smoke_agent_no_data("retry count evidence is not present")


def _runtime_smoke_agent_no_data(reason: str) -> dict[str, Any]:
    return {"status": "NO DATA", "reason": reason}


def _runtime_smoke_agent_source_evidence_reason(
    missing_reason: str,
    source_absent_reason: str,
) -> str:
    if missing_reason == "source evidence is absent":
        return source_absent_reason
    return missing_reason


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


def _runtime_smoke_lifecycle_next_actions(data: dict[str, Any]) -> list[str]:
    if data.get("contaminated") is True:
        if data.get("run_id") and not data.get("final"):
            return [
                "runtime_smoke_wait_for_result",
                "runtime_smoke_evidence_bundle",
                "runtime_smoke_tail_events",
                "runtime_smoke_get_result",
                "runtime_smoke_stop",
                "runtime_smoke_cleanup_contract",
                "debug_hygiene_preflight",
            ]
        return ["runtime_smoke_cleanup_contract", "debug_hygiene_preflight"]
    return [
        "runtime_smoke_evidence_bundle",
        "runtime_smoke_wait_for_result",
        "runtime_smoke_tail_events",
        "runtime_smoke_get_result",
        "runtime_smoke_stop",
    ]


def _runtime_smoke_cleanup_contract_next_actions(data: dict[str, Any]) -> list[str]:
    if data.get("contaminated") is True:
        return ["runtime_smoke_cleanup_contract", "debug_hygiene_preflight"]
    if data.get("status") == "BLOCKED" and data.get("run_id"):
        return [
            "runtime_smoke_wait_for_result",
            "runtime_smoke_tail_events",
            "runtime_smoke_get_result",
            "runtime_smoke_stop",
        ]
    return ["runtime_smoke_run_plan", "runtime_smoke_start", "debug_hygiene_preflight"]


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
    if isinstance(result.get("diagnostic_launch"), dict):
        bounded["diagnostic_launch"] = compact_value(result["diagnostic_launch"])
    debug_preflight = _bounded_debug_preflight_result(result)
    if debug_preflight is not None:
        bounded["debug_preflight"] = debug_preflight
    return bounded


def _bounded_debug_preflight_result(result: dict[str, Any]) -> dict[str, Any] | None:
    baseline = result.get("baseline")
    if not isinstance(baseline, dict):
        return None
    steps: list[dict[str, Any]] = []
    for raw_step in baseline.get("steps", []):
        if not isinstance(raw_step, dict):
            continue
        if raw_step.get("kind") != "debug_hygiene_preflight":
            continue
        step_result = raw_step.get("result")
        if not isinstance(step_result, dict):
            step_result = {}
        steps.append(
            {
                "id": raw_step.get("id"),
                "kind": "debug_hygiene_preflight",
                "status": raw_step.get("status", step_result.get("status")),
                "cleared": compact_value(step_result.get("cleared", {})),
                "tracepoints_removed": step_result.get("tracepoints_removed", 0),
                "remaining_breakpoints": compact_value(
                    step_result.get("remaining_breakpoints", [])
                ),
                "cleanup_errors": compact_value(step_result.get("cleanup_errors", [])),
            }
        )
    if not steps:
        return None
    return {"status": baseline.get("status"), "steps": compact_value(steps)}


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
            "debug_preflight",
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

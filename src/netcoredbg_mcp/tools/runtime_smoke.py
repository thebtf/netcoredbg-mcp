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

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def register_runtime_smoke_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
    resolve_project_root: Callable[..., Awaitable[Any]],
) -> None:
    """Register runtime smoke composite tools on the MCP server."""
    from mcp.types import ToolAnnotations

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

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def run_runtime_smoke(ctx: Context, plan: dict[str, Any]) -> dict:
        """Run a bounded runtime smoke scenario plan with cleanup evidence."""
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)
            if not isinstance(plan, dict):
                data = {
                    "status": "FAIL",
                    "reason": "invalid plan schema",
                    "validation_errors": ["plan must be an object"],
                    "action_count": 0,
                    "completed_steps": [],
                    "failed_assertions": [],
                    "cleanup": {
                        "status": "PASS",
                        "attempted": [],
                        "failures": [],
                    },
                }
            else:
                data = await RuntimeSmokeRunner(session).run(plan)
            return _build_runtime_smoke_response(
                session,
                data,
                ["verify_debug_freshness", "debug_hygiene_preflight"],
            )
        except Exception as exc:
            return build_error_response(str(exc), state=session.state.state)


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

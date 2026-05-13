"""MCP tools for project-scoped source navigation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from ..code_search import DEFAULT_SEARCH_TIMEOUT_SECONDS, MAX_SEARCH_RESULTS, CodeSearchEngine
from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.state import DebugState

CODE_SEARCH_ACTIONS = [
    "find_code_symbol",
    "find_code_references",
    "get_source_context",
    "search_source",
]


def register_code_search_tools(
    mcp: FastMCP,
    session: SessionManager,
    *,
    resolve_project_root: Callable[..., Awaitable[Path | None]],
) -> None:
    """Register source navigation tools that do not require an active debug session."""

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def find_code_symbol(
        ctx: Context,
        name: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Find a C# symbol definition by name and optional kind."""
        try:
            engine = await _get_engine(ctx, session, resolve_project_root)
            results = engine.find_code_symbol(name, kind=kind)
            return _success(
                session,
                engine,
                {"results": results, "count": len(results)},
            )
        except Exception as exc:
            return _failure(session, exc)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def find_code_references(
        ctx: Context,
        name: str,
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> dict[str, Any]:
        """Find literal symbol references across project files."""
        try:
            engine = await _get_engine(ctx, session, resolve_project_root)
            results = engine.find_code_references(name, max_results=max_results)
            return _success(
                session,
                engine,
                {"results": results, "count": len(results)},
            )
        except Exception as exc:
            return _failure(session, exc)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def get_source_context(
        ctx: Context,
        file: str,
        line: int,
        radius: int = 10,
    ) -> dict[str, Any]:
        """Read source lines around a project-scoped location."""
        try:
            engine = await _get_engine(ctx, session, resolve_project_root)
            context = engine.get_source_context(file, line=line, radius=radius)
            return _success(session, engine, context)
        except Exception as exc:
            return _failure(session, exc)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    async def search_source(
        ctx: Context,
        pattern: str,
        file_glob: str | None = None,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> dict[str, Any]:
        """Run a bounded regex search across project source files."""
        try:
            engine = await _get_engine(ctx, session, resolve_project_root)
            results = engine.search_source(
                pattern,
                file_glob=file_glob,
                timeout_seconds=timeout_seconds,
                max_results=max_results,
            )
            return _success(
                session,
                engine,
                {"results": results, "count": len(results)},
            )
        except Exception as exc:
            return _failure(session, exc)


async def _get_engine(
    ctx: Context,
    session: SessionManager,
    resolve_project_root: Callable[..., Awaitable[Path | None]],
) -> CodeSearchEngine:
    project_root = await resolve_project_root(ctx, session)
    if project_root is None and session.project_path:
        project_root = Path(session.project_path)
    if project_root is None:
        raise RuntimeError(
            "Project root is not configured. Start with --project or --project-from-cwd."
        )
    return CodeSearchEngine(project_root)


def _success(
    session: SessionManager,
    engine: CodeSearchEngine,
    data: dict[str, Any],
) -> dict[str, Any]:
    return build_response(
        data={**data, "project_root": str(engine.project_root)},
        state=_session_state(session),
        next_actions=CODE_SEARCH_ACTIONS,
    )


def _failure(session: SessionManager, exc: Exception) -> dict[str, Any]:
    return build_error_response(
        str(exc),
        state=_session_state(session),
        next_actions=CODE_SEARCH_ACTIONS,
    )


def _session_state(session: SessionManager) -> DebugState | str:
    state = getattr(getattr(session, "state", None), "state", DebugState.IDLE)
    return state

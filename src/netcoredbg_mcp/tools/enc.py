"""MCP tools for Edit-and-Continue code changes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from ..enc.applier import ApplyDeltasResult, apply_deltas
from ..enc.compiler import DeltaResult, SourceEdit, compile_delta
from ..enc.detect import detect_enc_support
from ..response import build_error_response, build_response
from ..session import SessionManager
from ..session.state import DebugState, ModuleInfo

CompilerFn = Callable[..., DeltaResult]
ApplyFn = Callable[..., Awaitable[ApplyDeltasResult]]


def register_enc_tools(
    mcp: FastMCP,
    session: SessionManager,
    *,
    check_session_access: Callable[[Any], str | None],
    notify_state_changed: Callable[[Any], Awaitable[None]],
    resolve_project_root: Callable[..., Awaitable[Path | None]],
) -> None:
    """Register Edit-and-Continue MCP tools."""

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def apply_code_change(
        ctx: Context,
        file: str,
        edits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply source edits to a stopped debug session using Edit-and-Continue."""
        access_error = check_session_access(ctx)
        if access_error:
            return build_error_response(access_error, state=session.state.state)

        project_root = await resolve_project_root(ctx, session)
        if project_root is None and session.project_path:
            project_root = Path(session.project_path)
        if project_root is None:
            return build_error_response(
                "Project root is not configured. Start with --project or --project-from-cwd.",
                state=session.state.state,
            )

        result = await apply_code_change_to_session(session, project_root, file, edits)
        await notify_state_changed(ctx)
        return result


async def apply_code_change_to_session(
    session: Any,
    project_root: str | Path,
    file: str | Path,
    edits: list[dict[str, Any]],
    *,
    compiler: CompilerFn = compile_delta,
    apply: ApplyFn = apply_deltas,
) -> dict[str, Any]:
    """Validate, compile, and apply source edits to a stopped debug session."""
    if session.state.state != DebugState.STOPPED:
        return build_error_response(
            "apply_code_change requires STOPPED state (break mode).",
            state=session.state.state,
        )

    enc_support = detect_enc_support(session.netcoredbg_path)
    if not enc_support["supported"]:
        return build_error_response(
            f"{enc_support['error']} Run `netcoredbg-mcp setup --enc` and restart debugging.",
            state=session.state.state,
        )

    try:
        root = _resolve_project_root(project_root)
        target_file = _resolve_project_file(root, file)
        source_edits = [_parse_edit(edit) for edit in edits]
    except Exception as exc:
        return build_error_response(str(exc), state=session.state.state)

    delta = await asyncio.to_thread(
        compiler,
        project_path=root,
        file_path=target_file,
        edits=source_edits,
    )
    if not delta.success:
        if delta.rude_edits:
            return build_error_response(
                "; ".join(delta.rude_edits)
                + ". Suggestion: restart_debug(rebuild=True).",
                state=session.state.state,
            )
        return build_error_response(
            "; ".join(delta.diagnostics) or "Delta compilation failed.",
            state=session.state.state,
        )

    try:
        module_name = _resolve_target_module(session, target_file)
    except Exception as exc:
        return build_error_response(str(exc), state=session.state.state)

    _apply_source_edits(target_file, source_edits)

    previous_state = session.begin_applying_changes()
    try:
        apply_result = await apply(
            session.client,
            dll_name=module_name,
            metadata_path=_require_delta_path(delta.metadata_delta_path, "metadata"),
            il_path=_require_delta_path(delta.il_delta_path, "il"),
            pdb_path=_require_delta_path(delta.pdb_delta_path, "pdb"),
            line_updates_path=None,
        )
    finally:
        session.finish_applying_changes(previous_state)

    if not apply_result.success:
        return build_error_response(
            apply_result.message or "netcoredbg applyDeltas request failed.",
            state=session.state.state,
        )

    return build_response(
        data={
            "success": True,
            "file": str(target_file),
            "module": module_name,
            "deltas": {
                "metadata_path": delta.metadata_delta_path,
                "il_path": delta.il_delta_path,
                "pdb_path": delta.pdb_delta_path,
            },
            "apply": apply_result.body,
        },
        state=session.state.state,
    )


def _parse_edit(edit: dict[str, Any]) -> SourceEdit:
    return SourceEdit(
        start_line=int(edit["start_line"]),
        end_line=int(edit["end_line"]),
        new_text=str(edit["new_text"]),
    )


def _resolve_project_root(project_root: str | Path) -> Path:
    path = Path(project_root).resolve()
    if path.is_file() and path.suffix.lower() == ".csproj":
        return path.parent
    return path


def _resolve_project_file(project_root: Path, file: str | Path) -> Path:
    path = Path(file)
    resolved = path.resolve() if path.is_absolute() else (project_root / path).resolve()
    relative = resolved.relative_to(project_root)
    if any(part == ".." for part in relative.parts):
        raise ValueError(f"Target file must be inside project root: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Target file not found: {resolved}")
    return resolved


def _resolve_target_module(session: Any, target_file: Path) -> str:
    modules = [
        module
        for module in getattr(session.state, "modules", [])
        if isinstance(module, ModuleInfo) and module.name.lower().endswith(".dll")
    ]
    if not modules:
        raise RuntimeError(
            "No loaded .dll module is known for apply_code_change. "
            "Wait for module events or call get_debug_state before applying changes."
        )

    project_name = _project_name_for_file(target_file)
    if project_name is not None:
        expected_name = f"{project_name}.dll".lower()
        for module in modules:
            if module.name.lower() == expected_name:
                return module.path or module.name

    if len(modules) == 1:
        module = modules[0]
        return module.path or module.name

    names = ", ".join(module.name for module in modules)
    raise RuntimeError(f"Could not choose target module for {target_file}; loaded modules: {names}")


def _project_name_for_file(target_file: Path) -> str | None:
    for parent in [target_file.parent, *target_file.parents]:
        project_files = sorted(parent.glob("*.csproj"))
        if project_files:
            return project_files[0].stem
    return None


def _apply_source_edits(target_file: Path, edits: list[SourceEdit]) -> None:
    lines = target_file.read_text(encoding="utf-8").splitlines()
    for edit in sorted(edits, key=lambda item: item.start_line, reverse=True):
        replacement = edit.new_text.rstrip("\r\n").splitlines()
        lines[edit.start_line - 1 : edit.end_line] = replacement
    target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _require_delta_path(value: str | None, kind: str) -> str:
    if value is None:
        raise RuntimeError(f"Delta compiler did not return {kind} delta path.")
    return value

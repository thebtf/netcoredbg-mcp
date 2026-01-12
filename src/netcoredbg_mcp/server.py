"""MCP Server for netcoredbg debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import Context, FastMCP

from .session import SessionManager
from .utils.project import get_project_root

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Global session manager (single client mode)
_session: SessionManager | None = None
_initial_project_path: str | None = None


def get_session() -> SessionManager:
    """Get or create session manager.

    Note: Single client mode - only one debug session supported at a time.
    """
    global _session
    if _session is None:
        netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
        _session = SessionManager(netcoredbg_path, _initial_project_path)
    return _session


async def resolve_project_root(ctx: Context, session: SessionManager) -> Path | None:
    """Resolve the current project root, potentially updating session.

    Uses MCP roots from client if available, otherwise falls back to
    configured project path.

    Args:
        ctx: MCP Context for accessing client roots
        session: Session manager to update if project root changes

    Returns:
        Current project root path
    """
    # Try to get project root from MCP context (includes client roots)
    project_root = await get_project_root(ctx)

    if project_root:
        # Update session's project path if it differs
        current = session.project_path
        new_path = str(project_root)
        if current != new_path:
            logger.info(f"Updating project root: {current} -> {new_path}")
            session.set_project_path(new_path)

    return project_root


def create_server(project_path: str | None = None) -> FastMCP:
    """Create and configure the MCP server.

    Args:
        project_path: Initial root path for the project being debugged.
            All file operations will be constrained to this path.
            Can be dynamically updated from MCP client roots.
    """
    global _initial_project_path
    _initial_project_path = project_path
    mcp = FastMCP("netcoredbg-mcp")
    session = get_session()

    # Helper to notify resource updates (MCP spec compliance)
    from pydantic import AnyUrl

    async def notify_state_changed(ctx: Context) -> None:
        """Notify client that debug://state resource has changed."""
        try:
            if ctx.session:
                await ctx.session.send_resource_updated(AnyUrl("debug://state"))
        except Exception:
            pass  # Notification failure shouldn't break the tool

    async def notify_breakpoints_changed(ctx: Context) -> None:
        """Notify client that debug://breakpoints resource has changed."""
        try:
            if ctx.session:
                await ctx.session.send_resource_updated(AnyUrl("debug://breakpoints"))
        except Exception:
            pass

    # ============== Debug Control Tools ==============

    @mcp.tool()
    async def start_debug(
        ctx: Context,
        program: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stop_at_entry: bool = False,
        pre_build: bool = True,
        build_project: str | None = None,
        build_configuration: str = "Debug",
    ) -> dict:
        """
        Start debugging a .NET program. RECOMMENDED for most debugging scenarios.

        This is the preferred method for debugging .NET applications. It launches
        a new process under the debugger with full feature support including:
        - Complete call stack visibility
        - Full variable inspection
        - All breakpoint features

        SMART RESOLUTION: For .NET 6+ apps (WPF/WinForms), automatically resolves
        .exe to .dll to avoid "deps.json conflict" errors. You can pass either
        App.exe or App.dll - the correct target will be selected automatically.

        PRE-BUILD: By default, builds the project before launching to ensure you're
        debugging the latest code. Provide build_project path to .csproj file.
        Set pre_build=False to skip building (e.g., for pre-built binaries).

        Use attach_debug only for already-running processes (e.g., ASP.NET services).

        Args:
            program: Path to the .NET executable or DLL to debug (auto-resolved)
            cwd: Working directory for the program
            args: Command line arguments
            env: Environment variables
            stop_at_entry: Stop at entry point
            pre_build: Build project before launching (default: True). Requires build_project.
            build_project: Path to .csproj file (required when pre_build=True)
            build_configuration: Build configuration (Debug/Release)
        """
        try:
            # Resolve project root from MCP context (may update session)
            await resolve_project_root(ctx, session)

            # Validate pre_build requires build_project
            if pre_build and not build_project:
                return {
                    "success": False,
                    "error": "pre_build=True requires build_project path to .csproj file. "
                    "Either provide build_project or set pre_build=False.",
                }

            # Validate program path (security: prevent arbitrary execution)
            # If pre_build=True, don't require file to exist yet (build will create it)
            validated_program = session.validate_program(program, must_exist=not pre_build)

            # Validate cwd if provided (for pre_build, cwd may not exist yet either)
            validated_cwd = cwd
            if cwd:
                validated_cwd = session.validate_path(cwd, must_exist=not pre_build)

            # Validate build_project if provided (must exist for build to work)
            validated_build_project = None
            if build_project:
                validated_build_project = session.validate_path(build_project, must_exist=True)

            # Progress callback to report to MCP client
            async def report_progress(progress: float, total: float, message: str) -> None:
                await ctx.report_progress(progress=progress, total=total, message=message)

            result = await session.launch(
                program=validated_program,
                cwd=validated_cwd,
                args=args,
                env=env,
                stop_at_entry=stop_at_entry,
                pre_build=pre_build,
                build_project=validated_build_project,
                build_configuration=build_configuration,
                progress_callback=report_progress,
            )
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def attach_debug(process_id: int) -> dict:
        """
        AVOID - Use start_debug instead. Attach to already-running process (LIMITED).

        LIMITATION: netcoredbg does NOT support justMyCode in attach mode (only in launch).
        This is an UPSTREAM limitation that CANNOT be fixed by this MCP server.
        Result: stack traces will be incomplete/empty, debugging will be unreliable.

        ONLY use this if you MUST debug an already-running process that you
        cannot restart (e.g., production service, container you cannot control).

        For normal debugging, ALWAYS use start_debug which has full functionality.
        If start_debug fails with build errors, fix the build - don't switch to attach.

        Args:
            process_id: PID of an already-running .NET process (NOT for normal debugging)
        """
        try:
            result = await session.attach(process_id)
            return {
                "success": True,
                "data": result,
                "warning": (
                    "ATTACH MODE HAS LIMITED FUNCTIONALITY. "
                    "Stack traces may be incomplete due to netcoredbg limitation. "
                    "For reliable debugging, use start_debug instead."
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def stop_debug(ctx: Context) -> dict:
        """Stop the current debug session."""
        try:
            result = await session.stop()
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def continue_execution(ctx: Context, thread_id: int | None = None) -> dict:
        """Continue program execution."""
        try:
            result = await session.continue_execution(thread_id)
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def pause_execution(ctx: Context, thread_id: int | None = None) -> dict:
        """Pause program execution."""
        try:
            result = await session.pause(thread_id)
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_over(ctx: Context, thread_id: int | None = None) -> dict:
        """Step over to the next line."""
        try:
            result = await session.step_over(thread_id)
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_into(ctx: Context, thread_id: int | None = None) -> dict:
        """Step into the next function call."""
        try:
            result = await session.step_in(thread_id)
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def step_out(ctx: Context, thread_id: int | None = None) -> dict:
        """Step out of the current function."""
        try:
            result = await session.step_out(thread_id)
            await notify_state_changed(ctx)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_debug_state() -> dict:
        """
        Get the current debug session state.

        Returns state, threads, current position, and exception info.
        The user cannot see this directly - summarize important info for them.

        IMPORTANT: Always check state before asking user to interact with the app GUI!
        If the app is paused at a breakpoint, the user cannot interact with UI.
        Call continue_execution first if state shows stopped/paused.
        """
        try:
            return {"success": True, "data": session.state.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Breakpoint Tools ==============

    @mcp.tool()
    async def add_breakpoint(
        ctx: Context,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> dict:
        """
        Add a breakpoint at a specific line.

        IMPORTANT TIMING:
        - Breakpoints set BEFORE start_debug only work for debugging app startup.
        - For UI apps (WPF/WinForms): remove breakpoints before launch, then add them
          AFTER the UI is fully loaded. Otherwise the app may hang during initialization.
        - When debugging UI issues: wait for app to be fully interactive before setting
          breakpoints in event handlers.

        Args:
            file: Absolute path to source file
            line: Line number (1-based)
            condition: Optional condition expression
            hit_condition: Optional hit count condition
        """
        try:
            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file, must_exist=True)
            bp = await session.add_breakpoint(validated_file, line, condition, hit_condition)
            await notify_breakpoints_changed(ctx)
            return {
                "success": True,
                "data": {
                    "file": bp.file,
                    "line": bp.line,
                    "condition": bp.condition,
                    "verified": bp.verified,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def remove_breakpoint(ctx: Context, file: str, line: int) -> dict:
        """Remove a breakpoint from a specific line."""
        try:
            # Resolve project root from MCP context
            await resolve_project_root(ctx, session)

            # Validate file path (security: prevent path traversal)
            validated_file = session.validate_path(file)
            removed = await session.remove_breakpoint(validated_file, line)
            await notify_breakpoints_changed(ctx)
            return {"success": True, "data": {"removed": removed}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def list_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """List all breakpoints or breakpoints in a specific file."""
        try:
            if file:
                # Resolve project root from MCP context
                await resolve_project_root(ctx, session)
                # Validate file path if provided
                validated_file = session.validate_path(file)
                bps = session.breakpoints.get_for_file(validated_file)
                result = {
                    validated_file: [
                        {"line": bp.line, "condition": bp.condition, "verified": bp.verified}
                        for bp in bps
                    ]
                }
            else:
                all_bps = session.breakpoints.get_all()
                result = {
                    f: [
                        {"line": bp.line, "condition": bp.condition, "verified": bp.verified}
                        for bp in bps
                    ]
                    for f, bps in all_bps.items()
                }
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def clear_breakpoints(ctx: Context, file: str | None = None) -> dict:
        """Clear breakpoints from a file or all files."""
        try:
            validated_file = None
            if file:
                # Resolve project root from MCP context
                await resolve_project_root(ctx, session)
                # Validate file path if provided
                validated_file = session.validate_path(file)
            count = await session.clear_breakpoints(validated_file)
            await notify_breakpoints_changed(ctx)
            return {"success": True, "data": {"removed": count}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Inspection Tools ==============

    @mcp.tool()
    async def get_threads() -> dict:
        """Get all threads in the debugged process."""
        try:
            threads = await session.get_threads()
            return {"success": True, "data": [{"id": t.id, "name": t.name} for t in threads]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_call_stack(thread_id: int | None = None, levels: int = 20) -> dict:
        """Get the call stack for a thread.

        Diagnostic: Set NETCOREDBG_STACKTRACE_DELAY_MS env var to add delay before
        stackTrace request. This helps diagnose timing issues with ICorDebugThread3.
        Example: NETCOREDBG_STACKTRACE_DELAY_MS=300
        """
        try:
            # Diagnostic test: configurable delay before stackTrace
            # If delay helps, root cause is timing (CLR not ready)
            # If delay doesn't help, root cause is binary mismatch
            delay_ms = int(os.environ.get("NETCOREDBG_STACKTRACE_DELAY_MS", "0"))
            if delay_ms > 0:
                logger.info(f"[DIAGNOSTIC] Applying {delay_ms}ms delay before stackTrace request")
                await asyncio.sleep(delay_ms / 1000.0)

            frames = await session.get_stack_trace(thread_id, 0, levels)
            return {
                "success": True,
                "data": [
                    {
                        "id": f.id, "name": f.name, "source": f.source,
                        "line": f.line, "column": f.column,
                    }
                    for f in frames
                ],
            }
        except Exception as e:
            error_msg = str(e)
            # Enhanced error message for E_NOINTERFACE
            if "0x80004002" in error_msg or "E_NOINTERFACE" in error_msg.upper():
                logger.warning(
                    "[DIAGNOSTIC] E_NOINTERFACE on ICorDebugThread3. "
                    "Try setting NETCOREDBG_STACKTRACE_DELAY_MS=300 to test timing hypothesis."
                )
            return {"success": False, "error": error_msg}

    @mcp.tool()
    async def get_scopes(frame_id: int | None = None) -> dict:
        """Get variable scopes for a stack frame."""
        try:
            scopes = await session.get_scopes(frame_id)
            return {
                "success": True,
                "data": [
                    {
                        "name": s.get("name", ""),
                        "variablesReference": s.get("variablesReference", 0),
                    }
                    for s in scopes
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_variables(variables_reference: int) -> dict:
        """Get variables for a scope or structured variable."""
        try:
            variables = await session.get_variables(variables_reference)
            return {
                "success": True,
                "data": [
                    {
                        "name": v.name,
                        "value": v.value,
                        "type": v.type,
                        "variablesReference": v.variables_reference,
                    }
                    for v in variables
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def evaluate_expression(expression: str, frame_id: int | None = None) -> dict:
        """Evaluate an expression in the current debug context."""
        try:
            result = await session.evaluate(expression, frame_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_exception_info(thread_id: int | None = None) -> dict:
        """Get information about the current exception."""
        try:
            info = await session.get_exception_info(thread_id)
            return {"success": True, "data": info}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_output(clear: bool = False) -> dict:
        """Get stdout/stderr output from the debugged program.

        IMPORTANT: The user cannot see this output directly.
        YOU must read it and summarize relevant information for the user.
        Never tell the user to "check the console" or "look at output".

        Call periodically during debugging to catch log messages and errors.
        """
        try:
            output = "".join(session.state.output_buffer)
            if clear:
                session.state.output_buffer.clear()
            return {"success": True, "data": {"output": output}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def search_output(pattern: str, context_lines: int = 2) -> dict:
        """Search program output for a pattern (regex supported).

        Use this instead of get_output when looking for specific messages,
        errors, or log entries in large output. Returns matching lines with context.

        Args:
            pattern: Regex pattern to search for (case-insensitive)
            context_lines: Number of lines before/after each match (default 2)

        Returns:
            List of matches with line numbers and context
        """
        import re

        try:
            output = "".join(session.state.output_buffer)
            lines = output.splitlines()
            matches = []

            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return {"success": False, "error": f"Invalid regex: {e}"}

            for i, line in enumerate(lines):
                if regex.search(line):
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    context = lines[start:end]
                    matches.append({
                        "line_number": i + 1,
                        "match": line,
                        "context": context,
                    })

            return {
                "success": True,
                "data": {
                    "pattern": pattern,
                    "match_count": len(matches),
                    "matches": matches[:50],  # Limit to 50 matches
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def get_output_tail(lines: int = 50) -> dict:
        """Get the last N lines of program output.

        Useful for checking recent output without loading everything.
        The user cannot see this - summarize relevant info for them.

        Args:
            lines: Number of lines to return (default 50)
        """
        try:
            output = "".join(session.state.output_buffer)
            all_lines = output.splitlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {
                "success": True,
                "data": {
                    "total_lines": len(all_lines),
                    "returned_lines": len(tail),
                    "output": "\n".join(tail),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== UI Automation Tools ==============

    # Global UI automation instance
    from typing import Any

    _ui: Any = None

    def _get_ui() -> Any:
        """Get or create UI automation instance."""
        nonlocal _ui
        if _ui is None:
            from .ui import UIAutomation

            _ui = UIAutomation()
        return _ui

    async def _ensure_ui_connected(session: SessionManager) -> Any:
        """Ensure UI automation is connected to the debug process.

        Raises:
            NoActiveSessionError: If no debug session is active
            NoProcessIdError: If process ID not available
        """
        from .ui import NoActiveSessionError, NoProcessIdError

        if session.state.state.value == "idle":
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        ui = _get_ui()
        if ui._process_id != process_id:
            await ui.connect(process_id)
        return ui

    async def _find_ui_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ):
        """Helper to connect to UI and find an element."""
        ui = await _ensure_ui_connected(session)
        element = await ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
        )
        return ui, element

    @mcp.tool()
    async def ui_get_window_tree(max_depth: int = 3, max_children: int = 50) -> dict:
        """
        Get the visual tree of the debugged application's main window.

        Use this to understand the UI structure before interacting with elements.
        Call after start_debug and wait for the application window to appear.

        Args:
            max_depth: Maximum depth to traverse (default 3)
            max_children: Maximum children per element (default 50)

        Returns:
            Visual tree with automationId, controlType, name, isEnabled, etc.
        """
        try:
            ui = await _ensure_ui_connected(session)
            tree = await ui.get_window_tree(max_depth, max_children)
            return {"success": True, "data": tree.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def ui_find_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Find a UI element by AutomationId, name, or control type.

        At least one search criterion must be provided.
        Use ui_get_window_tree first to discover available elements.

        Args:
            automation_id: AutomationId property (most reliable for WPF)
            name: Element's Name/Title property
            control_type: Type like "Button", "TextBox", "MenuItem"

        Returns:
            Element info if found
        """
        try:
            ui = await _ensure_ui_connected(session)
            element = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
            )
            info = await ui.get_element_info(element)
            return {"success": True, "data": info.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def ui_set_focus(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Set keyboard focus to a UI element.

        Call this before ui_send_keys to ensure keys go to the right element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.set_focus(element)
            return {"success": True, "data": {"focused": True}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def ui_send_keys(
        keys: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Send keyboard input to a UI element.

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}"
        - Tab: "{TAB}"
        - Escape: "{ESC}"
        - Ctrl+C: "^c"
        - Alt+F4: "%{F4}"
        - Shift+Tab: "+{TAB}"
        - Ctrl+Shift+S: "^+s"

        Args:
            keys: Keys to send (pywinauto syntax)
            automation_id: Target element's AutomationId
            name: Target element's Name
            control_type: Target element's control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.send_keys(element, keys)
            return {"success": True, "data": {"sent": keys}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def ui_send_keys_focused(keys: str) -> dict:
        """
        Send keyboard input to the currently focused element.

        Use this AFTER ui_set_focus to avoid re-searching for complex elements
        like DataGrid that may timeout on repeated searches.

        Workflow:
        1. ui_set_focus(automation_id="MyElement")  # Focus the element
        2. ui_send_keys_focused(keys="^{END}")      # Send keys without re-search

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}", Tab: "{TAB}", Escape: "{ESC}"
        - Ctrl+C: "^c", Alt+F4: "%{F4}", Shift+Tab: "+{TAB}"
        - Arrow keys: "{LEFT}", "{RIGHT}", "{UP}", "{DOWN}"
        - Ctrl+End: "^{END}", Ctrl+Home: "^{HOME}"

        Args:
            keys: Keys to send (pywinauto syntax)
        """
        try:
            ui = await _get_ui()
            await ui.send_keys_focused(keys)
            return {"success": True, "data": {"sent": keys, "target": "focused"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def ui_click(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Click on a UI element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.click(element)
            return {"success": True, "data": {"clicked": True}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============== Prompts (slash commands) ==============

    @mcp.prompt(
        name="debug",
        description="Debug session workflow guide for .NET applications",
    )
    def debug_prompt() -> list[dict]:
        """Start here when debugging .NET applications."""
        return [
            {
                "role": "user",
                "content": """# .NET Debug Session Guide

## CRITICAL: User Cannot See Debug Output

The user does NOT have access to Debug/Output windows.
YOU must:
1. Call `get_output()` periodically to read program output
2. Summarize important findings for the user
3. NEVER tell user to "look at Debug Output" or "check the console"
4. Report errors, exceptions, and relevant log messages proactively

## Debug Workflow

### 1. Start Session
```
start_debug(
    program="path/to/App.dll",      # Use .dll, not .exe for .NET 6+
    build_project="path/to/App.csproj",  # Optional: rebuild before debug
    pre_build=True                   # Default: builds before launch
)
```

### 2. Set Breakpoints
```
add_breakpoint(file="Program.cs", line=15)
add_breakpoint(file="Handler.cs", line=42, condition="x > 10")
```

### 3. When Execution Pauses
```
get_call_stack()           # See where execution stopped
get_scopes(frame_id)       # Get variable scopes for frame
get_variables(reference)   # Inspect variable values
evaluate_expression("x+1") # Evaluate expressions
```

### 4. Control Execution
```
step_over()           # Next line (skip function calls)
step_into()           # Enter function
step_out()            # Exit current function
continue_execution()  # Run until next breakpoint
```

### 5. Monitor Output
```
get_output()          # Read program stdout/stderr - SUMMARIZE FOR USER
get_debug_state()     # Check session state
```

### 6. End Session
```
stop_debug()
```

## Common Issues
- Empty stack trace? Use `start_debug`, not `attach_debug`
- deps.json conflict? Build uses .dll, not .exe
- E_NOINTERFACE? dbgshim.dll version mismatch with .NET runtime

## For UI Applications (WPF/WinForms)

After window appears, you can automate UI interaction:
```
ui_get_window_tree()                    # Discover UI structure
ui_find_element(automation_id="btn")    # Find element
ui_click(automation_id="btnSave")       # Click button
```

### Sending Keys to Complex Controls (DataGrid, TreeView, etc.)

For complex controls that timeout on repeated searches, use this workflow:
```
ui_set_focus(automation_id="MyDataGrid")  # 1. Focus the element
ui_send_keys_focused(keys="^{END}")       # 2. Send keys without re-search
ui_send_keys_focused(keys="{DOWN}")       # 3. Continue sending keys
```

For simple controls, direct send works:
```
ui_send_keys(keys="hello", automation_id="txtInput")
```

Keyboard syntax: `{ENTER}`, `{TAB}`, `^c` (Ctrl+C), `%{F4}` (Alt+F4)
""",
            }
        ]

    @mcp.prompt(
        name="exception",
        description="Guide for investigating exceptions during debugging",
    )
    def exception_prompt() -> list[dict]:
        """Steps to investigate an exception."""
        return [
            {
                "role": "user",
                "content": "The debugger stopped on an exception.",
            },
            {
                "role": "assistant",
                "content": "I'll investigate the exception. Let me gather the details.",
            },
            {
                "role": "user",
                "content": """## Exception Investigation Steps

Execute these in order:

### 1. Get Exception Details
```
get_debug_state()  # Check exceptionInfo field
```

### 2. Get Stack Trace
```
get_call_stack()   # See where exception occurred
```

### 3. Inspect Local State
```
get_scopes(frame_id)           # Get scopes for the frame
get_variables(scope_reference)  # See local variables
```

### 4. Read Recent Output
```
get_output()  # Check for error messages before exception
```

### 5. Report to User
Summarize:
- Exception type and message
- Where it occurred (file, line, method)
- Likely cause based on local state
- Suggested fix

### 6. Decision
- `continue_execution()` to ignore and continue
- `stop_debug()` to end session and fix code
""",
            },
        ]

    # ============== Resources ==============

    @mcp.resource("debug://state", mime_type="application/json")
    async def debug_state_resource() -> str:
        """Current debug session state (JSON).

        Contains: status, stop_reason, threads, process info.
        Updates when: session starts/stops, breakpoint hit, step completes.
        """
        return json.dumps(session.state.to_dict(), indent=2)

    @mcp.resource("debug://breakpoints", mime_type="application/json")
    async def debug_breakpoints_resource() -> str:
        """All active breakpoints (JSON).

        Contains: file paths with line numbers, conditions, verified status.
        Updates when: breakpoints added/removed/verified.
        """
        all_bps = session.breakpoints.get_all()
        result = {
            f: [{"line": bp.line, "condition": bp.condition, "verified": bp.verified} for bp in bps]
            for f, bps in all_bps.items()
        }
        return json.dumps(result, indent=2)

    @mcp.resource("debug://output", mime_type="text/plain")
    async def debug_output_resource() -> str:
        """Debug console output (plain text).

        Contains: stdout/stderr from debugged process.
        Updates when: new output arrives.
        """
        return "".join(session.state.output_buffer)

    logger.info("NetCoreDbg MCP Server initialized")
    return mcp

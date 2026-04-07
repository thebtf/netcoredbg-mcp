"""Debug session manager - orchestrates DAP client and state."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from ..build import BuildManager, BuildResult
from ..dap import DAPClient, DAPEvent
from ..dap.events import (
    BreakpointEventBody,
    ModuleEventBody,
    OutputEventBody,
    StoppedEventBody,
)
from ..dap.protocol import Events
from ..process_registry import ProcessRegistry
from ..ui.temp_manager import SessionTempManager
from ..utils.version import check_version_compatibility
from .state import (
    Breakpoint,
    BreakpointRegistry,
    DebugState,
    FunctionBreakpoint,
    ModuleInfo,
    OutputEntry,
    SessionState,
    StackFrame,
    StoppedSnapshot,
    ThreadInfo,
    Variable,
)

logger = logging.getLogger(__name__)

# Output buffer limits (security: prevent DoS). Configurable via env vars.
MAX_OUTPUT_BYTES = int(os.environ.get("NETCOREDBG_MAX_OUTPUT_BYTES", "10000000"))  # 10MB default
MAX_OUTPUT_ENTRY = int(os.environ.get("NETCOREDBG_MAX_OUTPUT_ENTRY", "100000"))  # 100KB default


class SessionManager:
    """Manages debug session lifecycle and state."""

    def __init__(self, netcoredbg_path: str | None = None, project_path: str | None = None):
        self._client = DAPClient(netcoredbg_path)
        self._state = SessionState()
        self._breakpoints = BreakpointRegistry()
        self._state_listeners: list[Callable[[DebugState], None]] = []
        self._initialized_event = asyncio.Event()
        self._execution_event = asyncio.Event()  # Signaled on stopped/terminated/exited
        self._project_path = os.path.abspath(project_path) if project_path else None
        self._output_bytes = 0  # Track output buffer size
        self._process_registry = ProcessRegistry()
        self._temp_manager = SessionTempManager()
        self._build_manager = BuildManager()
        self._last_build_result: BuildResult | None = None
        self._last_launch_config: dict[str, Any] | None = None  # For restart
        self._last_version_warning: str | None = None  # dbgshim version mismatch warning
        self._session_id: str | None = None
        self._quick_eval_lock = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        """Get current session state."""
        return self._state

    @property
    def client(self) -> DAPClient:
        """Access the underlying DAP client."""
        return self._client

    @property
    def breakpoints(self) -> BreakpointRegistry:
        """Get breakpoint registry."""
        return self._breakpoints

    @property
    def process_registry(self) -> ProcessRegistry:
        """Get process registry for tracking spawned processes."""
        return self._process_registry

    @property
    def temp_manager(self) -> SessionTempManager:
        """Get session temp file manager."""
        return self._temp_manager

    @property
    def session_id(self) -> str | None:
        """Get current session identifier for temp dir isolation."""
        return self._session_id

    @property
    def is_active(self) -> bool:
        """Check if session is active."""
        return self._state.state not in (DebugState.IDLE, DebugState.TERMINATED)

    @property
    def project_path(self) -> str | None:
        """Get project path scope."""
        return self._project_path

    def set_project_path(self, project_path: str | None) -> None:
        """Set project path scope dynamically.

        This allows updating the project path after initialization,
        e.g., when MCP client provides roots.

        Args:
            project_path: New project path, or None to disable scope checking
        """
        self._project_path = os.path.abspath(project_path) if project_path else None
        logger.debug(f"Project path updated to: {self._project_path}")

    @property
    def last_build_result(self) -> BuildResult | None:
        """Get last build result."""
        return self._last_build_result

    @property
    def netcoredbg_path(self) -> str:
        """Get netcoredbg executable path."""
        return self._client.netcoredbg_path

    @property
    def last_version_warning(self) -> str | None:
        """Get last dbgshim version mismatch warning."""
        return self._last_version_warning

    def check_dbgshim_compatibility(self, program: str) -> str | None:
        """Check if dbgshim.dll version is compatible with target runtime.

        Args:
            program: Path to the program being debugged

        Returns:
            Warning message if versions don't match, None if compatible
        """
        try:
            result = check_version_compatibility(program, self.netcoredbg_path)
            if not result.compatible and result.warning:
                logger.warning(f"[VERSION MISMATCH] {result.warning}")
                self._last_version_warning = result.warning
                return result.warning
            elif result.target_version and result.dbgshim_version:
                logger.info(
                    f"Version check: target .NET {result.target_version.major}, "
                    f"dbgshim v{result.dbgshim_version}"
                )
            self._last_version_warning = None
            return None
        except Exception as e:
            logger.debug(f"Version compatibility check failed: {e}")
            return None

    def validate_path(self, path: str, must_exist: bool = False) -> str:
        """Validate path is within project scope.

        Accepts paths within:
        1. The project root directory
        2. Git worktrees of the same repository (auto-detected)
        3. Paths listed in NETCOREDBG_ALLOWED_PATHS env var (comma-separated)

        Args:
            path: Path to validate
            must_exist: If True, path must exist on filesystem

        Returns:
            Absolute path

        Raises:
            ValueError: If path is invalid or outside all allowed scopes
        """
        # Resolve symlinks and normalize to absolute (security: prevent symlink traversal)
        abs_path = os.path.realpath(path)

        # Check within project scope
        if self._project_path:
            project_real = os.path.realpath(self._project_path)

            # Check 1: within project root
            if self._is_path_within(abs_path, project_real):
                pass  # OK
            # Check 2: within git worktrees
            elif any(self._is_path_within(abs_path, wt) for wt in self._get_worktree_paths()):
                pass  # OK
            # Check 3: within NETCOREDBG_ALLOWED_PATHS
            elif any(self._is_path_within(abs_path, ap) for ap in self._get_env_allowed_paths()):
                pass  # OK
            else:
                raise ValueError(
                    f"Path outside project scope: {path}. "
                    f"Set NETCOREDBG_ALLOWED_PATHS env var to add allowed path prefixes."
                )

        # Check existence if required
        if must_exist and not os.path.exists(abs_path):
            raise ValueError(f"Path does not exist: {path}")

        return abs_path

    @staticmethod
    def _is_path_within(path: str, root: str) -> bool:
        """Check if path is within root directory."""
        try:
            common = os.path.commonpath([path, root])
            return common == root
        except ValueError:
            return False

    def _get_worktree_paths(self) -> list[str]:
        """Auto-detect git worktree paths (cached)."""
        if not hasattr(self, '_worktree_cache'):
            self._worktree_cache: list[str] = []
            if self._project_path:
                import subprocess
                try:
                    result = subprocess.run(
                        ["git", "worktree", "list", "--porcelain"],
                        capture_output=True, text=True, timeout=5,
                        cwd=self._project_path,
                    )
                    if result.returncode == 0:
                        current_path = None
                        prunable = False
                        for line in [*result.stdout.splitlines(), ""]:
                            if line.startswith("worktree "):
                                current_path = line[len("worktree "):].strip()
                                prunable = False
                            elif line.startswith("prunable "):
                                prunable = True
                            elif not line and current_path:
                                abs_wt = os.path.abspath(current_path)
                                if not prunable and os.path.isdir(abs_wt):
                                    self._worktree_cache.append(abs_wt)
                                current_path = None
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                    pass
        return self._worktree_cache

    @staticmethod
    def _get_env_allowed_paths() -> list[str]:
        """Get additional allowed paths from NETCOREDBG_ALLOWED_PATHS env var."""
        raw = os.environ.get("NETCOREDBG_ALLOWED_PATHS", "")
        if not raw:
            return []
        return [os.path.abspath(p.strip()) for p in raw.split(",") if p.strip()]

    def validate_program(self, program: str, must_exist: bool = True) -> str:
        """Validate program is a .NET assembly within scope.

        For .NET 6+ apps (WPF/WinForms), automatically resolves .exe to .dll
        to avoid assembly name conflicts. .NET 6+ creates both:
        - App.exe (native host/launcher)
        - App.dll (managed assembly with actual code)

        Debugging the .exe causes "deps.json conflict" errors because the runtime
        finds both assemblies with the same name. The .dll is the correct target.

        Args:
            program: Path to program (.dll or .exe)
            must_exist: If True (default), raises if file doesn't exist.
                        Set to False when pre_build will create the file.

        Returns:
            Absolute path to program (resolved to .dll if applicable)

        Raises:
            ValueError: If program is invalid or outside project scope
        """
        path = self.validate_path(program, must_exist=must_exist)
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".dll", ".exe"):
            raise ValueError(f"Program must be .NET assembly (.dll/.exe): {program}")

        # Smart resolution: .exe → .dll for .NET 6+ apps
        # Only resolve if files exist (skip for pre_build case where must_exist=False)
        if ext == ".exe" and must_exist:
            dll_path = os.path.splitext(path)[0] + ".dll"
            if os.path.isfile(dll_path):
                # Check for .NET 6+ markers (runtimeconfig.json indicates SDK-style project)
                runtimeconfig = os.path.splitext(path)[0] + ".runtimeconfig.json"
                if os.path.isfile(runtimeconfig):
                    logger.info(
                        f"Resolved .exe to .dll for .NET 6+ debugging: "
                        f"{os.path.basename(path)} → {os.path.basename(dll_path)}"
                    )
                    return dll_path

        return path

    def on_state_change(self, listener: Callable[[DebugState], None]) -> None:
        """Register state change listener."""
        self._state_listeners.append(listener)

    def _set_state(self, new_state: DebugState) -> None:
        """Update state and notify listeners."""
        old_state = self._state.state
        self._state.state = new_state
        if old_state != new_state:
            logger.info(f"State changed: {old_state.value} -> {new_state.value}")
            for listener in self._state_listeners:
                try:
                    listener(new_state)
                except Exception:
                    logger.exception("State listener error")

    async def start(self) -> None:
        """Start DAP client and initialize session."""
        if self._client.is_running:
            return

        await self._client.start()

        # Track netcoredbg process
        if self._client._process and self._client._process.pid:
            self._process_registry.register(
                pid=self._client._process.pid,
                role="netcoredbg",
            )

        self._register_event_handlers()
        self._set_state(DebugState.INITIALIZING)

        # Initialize DAP
        await self._client.initialize()
        logger.info("DAP initialized, waiting for initialized event...")

    def _register_event_handlers(self) -> None:
        """Register DAP event handlers."""
        self._client.on_event(Events.INITIALIZED, self._on_initialized)
        self._client.on_event(Events.STOPPED, self._on_stopped)
        self._client.on_event(Events.CONTINUED, self._on_continued)
        self._client.on_event(Events.TERMINATED, self._on_terminated)
        self._client.on_event(Events.EXITED, self._on_exited)
        self._client.on_event(Events.OUTPUT, self._on_output)
        self._client.on_event(Events.THREAD, self._on_thread)
        self._client.on_event(Events.PROCESS, self._on_process)
        self._client.on_event(Events.BREAKPOINT, self._on_breakpoint)
        self._client.on_event(Events.MODULE, self._on_module)

    def _on_initialized(self, event: DAPEvent) -> None:
        """Handle initialized event."""
        logger.info("DAP adapter initialized")
        self._initialized_event.set()

    def _on_stopped(self, event: DAPEvent) -> None:
        """Handle stopped event."""
        body = StoppedEventBody.from_dict(event.body)
        self._state.current_thread_id = body.thread_id
        self._state.stop_reason = body.reason.value
        self._state.stop_description = body.description
        self._state.stop_text = body.text
        logger.info(f"Stopped: reason={body.reason.value}, thread={body.thread_id}")

        # For breakpoint stops with an active tracepoint manager, defer the STOPPED
        # state notification until after we confirm it is not a tracepoint hit.
        # Tracepoint hits are transparent to callers — they resume automatically and
        # must NOT surface as STOPPED events.
        if (
            body.reason.value == "breakpoint"
            and body.thread_id is not None
            and getattr(self, '_tracepoint_manager', None) is not None
        ):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._update_hit_count(body.thread_id))
                loop.create_task(self._check_tracepoint(body.thread_id))
            except RuntimeError:
                # No running event loop (test environment) — fall through to normal stop.
                self._set_state(DebugState.STOPPED)
                self._execution_event.set()
        else:
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
            # Still schedule hit counting for non-tracepoint breakpoint stops
            if body.reason.value == "breakpoint" and body.thread_id is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._update_hit_count(body.thread_id))
                except RuntimeError:
                    pass

    async def _check_tracepoint(self, thread_id: int) -> None:
        """Check if the stopped location matches a tracepoint and handle it.

        When a tracepoint is found, evaluates the expression and resumes execution
        WITHOUT surfacing a STOPPED state to callers (transparent non-stopping hit).
        When no tracepoint matches, transitions to STOPPED and signals _execution_event
        so that callers waiting on wait_for_stopped() are unblocked.

        Entire operation is wrapped in a 5s timeout to prevent event loop starvation
        if DAP requests hang (e.g., debugger unresponsive).
        """
        try:
            await asyncio.wait_for(self._check_tracepoint_inner(thread_id), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("_check_tracepoint timed out after 5s — falling back to STOPPED")
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
        except Exception as e:
            logger.warning("Tracepoint check failed: %s", e)
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()

    async def _check_tracepoint_inner(self, thread_id: int) -> None:
        """Inner tracepoint check logic (called with timeout wrapper)."""
        mgr = getattr(self, '_tracepoint_manager', None)
        if mgr is None:
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
            return

        frames = await self.get_stack_trace(thread_id=thread_id, levels=1)
        if not frames:
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
            return

        top = frames[0]
        logger.debug("_check_tracepoint: top frame source=%s line=%s", top.source, top.line)
        if not top.source or not top.line:
            logger.debug("_check_tracepoint: no source/line, falling through to STOPPED")
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
            return

        tp = mgr.find_tracepoint_for_location(top.source, top.line)
        if tp is None:
            logger.debug("_check_tracepoint: no matching tracepoint, normal STOPPED")
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()
            return
        logger.debug("_check_tracepoint: matched tracepoint %s, evaluating", tp.id)

        # Check whether a user-defined breakpoint also exists at this location.
        user_bps = self._breakpoints.get_for_file(top.source)
        has_user_breakpoint = any(bp.line == top.line for bp in user_bps)

        if has_user_breakpoint:
            self._set_state(DebugState.STOPPED)
            self._execution_event.set()

        await mgr.on_tracepoint_hit(
            tp, self, thread_id, has_user_breakpoint=has_user_breakpoint, top_frame=top
        )

    async def _update_hit_count(self, thread_id: int) -> None:
        """Fetch top frame and increment hit count for matching breakpoint."""
        try:
            frames = await asyncio.wait_for(
                self.get_stack_trace(thread_id=thread_id, levels=1), timeout=3.0,
            )
            if not frames:
                return
            top = frames[0]
            if top.source and top.line:
                key = (self.breakpoints._normalize_path(top.source), top.line)
                self._state.hit_counts[key] = self._state.hit_counts.get(key, 0) + 1
                logger.debug("Hit count for %s: %d", key, self._state.hit_counts[key])
        except asyncio.TimeoutError:
            logger.debug("_update_hit_count timed out after 3s")
        except Exception:
            logger.debug("Could not update hit count", exc_info=True)

    def _on_continued(self, event: DAPEvent) -> None:
        """Handle continued event."""
        self._set_state(DebugState.RUNNING)
        if event.body.get("allThreadsContinued", True):
            # All threads resumed — clear all stop-state
            self._state.current_thread_id = None
            self._state.current_frame_id = None
            self._state.stop_reason = None
            self._state.stop_description = None
            self._state.stop_text = None

    def _on_terminated(self, event: DAPEvent) -> None:
        """Handle terminated event."""
        self._set_state(DebugState.TERMINATED)
        self._execution_event.set()
        logger.info("Debug session terminated")

    def _on_exited(self, event: DAPEvent) -> None:
        """Handle exited event."""
        self._state.exit_code = event.body.get("exitCode", 0)
        self._execution_event.set()
        logger.info(f"Process exited with code {self._state.exit_code}")

    def _on_output(self, event: DAPEvent) -> None:
        """Handle output event."""

        body = OutputEventBody.from_dict(event.body)
        output = body.output

        # Truncate individual entries (security: prevent single large entry)
        if len(output) > MAX_OUTPUT_ENTRY:
            output = output[:MAX_OUTPUT_ENTRY] + "... [truncated]"

        entry = OutputEntry(text=output, category=body.category.value)

        # Capture variablesReference if the adapter attached structured output
        var_ref = event.body.get("variablesReference", 0)
        if var_ref and var_ref > 0:
            entry.variables_reference = var_ref

        self._state.output_buffer.append(entry)
        self._output_bytes += len(output)

        # Trim buffer by byte size (security: prevent DoS)
        while self._output_bytes > MAX_OUTPUT_BYTES and self._state.output_buffer:
            removed = self._state.output_buffer.popleft()
            self._output_bytes -= len(removed.text)

    def _on_thread(self, event: DAPEvent) -> None:
        """Handle thread event."""
        thread_id = event.body.get("threadId", 0)
        reason = event.body.get("reason", "started")

        if reason == "exited":
            self._state.threads = [t for t in self._state.threads if t.id != thread_id]
        # Note: "started" events are handled lazily via get_threads()

    def _on_process(self, event: DAPEvent) -> None:
        """Handle process event."""
        pid_raw = event.body.get("systemProcessId")
        name_raw = event.body.get("name")

        # Normalize PID to int, handle string/invalid values
        pid = None
        if pid_raw is not None:
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                logger.warning(f"Invalid systemProcessId in process event: {pid_raw}")
                pid = None

        # Normalize name to string
        name = str(name_raw) if name_raw is not None else None

        self._state.process_id = pid
        self._state.process_name = name

        if pid is not None:
            logger.info(f"Process started: PID={pid}, name={name or 'unknown'}")
            self._process_registry.register(
                pid=pid,
                role="debuggee",
                program=name,
            )

    def _on_breakpoint(self, event: DAPEvent) -> None:
        """Handle breakpoint changed/added/removed events from adapter."""

        body = BreakpointEventBody.from_dict(event.body)
        logger.debug(f"Breakpoint event: reason={body.reason}, id={body.breakpoint_id}")

        if body.reason == "removed" and body.breakpoint_id is not None:
            # Remove breakpoint by ID from registry
            for file_path, bps in self.breakpoints.get_all().items():
                for bp in bps:
                    if bp.id == body.breakpoint_id:
                        self.breakpoints.remove(file_path, bp.line)
                        logger.info(f"Breakpoint {body.breakpoint_id} removed by adapter")
                        return
        elif body.reason in ("changed", "new") and body.breakpoint_id is not None:
            # Update existing breakpoint's verified status and line
            for file_path, bps in self.breakpoints.get_all().items():
                for bp in bps:
                    if bp.id == body.breakpoint_id:
                        bp.verified = body.verified
                        if body.line is not None:
                            bp.line = body.line
                        logger.debug(
                            f"Breakpoint {body.breakpoint_id} updated: "
                            f"verified={body.verified}, line={body.line}"
                        )
                        return
            # New breakpoint from adapter — log but don't create (we don't know the file)
            if body.reason == "new":
                logger.info(
                    f"Adapter reported new breakpoint {body.breakpoint_id} "
                    f"(not in our registry)"
                )

    def _on_module(self, event: DAPEvent) -> None:
        """Handle module load/change/unload events."""

        body = ModuleEventBody.from_dict(event.body)
        logger.debug(f"Module event: reason={body.reason}, name={body.name}")

        if body.reason == "new":
            # Add new module — avoid duplicates by ID
            existing_ids = {m.id for m in self._state.modules}
            if body.module_id not in existing_ids:
                self._state.modules.append(ModuleInfo(
                    id=body.module_id,
                    name=body.name,
                    path=body.path,
                    version=body.version,
                    is_optimized=body.is_optimized,
                    symbol_status=body.symbol_status,
                ))
                logger.info(f"Module loaded: {body.name}")
        elif body.reason == "changed":
            for m in self._state.modules:
                if m.id == body.module_id:
                    m.name = body.name
                    m.path = body.path
                    m.version = body.version
                    m.is_optimized = body.is_optimized
                    m.symbol_status = body.symbol_status
                    logger.debug(f"Module updated: {body.name}")
                    break
        elif body.reason == "removed":
            self._state.modules = [
                m for m in self._state.modules if m.id != body.module_id
            ]
            logger.info(f"Module unloaded: {body.name}")

    def prepare_for_execution(self) -> None:
        """Prepare for an execution command by creating a fresh event.

        MUST be called immediately before sending the DAP command (continue,
        step_over, etc.) to avoid race conditions with _execution_event.

        Creates a new Event object so any previously-set state is discarded.
        The DAP event handlers (_on_stopped, _on_terminated, _on_exited)
        will set this new event when the program stops.
        """
        self._execution_event = asyncio.Event()

    async def wait_for_stopped(self, timeout: float = 30.0) -> StoppedSnapshot:
        """Wait for execution to stop (breakpoint, step, exception, or termination).

        Blocks until a DAP stopped/terminated/exited event fires, or timeout expires.
        Call prepare_for_execution() before the DAP command, then this after.

        Args:
            timeout: Maximum seconds to wait. On timeout, returns snapshot with
                     timed_out=True and the current state (likely still RUNNING).

        Returns:
            StoppedSnapshot with the state at the moment execution stopped.
        """
        try:
            await asyncio.wait_for(self._execution_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process_alive = (
                self._client.is_running
                and self._state.process_id is not None
            )
            logger.warning(
                f"wait_for_stopped timed out after {timeout}s "
                f"(state={self._state.state.value}, process_alive={process_alive})"
            )
            return StoppedSnapshot(
                state=self._state.state,
                stop_reason=self._state.stop_reason,
                thread_id=self._state.current_thread_id,
                timed_out=True,
                exit_code=self._state.exit_code,
                process_alive=process_alive,
                description=self._state.stop_description,
                text=self._state.stop_text,
            )

        return StoppedSnapshot(
            state=self._state.state,
            stop_reason=self._state.stop_reason,
            thread_id=self._state.current_thread_id,
            timed_out=False,
            exit_code=self._state.exit_code,
            exception_info=self._state.exception_info,
            process_alive=self._state.state != DebugState.TERMINATED,
            description=self._state.stop_description,
            text=self._state.stop_text,
        )

    async def quick_evaluate(self, expression: str, frame_id: int | None = None) -> dict[str, Any]:
        """Pause → evaluate → resume atomically. For use while program is running."""
        if self._state.state != DebugState.RUNNING:
            raise RuntimeError(
                f"Program is not running (state: {self._state.state.value}). "
                "Use evaluate_expression instead."
            )

        async with self._quick_eval_lock:
            # Pause — prepare_for_execution MUST be before pause to avoid race
            tid = self._state.current_thread_id or 1
            self.prepare_for_execution()
            await self._client.pause(tid)
            # Wait for stopped
            try:
                await asyncio.wait_for(self._execution_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                raise RuntimeError("Program did not pause within 5 seconds")

            try:
                # Evaluate
                fid = frame_id
                if fid is None:
                    frames = await self.get_stack_trace(thread_id=tid, levels=1)
                    if frames:
                        fid = frames[0].id
                response = await self._client.evaluate(expression, fid)
                if response.success:
                    result = {
                        "result": response.body.get("result", ""),
                        "type": response.body.get("type", ""),
                        "variablesReference": response.body.get("variablesReference", 0),
                    }
                else:
                    result = {"error": response.message or "Evaluation failed"}
            except Exception as e:
                result = {"error": str(e)}

            # Resume (always — even if evaluate failed)
            try:
                self.prepare_for_execution()
                await self._client.continue_execution(tid)
            except Exception:
                # If resume fails, leave paused (safe state) — agent can inspect
                pass

            return result

    async def pre_launch_build(
        self,
        project_file: str,
        configuration: str = "Debug",
        restore_first: bool = True,
        timeout: float = 300.0,
    ) -> BuildResult:
        """Execute pre-launch build sequence (restore + build).

        This is the equivalent of VSCode's preLaunchTask for debugging.

        Args:
            project_file: Path to .csproj or .sln file
            configuration: Build configuration (Debug/Release)
            restore_first: Whether to run restore before build
            timeout: Total timeout for all operations

        Returns:
            Build result

        Raises:
            BuildError: If build fails
            ValueError: If project path is invalid or outside scope
        """
        if not self._project_path:
            raise ValueError("Project path not set for pre-launch build")

        # Validate project file path
        validated_project = self.validate_path(project_file, must_exist=True)

        # Run pre-launch build
        result = await self._build_manager.pre_launch_build(
            workspace_root=self._project_path,
            project_path=validated_project,
            configuration=configuration,
            restore_first=restore_first,
            timeout=timeout,
        )
        self._last_build_result = result
        return result

    async def launch(
        self,
        program: str,
        cwd: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stop_at_entry: bool = False,
        pre_build: bool = False,
        build_project: str | None = None,
        build_configuration: str = "Debug",
        progress_callback: Callable[[float, float, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Launch program for debugging.

        Args:
            program: Path to .dll or .exe to debug
            cwd: Working directory
            args: Command-line arguments
            env: Environment variables
            stop_at_entry: Stop at program entry point
            pre_build: Run pre-launch build before launching
            build_project: Project file for pre-build (required if pre_build=True)
            build_configuration: Build configuration for pre-build
            progress_callback: Async callback(progress, total, message) for progress

        Returns:
            Launch result

        Raises:
            RuntimeError: If launch fails
            BuildError: If pre-build fails
        """
        # Helper to report progress (safely handles None callback)
        async def report(progress: float, total: float, message: str) -> None:
            if progress_callback:
                await progress_callback(progress, total, message)

        # Run pre-launch build if requested
        if pre_build:
            if not build_project:
                raise ValueError("build_project required when pre_build=True")

            await report(0, 100, "Building project...")

            # Stop existing session first to release file locks
            if self.is_active:
                logger.info("Stopping existing session before build")
                await self.stop()
                # Give processes time to release file handles
                await asyncio.sleep(0.5)

            await self.pre_launch_build(
                project_file=build_project,
                configuration=build_configuration,
            )

            await report(50, 100, "Build complete, starting debugger...")

            # Re-validate program path after build (now file should exist)
            # Also apply smart .exe → .dll resolution for .NET 6+
            program = self.validate_program(program, must_exist=True)
            logger.debug(f"Post-build program path: {program}")
        else:
            await report(0, 100, "Starting debugger...")

        # Check dbgshim version compatibility (warns if mismatch)
        version_warning = self.check_dbgshim_compatibility(program)

        if not self._client.is_running:
            await self.start()

        await report(60, 100, "Initializing debug adapter...")

        # Wait for initialized event
        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=10.0)
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timeout waiting for DAP initialization") from e

        await report(70, 100, "Setting breakpoints...")

        # Set all breakpoints before launch
        await self._sync_all_breakpoints()

        # Set exception breakpoints (stop on all exceptions by default)
        await self._client.set_exception_breakpoints([])

        await report(80, 100, "Launching program...")

        # Launch program with justMyCode=False to show all stack frames
        response = await self._client.launch(
            program=program,
            cwd=cwd,
            args=args,
            env=env,
            stop_at_entry=stop_at_entry,
            just_my_code=False,
        )

        if not response.success:
            raise RuntimeError(f"Launch failed: {response.message}")

        # Configuration done
        await self._client.configuration_done()
        self._set_state(DebugState.RUNNING)

        await report(100, 100, "Debug session started")

        # Generate session ID for temp dir isolation
        import uuid
        self._session_id = uuid.uuid4().hex[:12]

        # Save launch config for restart
        self._last_launch_config = {
            "program": program,
            "cwd": cwd,
            "args": args,
            "env": env,
            "stop_at_entry": stop_at_entry,
            "pre_build": pre_build,
            "build_project": build_project,
            "build_configuration": build_configuration,
        }

        result: dict[str, Any] = {"success": True, "program": program}
        if version_warning:
            result["warning"] = version_warning
        return result

    async def attach(self, process_id: int) -> dict[str, Any]:
        """Attach to running process."""
        if not self._client.is_running:
            await self.start()

        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=10.0)
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timeout waiting for session initialization") from e

        await self._sync_all_breakpoints()
        await self._client.set_exception_breakpoints([])

        # NOTE: justMyCode is NOT supported in attach mode by netcoredbg (upstream limitation).
        # The parameter is passed for API consistency but will be ignored by the debugger.
        # Stack traces may be incomplete - use start_debug/launch for full functionality.
        response = await self._client.attach(process_id, just_my_code=False)
        if not response.success:
            raise RuntimeError(f"Attach failed: {response.message}")

        await self._client.configuration_done()
        self._set_state(DebugState.RUNNING)

        # Generate session ID for temp dir isolation
        import uuid
        self._session_id = uuid.uuid4().hex[:12]

        return {"success": True, "processId": process_id}

    async def stop(self) -> dict[str, Any]:
        """Stop debug session."""
        if self._client.is_running:
            try:
                await self._client.disconnect(terminate=True)
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            await self._client.stop()

        # Cleanup tracked processes
        self._process_registry.cleanup_all()

        # Cleanup session temp directory
        if self._session_id:
            self._temp_manager.cleanup_session(self._session_id)
            self._session_id = None

        self._set_state(DebugState.IDLE)
        self._initialized_event.clear()
        self._execution_event.clear()
        self._state = SessionState()
        self._output_bytes = 0  # Reset output tracking for next session

        return {"success": True}

    async def restart(self, rebuild: bool = True) -> dict[str, Any]:
        """Restart debug session with same configuration.

        Stops current session, optionally rebuilds, and relaunches.

        Args:
            rebuild: Whether to rebuild before restarting (default True)

        Returns:
            Launch result

        Raises:
            RuntimeError: If no previous launch configuration exists,
                         or if rebuild requested but no build_project configured
            BuildError: If rebuild fails
        """
        if not self._last_launch_config:
            raise RuntimeError("No previous launch configuration for restart")

        config = self._last_launch_config.copy()

        # Validate rebuild request - cannot rebuild without build_project
        if rebuild and not config.get("build_project"):
            raise RuntimeError(
                "Cannot rebuild on restart: no build_project in saved configuration"
            )

        # Always stop existing session first to ensure clean state
        # This is needed even when pre_build=False to avoid relaunch issues
        await self.stop()

        # Force pre_build if rebuild requested and we have build info
        if rebuild and config.get("build_project"):
            config["pre_build"] = True

        return await self.launch(**config)

    async def _sync_all_breakpoints(self) -> None:
        """Sync all breakpoints to DAP."""
        for file_path in self._breakpoints.get_files():
            await self._sync_file_breakpoints(file_path)
        await self._sync_function_breakpoints()

    async def _sync_function_breakpoints(self) -> None:
        """Sync function breakpoints to DAP."""
        bps = self._breakpoints.get_function_breakpoints()
        dap_bps = [bp.to_dap() for bp in bps]
        response = await self._client.set_function_breakpoints(dap_bps)
        if response.success:
            for i, dap_bp in enumerate(response.body.get("breakpoints", [])):
                if i < len(bps):
                    bps[i].verified = dap_bp.get("verified", False)
                    bps[i].id = dap_bp.get("id")

    async def _sync_file_breakpoints(self, file_path: str) -> None:
        """Sync breakpoints for a single file."""
        breakpoints = self._breakpoints.get_for_file(file_path)
        dap_breakpoints = [bp.to_dap() for bp in breakpoints]

        response = await self._client.set_breakpoints(file_path, dap_breakpoints)
        if response.success:
            self._breakpoints.update_from_dap(
                file_path, response.body.get("breakpoints", [])
            )

    # Breakpoint operations

    async def add_breakpoint(
        self,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> Breakpoint:
        """Add a breakpoint."""
        bp = Breakpoint(
            file=file,
            line=line,
            condition=condition,
            hit_condition=hit_condition,
        )
        self._breakpoints.add(bp)

        if self.is_active:
            await self._sync_file_breakpoints(file)

        return bp

    async def remove_breakpoint(self, file: str, line: int) -> bool:
        """Remove a breakpoint."""
        removed = self._breakpoints.remove(file, line)
        if removed and self.is_active:
            await self._sync_file_breakpoints(file)
        return removed

    async def clear_breakpoints(self, file: str | None = None) -> int:
        """Clear breakpoints."""
        if file:
            files = [file]
        else:
            files = self._breakpoints.get_files()

        count = self._breakpoints.clear(file)

        if self.is_active:
            for f in files:
                await self._client.set_breakpoints(f, [])

        return count

    async def add_function_breakpoint(
        self, name: str, condition: str | None = None, hit_condition: str | None = None
    ) -> FunctionBreakpoint:
        """Add a function breakpoint."""
        bp = FunctionBreakpoint(name=name, condition=condition, hit_condition=hit_condition)
        self._breakpoints.add_function_breakpoint(bp)
        if self.is_active:
            await self._sync_function_breakpoints()
        return bp

    async def remove_function_breakpoint(self, name: str) -> bool:
        """Remove a function breakpoint."""
        removed = self._breakpoints.remove_function_breakpoint(name)
        if removed and self.is_active:
            await self._sync_function_breakpoints()
        return removed

    async def set_variable(
        self, variables_reference: int, name: str, value: str
    ) -> dict[str, Any]:
        """Set a variable's value."""
        response = await self._client.set_variable(variables_reference, name, value)
        if response.success:
            return {
                "value": response.body.get("value", ""),
                "type": response.body.get("type"),
                "variablesReference": response.body.get("variablesReference", 0),
            }
        else:
            raise RuntimeError(response.message or "Failed to set variable")

    # Execution control

    async def continue_execution(self, thread_id: int | None = None) -> dict[str, Any]:
        """Continue execution."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread to continue")

        response = await self._client.continue_execution(tid)
        if response.success:
            self._set_state(DebugState.RUNNING)
        return {"success": response.success, "threadId": tid}

    async def step_over(self, thread_id: int | None = None) -> dict[str, Any]:
        """Step over."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stepping")

        response = await self._client.step_over(tid)
        return {"success": response.success, "threadId": tid}

    async def step_in(
        self, thread_id: int | None = None, target_id: int | None = None
    ) -> dict[str, Any]:
        """Step into, optionally targeting a specific call on the line."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stepping")

        response = await self._client.step_in(tid, target_id=target_id)
        return {"success": response.success, "threadId": tid}

    async def get_step_in_targets(self, frame_id: int | None = None) -> list[dict[str, Any]]:
        """Get available step-in targets for a frame."""
        fid = frame_id or self._state.current_frame_id
        if fid is None:
            raise RuntimeError(
                "No frame for step-in targets. Call get_call_stack first or provide frame_id."
            )
        response = await self._client.step_in_targets(fid)
        if response.success:
            targets = response.body.get("targets", [])
            return [{"id": t["id"], "label": t["label"]} for t in targets]
        return []

    async def step_out(self, thread_id: int | None = None) -> dict[str, Any]:
        """Step out."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stepping")

        response = await self._client.step_out(tid)
        return {"success": response.success, "threadId": tid}

    async def pause(self, thread_id: int | None = None) -> dict[str, Any]:
        """Pause execution.

        If no thread_id is provided, uses current_thread_id or queries
        available threads and pauses the first one.
        """
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            # Query threads and use the first one
            threads = await self.get_threads()
            if threads:
                tid = threads[0].id
                logger.debug(f"pause: no thread_id provided, using first thread {tid}")
            else:
                raise RuntimeError(
                    "No threads available to pause. The program may not be running."
                )

        response = await self._client.pause(tid)
        return {"success": response.success, "threadId": tid}

    # Inspection

    async def get_threads(self) -> list[ThreadInfo]:
        """Get all threads."""
        response = await self._client.threads()
        if response.success:
            threads = [
                ThreadInfo(id=t["id"], name=t.get("name", f"Thread {t['id']}"))
                for t in response.body.get("threads", [])
            ]
            self._state.threads = threads
            return threads
        return []

    async def get_stack_trace(
        self, thread_id: int | None = None, start_frame: int = 0, levels: int = 20
    ) -> list[StackFrame]:
        """Get stack trace for thread."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stack trace")

        # Retry on CORDBG_E_PROCESS_NOT_SYNCHRONIZED (0x80131302) — race
        # between stopped event and ICorDebug internal synchronization.
        # Occurs after step_in/step_over/step_out when stackTrace is called
        # before netcoredbg finishes syncing the debuggee process.
        max_retries = 3
        retry_delay = 0.1  # 100ms between retries
        response = None
        for attempt in range(max_retries + 1):
            response = await self._client.stack_trace(tid, start_frame, levels)
            if response.success:
                break
            if "0x80131302" in (response.message or "") and attempt < max_retries:
                logger.debug(
                    "stack_trace: PROCESS_NOT_SYNCHRONIZED, retry %d/%d after %.0fms",
                    attempt + 1, max_retries, retry_delay * 1000,
                )
                await asyncio.sleep(retry_delay)
                continue
            break

        logger.debug(
            f"stack_trace response for thread {tid}: success={response.success}, "
            f"body={response.body}, message={response.message}"
        )
        if response.success:
            frames = []
            raw_frames = response.body.get("stackFrames", [])
            logger.debug(f"Parsing {len(raw_frames)} stack frames")
            for f in raw_frames:
                source = f.get("source", {})
                frames.append(
                    StackFrame(
                        id=f["id"],
                        name=f.get("name", "<unknown>"),
                        source=source.get("path") if source else None,
                        line=f.get("line", 0),
                        column=f.get("column", 0),
                    )
                )
            if frames:
                self._state.current_frame_id = frames[0].id
            return frames
        else:
            logger.warning(
                f"stack_trace failed for thread {tid}: {response.message}"
            )
            return []

    async def get_scopes(self, frame_id: int | None = None) -> list[dict[str, Any]]:
        """Get scopes for frame."""
        fid = frame_id or self._state.current_frame_id
        logger.debug(
            f"get_scopes: frame_id={frame_id}, current_frame_id={self._state.current_frame_id}, "
            f"resolved_fid={fid}"
        )
        if fid is None:
            raise RuntimeError(
                "No frame for scopes. Call get_call_stack first to select a frame, "
                "or provide frame_id explicitly."
            )

        response = await self._client.scopes(fid)
        if response.success:
            scopes = response.body.get("scopes", [])
            logger.debug(f"get_scopes: found {len(scopes)} scopes for frame {fid}")
            return scopes
        logger.warning(f"get_scopes failed for frame {fid}: {response.message}")
        return []

    async def get_variables(
        self,
        variables_reference: int,
        filter: str | None = None,
        start: int | None = None,
        count: int | None = None,
    ) -> list[Variable]:
        """Get variables for scope/variable, with optional paging and filtering."""
        response = await self._client.variables(
            variables_reference, filter=filter, start=start, count=count
        )
        if response.success:
            return [
                Variable(
                    name=v["name"],
                    value=v.get("value", ""),
                    type=v.get("type"),
                    variables_reference=v.get("variablesReference", 0),
                    named_variables=v.get("namedVariables", 0),
                    indexed_variables=v.get("indexedVariables", 0),
                )
                for v in response.body.get("variables", [])
            ]
        return []

    async def evaluate(
        self, expression: str, frame_id: int | None = None, context: str = "watch"
    ) -> dict[str, Any]:
        """Evaluate expression."""
        fid = frame_id or self._state.current_frame_id
        response = await self._client.evaluate(expression, fid, context)
        if response.success:
            return {
                "result": response.body.get("result", ""),
                "type": response.body.get("type"),
                "variablesReference": response.body.get("variablesReference", 0),
            }
        else:
            return {"error": response.message or "Evaluation failed"}

    async def configure_exception_breakpoints(self, filters: list[str]) -> bool:
        """Configure which exceptions should pause the debugger.

        Args:
            filters: List of exception filter names

        Returns:
            True if successful
        """
        response = await self._client.set_exception_breakpoints(filters)
        return response.success

    async def get_exception_info(
        self, thread_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get exception info for thread."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            return None

        response = await self._client.exception_info(tid)
        if response.success:
            self._state.exception_info = response.body
            return response.body
        return None

    async def get_exception_context(
        self,
        max_frames: int = 10,
        include_variables_for_frames: int = 1,
        max_inner_exceptions: int = 5,
    ) -> dict[str, Any]:
        """Get full exception context in a single call (exception autopsy).

        Returns exception type, message, inner exception chain, stack frames
        with source locations, and local variables for top N frames.
        Replaces the manual sequence: get_exception_info + get_call_stack + get_scopes + get_variables.
        """
        if self._state.state != DebugState.STOPPED:
            raise RuntimeError("Program is not stopped")
        if self._state.stop_reason != "exception":
            raise RuntimeError(
                f"Not stopped at an exception (reason: {self._state.stop_reason}). "
                "Use get_call_stack + get_variables instead."
            )

        tid = self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No active thread")

        result: dict[str, Any] = {"threadId": tid}

        # 1. Exception info from DAP
        exc_info = await self.get_exception_info(tid)
        result["exception"] = exc_info or {}

        # 2. Stack frames
        frames = await self.get_stack_trace(thread_id=tid, levels=max_frames)
        frame_data = []
        for i, frame in enumerate(frames):
            fd: dict[str, Any] = {
                "index": i,
                "name": frame.name,
                "source": frame.source,
                "line": frame.line,
                "column": frame.column,
                "id": frame.id,
            }

            # Include variables for top N frames
            if i < include_variables_for_frames:
                try:
                    scopes = await self.get_scopes(frame.id)
                    scope_vars = {}
                    for scope in scopes:
                        ref = scope.get("variablesReference", 0)
                        if ref:
                            variables = await self.get_variables(ref)
                            scope_vars[scope.get("name", "Locals")] = [
                                {"name": v.name, "value": v.value, "type": v.type}
                                for v in variables[:20]  # Cap at 20 vars per scope
                            ]
                    fd["variables"] = scope_vars
                except Exception as e:
                    fd["variables"] = {"error": str(e)}

            frame_data.append(fd)

        result["frames"] = frame_data
        result["totalFrames"] = len(frames)

        # 3. Inner exceptions via $exception evaluation
        inner_exceptions = []
        prefix = "$exception.InnerException"
        for depth in range(1, max_inner_exceptions + 1):
            try:
                type_result = await self.evaluate(f"{prefix}.GetType().FullName")
                if "error" in type_result:
                    break
                msg_result = await self.evaluate(f"{prefix}.Message")
                inner_exceptions.append({
                    "type": type_result.get("result", "Unknown"),
                    "message": msg_result.get("result", ""),
                    "depth": depth,
                })
                prefix = f"{prefix}.InnerException"
            except Exception:
                break

        result["innerExceptions"] = inner_exceptions

        return result

    async def get_stop_context(
        self,
        include_variables: bool = True,
        include_output_tail: int = 10,
    ) -> dict[str, Any]:
        """Get rich context when stopped at any breakpoint — one call instead of many.

        Returns: stop reason, stack trace with source, locals in top frame,
        hit count for current location, recent output lines.
        """
        if self._state.state != DebugState.STOPPED:
            raise RuntimeError("Program is not stopped")

        tid = self._state.current_thread_id
        result: dict[str, Any] = {
            "state": self._state.state.value,
            "reason": self._state.stop_reason,
            "threadId": tid,
            "description": self._state.stop_description,
            "text": self._state.stop_text,
        }

        # Stack trace (top 5 frames)
        if tid is not None:
            frames = await self.get_stack_trace(thread_id=tid, levels=5)
            frame_data = []
            for frame in frames:
                fd: dict[str, Any] = {
                    "name": frame.name,
                    "source": frame.source,
                    "line": frame.line,
                    "id": frame.id,
                }
                frame_data.append(fd)
            result["frames"] = frame_data

            # Variables for top frame
            if include_variables and frames:
                try:
                    scopes = await self.get_scopes(frames[0].id)
                    local_vars = []
                    for scope in scopes:
                        ref = scope.get("variablesReference", 0)
                        if ref:
                            variables = await self.get_variables(ref)
                            for v in variables[:15]:
                                local_vars.append({
                                    "name": v.name,
                                    "value": v.value,
                                    "type": v.type,
                                })
                            break  # Only first scope (Locals)
                    result["locals"] = local_vars
                except Exception as e:
                    result["locals"] = [{"error": str(e)}]

            # Hit count for current location
            if frames and frames[0].source:
                norm = self.breakpoints._normalize_path(frames[0].source)
                key = (norm, frames[0].line)
                result["hitCount"] = self._state.hit_counts.get(key, 0)

        # Recent output
        if include_output_tail > 0:
            tail_entries = list(self._state.output_buffer)[-include_output_tail:]
            result["recentOutput"] = [
                {"text": e.text.rstrip(), "category": e.category}
                for e in tail_entries
                if e.text.strip()  # Skip empty lines
            ]

        # Exception info if stopped at exception
        if self._state.stop_reason == "exception" and tid is not None:
            try:
                result["exception"] = await self.get_exception_info(tid)
            except Exception:
                pass

        return result

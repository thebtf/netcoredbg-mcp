"""Debug session manager - orchestrates DAP client and state."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from ..build import BuildManager, BuildError, BuildResult
from ..dap import DAPClient, DAPEvent
from ..dap.events import StoppedEventBody, OutputEventBody, StopReason
from ..dap.protocol import Events
from .state import (
    DebugState,
    SessionState,
    BreakpointRegistry,
    Breakpoint,
    ThreadInfo,
    StackFrame,
    Variable,
)

logger = logging.getLogger(__name__)

# Output buffer limits (security: prevent DoS)
MAX_OUTPUT_BYTES = 10_000_000  # 10MB total buffer
MAX_OUTPUT_ENTRY = 100_000  # 100KB per entry


class SessionManager:
    """Manages debug session lifecycle and state."""

    def __init__(self, netcoredbg_path: str | None = None, project_path: str | None = None):
        self._client = DAPClient(netcoredbg_path)
        self._state = SessionState()
        self._breakpoints = BreakpointRegistry()
        self._state_listeners: list[Callable[[DebugState], None]] = []
        self._initialized_event = asyncio.Event()
        self._project_path = os.path.abspath(project_path) if project_path else None
        self._output_bytes = 0  # Track output buffer size
        self._build_manager = BuildManager()
        self._last_build_result: BuildResult | None = None
        self._last_launch_config: dict[str, Any] | None = None  # For restart

    @property
    def state(self) -> SessionState:
        """Get current session state."""
        return self._state

    @property
    def breakpoints(self) -> BreakpointRegistry:
        """Get breakpoint registry."""
        return self._breakpoints

    @property
    def is_active(self) -> bool:
        """Check if session is active."""
        return self._state.state not in (DebugState.IDLE, DebugState.TERMINATED)

    @property
    def project_path(self) -> str | None:
        """Get project path scope."""
        return self._project_path

    @property
    def last_build_result(self) -> BuildResult | None:
        """Get last build result."""
        return self._last_build_result

    def validate_path(self, path: str, must_exist: bool = False) -> str:
        """Validate path is within project scope.

        Args:
            path: Path to validate
            must_exist: If True, path must exist on filesystem

        Returns:
            Absolute path

        Raises:
            ValueError: If path is invalid or outside project scope
        """
        # Normalize path to absolute
        abs_path = os.path.abspath(path)

        # Check within project scope using os.path.commonpath
        if self._project_path:
            try:
                common = os.path.commonpath([abs_path, self._project_path])
                if common != self._project_path:
                    raise ValueError(f"Path outside project scope: {path}")
            except ValueError as e:
                # Different drives on Windows or other path issues
                raise ValueError(f"Path outside project scope: {path}") from e

        # Check existence if required
        if must_exist and not os.path.exists(abs_path):
            raise ValueError(f"Path does not exist: {path}")

        return abs_path

    def validate_program(self, program: str) -> str:
        """Validate program is a .NET assembly within scope.

        Args:
            program: Path to program (.dll or .exe)

        Returns:
            Absolute path to program

        Raises:
            ValueError: If program is invalid or outside project scope
        """
        path = self.validate_path(program, must_exist=True)
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".dll", ".exe"):
            raise ValueError(f"Program must be .NET assembly (.dll/.exe): {program}")
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

    def _on_initialized(self, event: DAPEvent) -> None:
        """Handle initialized event."""
        logger.info("DAP adapter initialized")
        self._initialized_event.set()

    def _on_stopped(self, event: DAPEvent) -> None:
        """Handle stopped event."""
        body = StoppedEventBody.from_dict(event.body)
        self._state.current_thread_id = body.thread_id
        self._state.stop_reason = body.reason.value
        self._set_state(DebugState.STOPPED)
        logger.info(f"Stopped: reason={body.reason.value}, thread={body.thread_id}")

    def _on_continued(self, event: DAPEvent) -> None:
        """Handle continued event."""
        self._set_state(DebugState.RUNNING)

    def _on_terminated(self, event: DAPEvent) -> None:
        """Handle terminated event."""
        self._set_state(DebugState.TERMINATED)
        logger.info("Debug session terminated")

    def _on_exited(self, event: DAPEvent) -> None:
        """Handle exited event."""
        self._state.exit_code = event.body.get("exitCode", 0)
        logger.info(f"Process exited with code {self._state.exit_code}")

    def _on_output(self, event: DAPEvent) -> None:
        """Handle output event."""
        body = OutputEventBody.from_dict(event.body)
        output = body.output

        # Truncate individual entries (security: prevent single large entry)
        if len(output) > MAX_OUTPUT_ENTRY:
            output = output[:MAX_OUTPUT_ENTRY] + "... [truncated]"

        self._state.output_buffer.append(output)
        self._output_bytes += len(output)

        # Trim buffer by byte size (security: prevent DoS)
        while self._output_bytes > MAX_OUTPUT_BYTES and self._state.output_buffer:
            removed = self._state.output_buffer.pop(0)
            self._output_bytes -= len(removed)

    def _on_thread(self, event: DAPEvent) -> None:
        """Handle thread event."""
        thread_id = event.body.get("threadId", 0)
        reason = event.body.get("reason", "started")

        if reason == "exited":
            self._state.threads = [t for t in self._state.threads if t.id != thread_id]
        # Note: "started" events are handled lazily via get_threads()

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

        Returns:
            Launch result

        Raises:
            RuntimeError: If launch fails
            BuildError: If pre-build fails
        """
        # Run pre-launch build if requested
        if pre_build:
            if not build_project:
                raise ValueError("build_project required when pre_build=True")

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

        if not self._client.is_running:
            await self.start()

        # Wait for initialized event
        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Timeout waiting for DAP initialization")

        # Set all breakpoints before launch
        await self._sync_all_breakpoints()

        # Set exception breakpoints (stop on all exceptions by default)
        await self._client.set_exception_breakpoints([])

        # Launch program
        response = await self._client.launch(
            program=program,
            cwd=cwd,
            args=args,
            env=env,
            stop_at_entry=stop_at_entry,
        )

        if not response.success:
            raise RuntimeError(f"Launch failed: {response.message}")

        # Configuration done
        await self._client.configuration_done()
        self._set_state(DebugState.RUNNING)

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

        return {"success": True, "program": program}

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

        response = await self._client.attach(process_id)
        if not response.success:
            raise RuntimeError(f"Attach failed: {response.message}")

        await self._client.configuration_done()
        self._set_state(DebugState.RUNNING)

        return {"success": True, "processId": process_id}

    async def stop(self) -> dict[str, Any]:
        """Stop debug session."""
        if self._client.is_running:
            try:
                await self._client.disconnect(terminate=True)
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            await self._client.stop()

        self._set_state(DebugState.IDLE)
        self._initialized_event.clear()
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
            RuntimeError: If no previous launch configuration exists
            BuildError: If rebuild fails
        """
        if not self._last_launch_config:
            raise RuntimeError("No previous launch configuration for restart")

        config = self._last_launch_config.copy()

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

    async def step_in(self, thread_id: int | None = None) -> dict[str, Any]:
        """Step into."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stepping")

        response = await self._client.step_in(tid)
        return {"success": response.success, "threadId": tid}

    async def step_out(self, thread_id: int | None = None) -> dict[str, Any]:
        """Step out."""
        tid = thread_id or self._state.current_thread_id
        if tid is None:
            raise RuntimeError("No thread for stepping")

        response = await self._client.step_out(tid)
        return {"success": response.success, "threadId": tid}

    async def pause(self, thread_id: int | None = None) -> dict[str, Any]:
        """Pause execution."""
        tid = thread_id or self._state.current_thread_id or 0
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

        response = await self._client.stack_trace(tid, start_frame, levels)
        if response.success:
            frames = []
            for f in response.body.get("stackFrames", []):
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
        return []

    async def get_scopes(self, frame_id: int | None = None) -> list[dict[str, Any]]:
        """Get scopes for frame."""
        fid = frame_id or self._state.current_frame_id
        if fid is None:
            raise RuntimeError("No frame for scopes")

        response = await self._client.scopes(fid)
        if response.success:
            return response.body.get("scopes", [])
        return []

    async def get_variables(self, variables_reference: int) -> list[Variable]:
        """Get variables for scope/variable."""
        response = await self._client.variables(variables_reference)
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

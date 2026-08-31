"""Build session - per-workspace state machine with process management.

State machine:
IDLE → BUILDING → READY | FAILED | CANCELLED
     ↑__________________|

Uses one private WindowsOwnedProcess capability per Windows command.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

from ..windows_process_owner import DrainStatus, OwnerDrainReceipt, WindowsOwnedProcess
from .policy import BuildCommand, BuildPolicy
from .state import BuildError, BuildResult, BuildState

logger = logging.getLogger(__name__)

# Output buffer limits (security: prevent DoS)
MAX_OUTPUT_BYTES: int = 5_000_000  # 5MB total
MAX_OUTPUT_LINE: int = 10_000  # 10KB per line

# Retry settings for file lock issues
MAX_BUILD_RETRIES: int = 3
RETRY_DELAY_SECONDS: float = 1.0

# A completed command gives descendants a short natural-exit window; cancellation
# skips that window and uses the same bounded retained-Job force operation.
COMMAND_OWNER_GRACE_TIMEOUT: float = 0.25
COMMAND_OWNER_FORCE_TIMEOUT: float = 7.0
_IS_WINDOWS = os.name == "nt"


class BuildSession:
    """Per-workspace build session with state machine.

    Thread-safe via asyncio.Lock. Only one build can run at a time per workspace.
    """

    def __init__(
        self,
        workspace_root: str,
        policy: BuildPolicy | None = None,
    ):
        """Initialize build session.

        Args:
            workspace_root: Root directory of workspace
            policy: Build policy (created with defaults if not provided)
        """
        self._workspace_root = os.path.abspath(workspace_root)
        self._policy = policy or BuildPolicy(workspace_root=self._workspace_root)
        self._state = BuildState.IDLE
        self._lock = asyncio.Lock()
        self._current_process: asyncio.subprocess.Process | WindowsOwnedProcess | None = None
        self._current_owner: WindowsOwnedProcess | None = None
        self._last_owner_drain_receipt: OwnerDrainReceipt | None = None
        self._command_generation = 0
        self._cancel_requested = False
        self._last_result: BuildResult | None = None
        self._state_listeners: list[Callable[[BuildState], None]] = []

    @property
    def state(self) -> BuildState:
        """Current build state."""
        return self._state

    @property
    def workspace_root(self) -> str:
        """Workspace root directory."""
        return self._workspace_root

    @property
    def last_result(self) -> BuildResult | None:
        """Last build result."""
        return self._last_result

    @property
    def is_building(self) -> bool:
        """Whether a build is currently running."""
        return self._state == BuildState.BUILDING

    def on_state_change(self, listener: Callable[[BuildState], None]) -> None:
        """Register state change listener."""
        self._state_listeners.append(listener)

    def _set_state(self, new_state: BuildState) -> None:
        """Update state and notify listeners."""
        old_state = self._state
        self._state = new_state
        if old_state != new_state:
            logger.info(f"Build state: {old_state.value} -> {new_state.value}")
            for listener in self._state_listeners:
                try:
                    listener(new_state)
                except Exception:
                    logger.exception("State listener error")

    def _next_command_generation(self) -> int:
        """Return a session-local identity for one Windows command capability."""
        self._command_generation += 1
        return self._command_generation

    async def _drain_windows_owner(
        self,
        owner: WindowsOwnedProcess,
        *,
        force: bool,
    ) -> OwnerDrainReceipt:
        """Capture one retained-owner receipt without abandoning cleanup on cancellation."""
        operation = (
            owner.force_and_drain(timeout=COMMAND_OWNER_FORCE_TIMEOUT)
            if force
            else owner.drain_after_grace(
                grace_timeout=COMMAND_OWNER_GRACE_TIMEOUT,
                force_timeout=COMMAND_OWNER_FORCE_TIMEOUT,
            )
        )
        drain_task = asyncio.create_task(operation)
        cancelled = False
        while True:
            try:
                receipt = await asyncio.shield(drain_task)
                break
            except asyncio.CancelledError:
                if drain_task.cancelled():
                    raise
                # Shielding keeps the retained Job drain alive through a second
                # cancellation. We must wait for its accounting receipt before
                # restoring the original cancellation outcome to the caller.
                cancelled = True

        self._last_owner_drain_receipt = receipt
        if cancelled:
            raise asyncio.CancelledError
        return receipt

    @staticmethod
    def _require_drained_owner_receipt(receipt: OwnerDrainReceipt) -> None:
        """Reject normal completion unless private-Job accounting reached zero."""
        if receipt.status is DrainStatus.DRAINED and receipt.active_processes == 0:
            return
        # A root exit, root kill, or numeric PID says nothing about descendants.
        # Only zero accounting from this retained private Job proves this command
        # tree is drained, so normal command completion must fail closed here.
        raise RuntimeError(
            "Windows build command owner did not drain "
            f"(status={receipt.status.value}, active={receipt.active_processes})"
        )

    async def _run_command(
        self,
        command: list[str],
        cwd: str | None = None,
        timeout: float = 300.0,
        output_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> tuple[int, str, str]:
        """Run command with output capture and timeout.

        Args:
            command: Command and arguments
            cwd: Working directory
            timeout: Timeout in seconds

        Returns:
            Tuple of (exit_code, stdout, stderr)

        Raises:
            asyncio.CancelledError: If cancelled
            asyncio.TimeoutError: If timeout exceeded
        """
        owner: WindowsOwnedProcess | None = None
        try:
            if _IS_WINDOWS:
                # Windows must not launch an executing asyncio child and then
                # reopen its PID. This capability returns only after suspended
                # creation, private-Job admission, accounting, I/O wiring, and
                # the final resume have all succeeded.
                owner = await WindowsOwnedProcess.launch(
                    generation=self._next_command_generation(),
                    argv=tuple(command),
                    cwd=cwd,
                    env=None,
                    stdin_mode="devnull",
                )
                process: asyncio.subprocess.Process | WindowsOwnedProcess = owner
                self._current_owner = owner
                self._last_owner_drain_receipt = None
            else:
                # This Windows-only owner is unavailable here. Keep shell=False
                # for command integrity; DEVNULL prevents an IPC stdin from hanging.
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )

            self._current_process = process
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            async def read_stream(
                stream: asyncio.StreamReader | None,
                lines: list[str],
                byte_counter: list[int],
                stream_name: str = "stdout",
            ) -> None:
                if stream is None:
                    return
                while True:
                    try:
                        line = await asyncio.wait_for(stream.readline(), timeout=1.0)
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace")
                        # Bound one runaway line before retaining it.
                        if len(decoded) > MAX_OUTPUT_LINE:
                            decoded = decoded[:MAX_OUTPUT_LINE] + "...[truncated]\n"
                        lines.append(decoded)
                        byte_counter[0] += len(decoded)
                        # Evict oldest lines until the total buffer is bounded.
                        while byte_counter[0] > MAX_OUTPUT_BYTES and lines:
                            removed = lines.pop(0)
                            byte_counter[0] -= len(removed)
                        if output_callback:
                            try:
                                await output_callback(decoded.rstrip("\r\n"), stream_name)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "output_callback raised %s: %s", type(exc).__name__, exc
                                )
                    except asyncio.TimeoutError:
                        if self._cancel_requested:
                            raise asyncio.CancelledError()
                        continue

            stdout_counter = [0]
            stderr_counter = [0]

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(process.stdout, stdout_lines, stdout_counter, "stdout"),
                        read_stream(process.stderr, stderr_lines, stderr_counter, "stderr"),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Build timeout after {timeout}s")
                if owner is not None:
                    # Preserve the timeout only after the exact owner has
                    # recorded its drain receipt. Returning first could report
                    # a timed-out build while a retained Job still has children.
                    await self._drain_windows_owner(owner, force=True)
                else:
                    assert not isinstance(process, WindowsOwnedProcess)
                    process.kill()
                raise

            exit_code = await process.wait()
            if owner is not None:
                receipt = await self._drain_windows_owner(owner, force=False)
                self._require_drained_owner_receipt(receipt)
            return exit_code, "".join(stdout_lines), "".join(stderr_lines)

        except asyncio.CancelledError:
            if owner is not None:
                # Outer cancellation and BuildSession.cancel() share this owner
                # operation. Its receipt must exist before this original
                # cancellation is re-raised, even if cleanup is cancelled again.
                await self._drain_windows_owner(owner, force=True)
            raise
        finally:
            self._current_process = None
            if owner is not None:
                try:
                    await owner.aclose()
                finally:
                    if self._current_owner is owner:
                        self._current_owner = None

    def _is_file_lock_error(self, stdout: str, stderr: str) -> bool:
        """Check if build failed due to file lock errors.

        Args:
            stdout: Build stdout
            stderr: Build stderr

        Returns:
            True if file lock error detected
        """
        lock_patterns = [
            "MSB3021",  # Cannot copy file - access denied
            "MSB3026",  # Could not copy - file is in use
            "MSB3027",  # Could not copy - exceeded retry count
            "being used by another process",
            "The process cannot access the file",
            "because it is being used by another process",
        ]
        combined = stdout + stderr
        return any(pattern in combined for pattern in lock_patterns)

    async def _run_build_with_retry(
        self,
        cmd: list[str],
        timeout: float,
        retry_on_lock: bool,
        output_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> tuple[int, str, str]:
        """Run build command with retry logic for file lock errors.

        Args:
            cmd: Build command
            timeout: Timeout in seconds
            retry_on_lock: Whether to retry on lock errors

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        last_exit_code = 1
        last_stdout = ""
        last_stderr = ""

        for attempt in range(MAX_BUILD_RETRIES):
            exit_code, stdout, stderr = await self._run_command(
                cmd, cwd=self._workspace_root, timeout=timeout, output_callback=output_callback
            )

            last_exit_code = exit_code
            last_stdout = stdout
            last_stderr = stderr

            # Success - no retry needed
            if exit_code == 0:
                return exit_code, stdout, stderr

            # Check if file lock error
            if retry_on_lock and self._is_file_lock_error(stdout, stderr):
                if attempt < MAX_BUILD_RETRIES - 1:
                    logger.warning(
                        f"Build failed due to file locks "
                        f"(attempt {attempt + 1}/{MAX_BUILD_RETRIES}), retrying..."
                    )
                    # The failed command already drained its own capability.
                    # A retry starts a fresh command capability without deriving
                    # process authority from an observation.
                    await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue

            # Not a lock error or max retries reached - don't retry
            break

        return last_exit_code, last_stdout, last_stderr

    async def build(
        self,
        project_path: str,
        command: BuildCommand = BuildCommand.BUILD,
        configuration: str = "Debug",
        extra_args: list[str] | None = None,
        timeout: float = 300.0,
        retry_on_lock: bool = True,
        output_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> BuildResult:
        """Execute build command.

        Args:
            project_path: Path to project file or directory
            command: Build command to execute
            configuration: Build configuration
            extra_args: Additional arguments
            timeout: Timeout in seconds
            retry_on_lock: Retry build if file lock errors detected

        Returns:
            Build result

        Raises:
            BuildError: If build fails critically
        """
        async with self._lock:
            self._cancel_requested = False
            self._set_state(BuildState.BUILDING)
            start_time = time.perf_counter()

            try:
                # Validate project path
                validated_path = self._policy.validate_project_path(project_path)

                # For rebuild, run clean first
                if command == BuildCommand.REBUILD:
                    clean_cmd = self._policy.get_dotnet_command(
                        BuildCommand.CLEAN, validated_path, configuration
                    )
                    logger.info(f"Running clean: {' '.join(clean_cmd)}")
                    exit_code, stdout, stderr = await self._run_command(
                        clean_cmd,
                        cwd=self._workspace_root,
                        timeout=timeout / 2,
                        output_callback=output_callback,
                    )
                    if exit_code != 0:
                        duration = (time.perf_counter() - start_time) * 1000
                        result = BuildResult(
                            success=False,
                            state=BuildState.FAILED,
                            command="clean",
                            project_path=validated_path,
                            configuration=configuration,
                            exit_code=exit_code,
                            stdout=stdout,
                            stderr=stderr,
                            duration_ms=duration,
                        )
                        self._last_result = result
                        self._set_state(BuildState.FAILED)
                        return result

                # Run main command with retry logic
                cmd = self._policy.get_dotnet_command(
                    command, validated_path, configuration, extra_args
                )
                logger.info(f"Running: {' '.join(cmd)}")

                exit_code, stdout, stderr = await self._run_build_with_retry(
                    cmd=cmd,
                    timeout=timeout,
                    retry_on_lock=retry_on_lock,
                    output_callback=output_callback,
                )

                duration = (time.perf_counter() - start_time) * 1000
                success = exit_code == 0

                result = BuildResult(
                    success=success,
                    state=BuildState.READY if success else BuildState.FAILED,
                    command=command.value,
                    project_path=validated_path,
                    configuration=configuration,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=duration,
                )
                self._last_result = result
                self._set_state(result.state)
                return result

            except asyncio.CancelledError:
                duration = (time.perf_counter() - start_time) * 1000
                result = BuildResult(
                    success=False,
                    state=BuildState.CANCELLED,
                    command=command.value,
                    project_path=project_path,
                    configuration=configuration,
                    duration_ms=duration,
                    cancelled=True,
                )
                self._last_result = result
                self._set_state(BuildState.CANCELLED)
                return result

            except asyncio.TimeoutError:
                duration = (time.perf_counter() - start_time) * 1000
                result = BuildResult(
                    success=False,
                    state=BuildState.FAILED,
                    command=command.value,
                    project_path=project_path,
                    configuration=configuration,
                    duration_ms=duration,
                    stderr=f"Build timeout after {timeout}s",
                )
                self._last_result = result
                self._set_state(BuildState.FAILED)
                return result

            except ValueError as e:
                # Policy validation errors
                self._set_state(BuildState.FAILED)
                raise BuildError(str(e)) from e

            except Exception as e:
                self._set_state(BuildState.FAILED)
                raise BuildError(f"Build failed: {e}") from e

    async def cancel(self) -> bool:
        """Cancel current build.

        Returns:
            True if a build was cancelled
        """
        if not self.is_building:
            return False

        self._cancel_requested = True

        if self._current_owner is not None:
            await self._drain_windows_owner(self._current_owner, force=True)
        elif not _IS_WINDOWS and self._current_process is not None:
            assert not isinstance(self._current_process, WindowsOwnedProcess)
            try:
                self._current_process.kill()
            except Exception:
                pass

        return True

    async def clean(
        self,
        project_path: str,
        configuration: str = "Debug",
        timeout: float = 60.0,
    ) -> BuildResult:
        """Clean build outputs."""
        return await self.build(project_path, BuildCommand.CLEAN, configuration, timeout=timeout)

    async def restore(
        self,
        project_path: str,
        timeout: float = 300.0,
        output_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> BuildResult:
        """Restore NuGet packages."""
        return await self.build(
            project_path,
            BuildCommand.RESTORE,
            timeout=timeout,
            output_callback=output_callback,
        )

    async def rebuild(
        self,
        project_path: str,
        configuration: str = "Debug",
        extra_args: list[str] | None = None,
        timeout: float = 600.0,
    ) -> BuildResult:
        """Clean and rebuild project."""
        return await self.build(
            project_path, BuildCommand.REBUILD, configuration, extra_args, timeout
        )

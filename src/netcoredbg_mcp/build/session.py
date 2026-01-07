"""Build session - per-workspace state machine with process management.

State machine:
IDLE → BUILDING → READY | FAILED | CANCELLED
     ↑__________________|

Uses Windows Job Objects for reliable process tree cleanup.
Includes process cleanup before build to release file locks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable

from .cleanup import cleanup_for_build
from .policy import BuildCommand, BuildPolicy
from .state import BuildError, BuildResult, BuildState

logger = logging.getLogger(__name__)

# Output buffer limits (security: prevent DoS)
MAX_OUTPUT_BYTES: int = 5_000_000  # 5MB total
MAX_OUTPUT_LINE: int = 10_000  # 10KB per line

# Retry settings for file lock issues
MAX_BUILD_RETRIES: int = 3
RETRY_DELAY_SECONDS: float = 1.0


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
        self._current_process: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self._last_result: BuildResult | None = None
        self._state_listeners: list[Callable[[BuildState], None]] = []
        self._job_handle: int | None = None  # Windows Job Object handle

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

    async def _create_job_object(self) -> int | None:
        """Create Windows Job Object for process tree management.

        Returns:
            Job handle or None if not on Windows
        """
        if os.name != "nt":
            return None

        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32

            # CreateJobObjectW
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                logger.warning("Failed to create job object")
                return None

            # Set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

            success = kernel32.SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not success:
                logger.warning("Failed to set job object limits")
                kernel32.CloseHandle(job)
                return None

            return job
        except Exception as e:
            logger.warning(f"Job object creation failed: {e}")
            return None

    async def _assign_to_job(self, process: asyncio.subprocess.Process) -> None:
        """Assign process to job object for tree management."""
        if self._job_handle is None or os.name != "nt":
            return

        # Get PID safely (may be None for mocks or failed processes)
        pid = getattr(process, "pid", None)
        if pid is None:
            return

        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32

            # Get process handle from PID
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            proc_handle = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid
            )
            if proc_handle:
                kernel32.AssignProcessToJobObject(self._job_handle, proc_handle)
                kernel32.CloseHandle(proc_handle)
        except Exception as e:
            logger.warning(f"Failed to assign process to job: {e}")

    async def _close_job_object(self) -> None:
        """Close job object (kills all assigned processes)."""
        if self._job_handle is None or os.name != "nt":
            return

        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle(self._job_handle)
            self._job_handle = None
        except Exception as e:
            logger.warning(f"Failed to close job object: {e}")

    async def _run_command(
        self,
        command: list[str],
        cwd: str | None = None,
        timeout: float = 300.0,
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
        # Create job object for this build
        self._job_handle = await self._create_job_object()

        try:
            # Never use shell=True (security)
            self._current_process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            # Assign to job object
            await self._assign_to_job(self._current_process)

            # Read output with limits
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            stdout_bytes = 0
            stderr_bytes = 0

            async def read_stream(
                stream: asyncio.StreamReader | None,
                lines: list[str],
                byte_counter: list[int],
            ) -> None:
                if stream is None:
                    return
                while True:
                    try:
                        line = await asyncio.wait_for(stream.readline(), timeout=1.0)
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace")
                        # Truncate long lines
                        if len(decoded) > MAX_OUTPUT_LINE:
                            decoded = decoded[:MAX_OUTPUT_LINE] + "...[truncated]\n"
                        lines.append(decoded)
                        byte_counter[0] += len(decoded)
                        # Drop old lines if buffer too large
                        while byte_counter[0] > MAX_OUTPUT_BYTES and lines:
                            removed = lines.pop(0)
                            byte_counter[0] -= len(removed)
                    except asyncio.TimeoutError:
                        if self._cancel_requested:
                            raise asyncio.CancelledError()
                        continue

            stdout_counter = [0]
            stderr_counter = [0]

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(
                            self._current_process.stdout, stdout_lines, stdout_counter
                        ),
                        read_stream(
                            self._current_process.stderr, stderr_lines, stderr_counter
                        ),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"Build timeout after {timeout}s")
                self._current_process.kill()
                raise

            await self._current_process.wait()
            exit_code = self._current_process.returncode or 0

            return exit_code, "".join(stdout_lines), "".join(stderr_lines)

        finally:
            self._current_process = None
            await self._close_job_object()

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
        project_path: str,
        configuration: str,
        timeout: float,
        retry_on_lock: bool,
    ) -> tuple[int, str, str]:
        """Run build command with retry logic for file lock errors.

        Args:
            cmd: Build command
            project_path: Project path
            configuration: Build configuration
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
                cmd, cwd=self._workspace_root, timeout=timeout
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
                        f"Build failed due to file locks (attempt {attempt + 1}/{MAX_BUILD_RETRIES}), "
                        f"cleaning up and retrying..."
                    )
                    # Run cleanup and retry
                    await cleanup_for_build(
                        project_path=project_path,
                        configurations=[configuration],
                    )
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
        cleanup_before_build: bool = False,
        retry_on_lock: bool = True,
    ) -> BuildResult:
        """Execute build command.

        Args:
            project_path: Path to project file or directory
            command: Build command to execute
            configuration: Build configuration
            extra_args: Additional arguments
            timeout: Timeout in seconds
            cleanup_before_build: Kill processes in output directories first
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

                # Cleanup processes before build if requested
                if cleanup_before_build:
                    killed = await cleanup_for_build(
                        project_path=validated_path,
                        configurations=[configuration],
                    )
                    if killed > 0:
                        logger.info(f"Pre-build cleanup: {killed} processes killed")

                # For rebuild, run clean first
                if command == BuildCommand.REBUILD:
                    clean_cmd = self._policy.get_dotnet_command(
                        BuildCommand.CLEAN, validated_path, configuration
                    )
                    logger.info(f"Running clean: {' '.join(clean_cmd)}")
                    exit_code, stdout, stderr = await self._run_command(
                        clean_cmd, cwd=self._workspace_root, timeout=timeout / 2
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
                    project_path=validated_path,
                    configuration=configuration,
                    timeout=timeout,
                    retry_on_lock=retry_on_lock,
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

        if self._current_process is not None:
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
        return await self.build(
            project_path, BuildCommand.CLEAN, configuration, timeout=timeout
        )

    async def restore(
        self,
        project_path: str,
        timeout: float = 300.0,
    ) -> BuildResult:
        """Restore NuGet packages."""
        return await self.build(
            project_path, BuildCommand.RESTORE, timeout=timeout
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

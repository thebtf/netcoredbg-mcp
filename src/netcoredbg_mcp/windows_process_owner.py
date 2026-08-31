"""Private, handle-backed Windows process-tree ownership.

This module is deliberately not re-exported.  A retained Job handle plus the
root process handle is the authority to drain one launched tree; a PID is only
an observation carried in ``OwnedProcessRef``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

_ADMISSION_CLEANUP_TIMEOUT = 5.0
_ACCOUNTING_POLL_SECONDS = 0.01
_INFINITE = 0xFFFFFFFF
_WAIT_FAILED = 0xFFFFFFFF
_WAIT_TIMEOUT = 258
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_RESUME_FAILED = 0xFFFFFFFF


class AdmissionStage(str, Enum):
    """The one boundary stage at which admission or drain failed."""

    CREATE_JOB = "create_job"
    SET_LIMITS = "set_limits"
    CREATE_PROCESS = "create_process"
    ASSIGN = "assign"
    VERIFY = "verify"
    WIRE_IO = "wire_io"
    RESUME = "resume"
    DRAIN = "drain"


@dataclass(frozen=True, slots=True)
class OwnedProcessRef:
    """Opaque observation for one retained private process capability."""

    owner_id: str
    generation: object
    root_pid: int


class DrainStatus(str, Enum):
    """Truthful result of one owner-only drain attempt.

    ``STALE`` rejects a mismatched capability fence without an effect. The
    other variants report retained-Job accounting.
    """

    DRAINED = "drained"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class OwnerDrainReceipt:
    """Bounded evidence captured before the owner releases its handles.

    ``forced`` records Job-wide escalation. ``root_was_forced`` separately
    records whether that escalation still included the retained root, so a
    forced descendant drain cannot relabel an already exited root as killed.
    ``None`` preserves compatibility for receipts created before that fact was
    available.
    """

    owner: OwnedProcessRef
    status: DrainStatus
    forced: bool
    root_returncode: int | None
    active_processes: int | None
    failure_stage: AdmissionStage | None = None
    winerror: int | None = None
    root_was_forced: bool | None = None


class ProcessAdmissionError(RuntimeError):
    """A Windows child was never safely admitted to its private Job."""

    def __init__(
        self,
        stage: AdmissionStage,
        owner_id: str,
        winerror: int | None = None,
    ) -> None:
        self.stage = stage
        self.owner_id = owner_id
        self.winerror = winerror
        detail = f"Windows process admission failed at {stage.value}"
        if winerror is not None:
            detail = f"{detail} (winerror {winerror})"
        super().__init__(detail)


class AdmissionCleanupError(RuntimeError):
    """Admission cleanup could not prove root exit, so controlling handles stay open."""

    def __init__(
        self,
        *,
        owner_id: str,
        admission_stage: AdmissionStage | None,
        admission_winerror: int | None,
        cleanup_stage: AdmissionStage,
        cleanup_winerror: int | None,
    ) -> None:
        self.owner_id = owner_id
        self.admission_stage = admission_stage
        self.admission_winerror = admission_winerror
        self.cleanup_stage = cleanup_stage
        self.cleanup_winerror = cleanup_winerror
        self.controlling_handles_retained = True
        detail = "Windows process admission cleanup did not confirm root exit"
        if admission_stage is not None:
            detail = f"{detail} after {admission_stage.value}"
        detail = f"{detail} at {cleanup_stage.value}"
        if cleanup_winerror is not None:
            detail = f"{detail} (winerror {cleanup_winerror})"
        super().__init__(detail)


class _Win32CallError(RuntimeError):
    """Private Win32 failure retaining its stage and Windows error code."""

    def __init__(self, stage: AdmissionStage, winerror: int | None) -> None:
        self.stage = stage
        self.winerror = winerror
        super().__init__(stage.value)


class _WindowsApi(Protocol):
    """Private direct-handle calls used by the admission boundary."""

    def create_job(self) -> int: ...

    def set_kill_on_close(self, job_handle: int) -> None: ...

    def assign_process(self, job_handle: int, process_handle: int) -> None: ...

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool: ...

    def active_processes(self, job_handle: int) -> int: ...

    def resume_thread(self, thread_handle: int) -> int: ...

    def terminate_job(self, job_handle: int) -> None: ...

    def terminate_process(self, process_handle: int) -> None: ...

    def wait_for_process(self, process_handle: int, timeout_ms: int) -> bool: ...

    def exit_code(self, process_handle: int) -> int | None: ...

    def close_handle(self, handle: int) -> None: ...


class _Kernel32:
    """Explicitly typed kernel32 calls created only by the Windows-gated owner."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        dword = wintypes.DWORD
        bool_ = wintypes.BOOL
        void_p = wintypes.LPVOID

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", dword),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", dword),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", dword),
                ("SchedulingClass", dword),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", dword),
                ("TotalProcesses", dword),
                ("ActiveProcesses", dword),
                ("TotalTerminatedProcesses", dword),
            ]

        self._extended_limit_information = ExtendedLimitInformation
        self._basic_accounting_information = BasicAccountingInformation
        self._create_job = kernel32.CreateJobObjectW
        self._create_job.argtypes = (void_p, wintypes.LPCWSTR)
        self._create_job.restype = handle
        self._set_information = kernel32.SetInformationJobObject
        self._set_information.argtypes = (handle, ctypes.c_int, void_p, dword)
        self._set_information.restype = bool_
        self._assign = kernel32.AssignProcessToJobObject
        self._assign.argtypes = (handle, handle)
        self._assign.restype = bool_
        self._is_in_job = kernel32.IsProcessInJob
        self._is_in_job.argtypes = (handle, handle, ctypes.POINTER(bool_))
        self._is_in_job.restype = bool_
        self._query_information = kernel32.QueryInformationJobObject
        self._query_information.argtypes = (handle, ctypes.c_int, void_p, dword, void_p)
        self._query_information.restype = bool_
        self._resume_thread = kernel32.ResumeThread
        self._resume_thread.argtypes = (handle,)
        self._resume_thread.restype = dword
        self._terminate_job = kernel32.TerminateJobObject
        self._terminate_job.argtypes = (handle, wintypes.UINT)
        self._terminate_job.restype = bool_
        self._terminate_process = kernel32.TerminateProcess
        self._terminate_process.argtypes = (handle, wintypes.UINT)
        self._terminate_process.restype = bool_
        self._wait_for_single_object = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = (handle, dword)
        self._wait_for_single_object.restype = dword
        self._get_exit_code = kernel32.GetExitCodeProcess
        self._get_exit_code.argtypes = (handle, ctypes.POINTER(dword))
        self._get_exit_code.restype = bool_
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = (handle,)
        self._close_handle.restype = bool_

    def _error(self, stage: AdmissionStage) -> _Win32CallError:
        return _Win32CallError(stage, self._ctypes.get_last_error() or None)

    def create_job(self) -> int:
        handle = self._create_job(None, None)
        if not handle:
            raise self._error(AdmissionStage.CREATE_JOB)
        return int(handle)

    def set_kill_on_close(self, job_handle: int) -> None:
        info = self._extended_limit_information()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._set_information(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        ):
            raise self._error(AdmissionStage.SET_LIMITS)

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if not self._assign(job_handle, process_handle):
            raise self._error(AdmissionStage.ASSIGN)

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool:
        result = self._wintypes.BOOL()
        if not self._is_in_job(process_handle, job_handle, self._ctypes.byref(result)):
            raise self._error(AdmissionStage.VERIFY)
        return bool(result.value)

    def active_processes(self, job_handle: int) -> int:
        info = self._basic_accounting_information()
        if not self._query_information(
            job_handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        ):
            raise self._error(AdmissionStage.VERIFY)
        return int(info.ActiveProcesses)

    def resume_thread(self, thread_handle: int) -> int:
        result = int(self._resume_thread(thread_handle))
        if result == _RESUME_FAILED:
            raise self._error(AdmissionStage.RESUME)
        return result

    def terminate_job(self, job_handle: int) -> None:
        if not self._terminate_job(job_handle, 1):
            raise self._error(AdmissionStage.DRAIN)

    def terminate_process(self, process_handle: int) -> None:
        if not self._terminate_process(process_handle, 1):
            raise self._error(AdmissionStage.DRAIN)

    def wait_for_process(self, process_handle: int, timeout_ms: int) -> bool:
        result = int(self._wait_for_single_object(process_handle, timeout_ms))
        if result == _WAIT_FAILED:
            raise self._error(AdmissionStage.DRAIN)
        return result != _WAIT_TIMEOUT

    def exit_code(self, process_handle: int) -> int | None:
        wait_result = int(self._wait_for_single_object(process_handle, 0))
        if wait_result == _WAIT_FAILED:
            raise self._error(AdmissionStage.DRAIN)
        if wait_result == _WAIT_TIMEOUT:
            return None
        value = self._wintypes.DWORD()
        if not self._get_exit_code(process_handle, self._ctypes.byref(value)):
            raise self._error(AdmissionStage.DRAIN)
        return int(value.value)

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise self._error(AdmissionStage.DRAIN)


class _WritePipeProtocol(asyncio.streams.FlowControlMixin):
    """The stdlib flow-control protocol needed by an IOCP StreamWriter."""


class _PipeEnds:
    """Child and parent pipe handles with one explicit transfer of ownership."""

    def __init__(
        self,
        *,
        stdin_child: int,
        stdin_parent: int | None,
        stdout_parent: int,
        stdout_child: int,
        stderr_parent: int,
        stderr_child: int,
        devnull_fd: int | None = None,
    ) -> None:
        self.stdin_child: int = stdin_child
        self.stdin_parent: int | None = stdin_parent
        self.stdout_parent: int | None = stdout_parent
        self.stdout_child: int = stdout_child
        self.stderr_parent: int | None = stderr_parent
        self.stderr_child: int = stderr_child
        self.devnull_fd: int | None = devnull_fd

    @classmethod
    def create(cls, stdin_mode: Literal["pipe", "devnull"]) -> _PipeEnds:
        from asyncio import windows_utils

        stdin_child: int | None = None
        stdin_parent: int | None = None
        stdout_parent: int | None = None
        stdout_child: int | None = None
        stderr_parent: int | None = None
        stderr_child: int | None = None
        devnull_fd: int | None = None
        try:
            if stdin_mode == "pipe":
                stdin_child, stdin_parent = windows_utils.pipe(
                    duplex=True,
                    overlapped=(False, True),
                )
            else:
                import msvcrt

                devnull_fd = os.open(os.devnull, os.O_RDONLY)
                stdin_child = msvcrt.get_osfhandle(devnull_fd)
            stdout_parent, stdout_child = windows_utils.pipe(overlapped=(True, False))
            stderr_parent, stderr_child = windows_utils.pipe(overlapped=(True, False))
            for child_handle in (stdin_child, stdout_child, stderr_child):
                os.set_handle_inheritable(int(child_handle), True)
            for parent_handle in (stdin_parent, stdout_parent, stderr_parent):
                if parent_handle is not None:
                    os.set_handle_inheritable(int(parent_handle), False)
            return cls(
                stdin_child=int(stdin_child),
                stdin_parent=None if stdin_parent is None else int(stdin_parent),
                stdout_parent=int(stdout_parent),
                stdout_child=int(stdout_child),
                stderr_parent=int(stderr_parent),
                stderr_child=int(stderr_child),
                devnull_fd=devnull_fd,
            )
        except BaseException:
            if devnull_fd is not None:
                try:
                    os.close(devnull_fd)
                except OSError:
                    pass
                stdin_child = None
            closed_handles: set[int] = set()
            for handle in (
                stdin_child,
                stdin_parent,
                stdout_parent,
                stdout_child,
                stderr_parent,
                stderr_child,
            ):
                if handle is None or int(handle) in closed_handles:
                    continue
                closed_handles.add(int(handle))
                _close_raw_handle(int(handle))
            raise

    def handle_list(self) -> list[int]:
        # This is the complete inherited-handle list.  Job, process, and
        # primary-thread handles are never available to the child.
        return [self.stdin_child, self.stdout_child, self.stderr_child]

    def close_child_ends(self, api: _WindowsApi) -> None:
        if self.stdin_child:
            if self.devnull_fd is not None:
                os.close(self.devnull_fd)
                self.devnull_fd = None
            else:
                _close_ignoring_errors(api, self.stdin_child)
            self.stdin_child = 0
        for name in ("stdout_child", "stderr_child"):
            handle = getattr(self, name)
            if handle:
                _close_ignoring_errors(api, handle)
                setattr(self, name, 0)

    def close_unwired(self, api: _WindowsApi) -> None:
        self.close_child_ends(api)
        for name in ("stdin_parent", "stdout_parent", "stderr_parent"):
            handle = getattr(self, name)
            if handle is not None:
                _close_ignoring_errors(api, handle)
                setattr(self, name, None)

    async def wire(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[
        asyncio.StreamWriter | None,
        asyncio.StreamReader,
        asyncio.StreamReader,
        tuple[asyncio.BaseTransport, ...],
    ]:
        from asyncio.windows_utils import PipeHandle

        transports: list[asyncio.BaseTransport] = []
        try:
            stdin: asyncio.StreamWriter | None = None
            if self.stdin_parent is not None:
                handle = PipeHandle(self.stdin_parent)
                self.stdin_parent = None
                protocol = _WritePipeProtocol(loop=loop)
                transport, _ = await loop.connect_write_pipe(lambda: protocol, handle)
                transports.append(transport)
                stdin = asyncio.StreamWriter(transport, protocol, None, loop)

            stdout = asyncio.StreamReader()
            assert self.stdout_parent is not None
            stdout_handle = PipeHandle(self.stdout_parent)
            self.stdout_parent = None
            stdout_protocol = asyncio.StreamReaderProtocol(stdout, loop=loop)
            stdout_transport, _ = await loop.connect_read_pipe(
                lambda: stdout_protocol,
                stdout_handle,
            )
            transports.append(stdout_transport)

            stderr = asyncio.StreamReader()
            assert self.stderr_parent is not None
            stderr_handle = PipeHandle(self.stderr_parent)
            self.stderr_parent = None
            stderr_protocol = asyncio.StreamReaderProtocol(stderr, loop=loop)
            stderr_transport, _ = await loop.connect_read_pipe(
                lambda: stderr_protocol,
                stderr_handle,
            )
            transports.append(stderr_transport)
            return stdin, stdout, stderr, tuple(transports)
        except BaseException:
            for owned_transport in transports:
                owned_transport.close()
            raise


def _close_ignoring_errors(api: _WindowsApi, handle: int) -> None:
    try:
        api.close_handle(handle)
    except _Win32CallError:
        pass


def _close_raw_handle(handle: int) -> None:
    """Release a pipe handle before a `_PipeEnds` instance can own it."""

    try:
        import _winapi

        _winapi.CloseHandle(handle)
    except (AttributeError, OSError):
        pass


def _make_non_inheritable(handle: int, stage: AdmissionStage) -> None:
    try:
        os.set_handle_inheritable(handle, False)
    except OSError as error:
        raise _Win32CallError(stage, error.winerror) from error


def _resolve_application_name(
    executable: str,
    environment: Mapping[str, str] | None,
) -> str:
    """Resolve a bare executable before passing it as lpApplicationName."""

    if os.path.dirname(executable):
        return os.path.abspath(executable)

    search_path: str | None = None
    if environment is not None:
        for name, value in environment.items():
            if name.casefold() == "path":
                search_path = value
                break
        if not search_path:
            raise _Win32CallError(AdmissionStage.CREATE_PROCESS, None)

    resolved = shutil.which(executable, path=search_path)
    if resolved is None:
        raise _Win32CallError(AdmissionStage.CREATE_PROCESS, None)
    return os.path.abspath(resolved)


def _create_suspended_process(
    *,
    argv: Sequence[str],
    cwd: str | None,
    env: Mapping[str, str] | None,
    pipe_ends: _PipeEnds,
) -> tuple[int, int, int]:
    import _winapi

    if not argv:
        raise _Win32CallError(AdmissionStage.CREATE_PROCESS, None)
    application_name = _resolve_application_name(os.fspath(argv[0]), env)
    command_line = subprocess.list2cmdline([os.fspath(item) for item in argv])
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESTDHANDLES
    startup_info.hStdInput = pipe_ends.stdin_child
    startup_info.hStdOutput = pipe_ends.stdout_child
    startup_info.hStdError = pipe_ends.stderr_child
    startup_info.lpAttributeList = {"handle_list": pipe_ends.handle_list()}
    environment = dict(os.environ if env is None else env)
    try:
        process_handle, thread_handle, process_id, _thread_id = _winapi.CreateProcess(
            application_name,
            command_line,
            None,
            None,
            True,
            _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT,
            environment,
            cwd,
            startup_info,
        )
    except OSError as error:
        raise _Win32CallError(AdmissionStage.CREATE_PROCESS, error.winerror) from error
    return int(process_handle), int(thread_handle), int(process_id)


class WindowsOwnedProcess:
    """One private admitted Windows process capability and its async stdio.

    The retained Job and root handles are never reconstructed from a PID.  The
    child remains suspended until assignment, membership/accounting validation,
    and parent I/O wiring all succeeded.
    """

    def __init__(
        self,
        *,
        owner: OwnedProcessRef,
        api: _WindowsApi,
        job_handle: int,
        process_handle: int,
        stdin: asyncio.StreamWriter | None,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        transports: tuple[asyncio.BaseTransport, ...],
    ) -> None:
        self.owner = owner
        self._api = api
        self._job_handle: int | None = job_handle
        self._process_handle: int | None = process_handle
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._transports = transports
        self._returncode: int | None = None
        self._drain_task: asyncio.Task[OwnerDrainReceipt] | None = None
        self._drain_receipt: OwnerDrainReceipt | None = None
        self._closed = False

    @property
    def pid(self) -> int:
        return self.owner.root_pid

    @property
    def returncode(self) -> int | None:
        if self._returncode is not None or self._process_handle is None:
            return self._returncode
        try:
            self._returncode = self._api.exit_code(self._process_handle)
        except _Win32CallError:
            return None
        return self._returncode

    @classmethod
    async def launch(
        cls,
        *,
        generation: object,
        argv: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin_mode: Literal["pipe", "devnull"],
    ) -> WindowsOwnedProcess:
        """Create a suspended child and return only after Job admission succeeds."""

        if os.name != "nt":
            raise RuntimeError("WindowsOwnedProcess is available only on Windows")
        return await cls._launch_with(
            generation=generation,
            argv=argv,
            cwd=cwd,
            env=env,
            stdin_mode=stdin_mode,
            api=_Kernel32(),
            pipe_ends=None,
            process_creator=_create_suspended_process,
        )

    @classmethod
    async def _launch_with(
        cls,
        *,
        generation: object,
        argv: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin_mode: Literal["pipe", "devnull"],
        api: _WindowsApi,
        pipe_ends: _PipeEnds | None,
        process_creator: Any,
    ) -> WindowsOwnedProcess:
        """Private injection seam for deterministic admission-order coverage."""

        owner_id = uuid.uuid4().hex
        job_handle: int | None = None
        process_handle: int | None = None
        thread_handle: int | None = None
        endpoints = pipe_ends
        admitted = False
        transports: tuple[asyncio.BaseTransport, ...] = ()
        try:
            job_handle = api.create_job()
            _make_non_inheritable(job_handle, AdmissionStage.CREATE_JOB)
            api.set_kill_on_close(job_handle)
            endpoints = endpoints or _PipeEnds.create(stdin_mode)
            process_handle, thread_handle, process_id = process_creator(
                argv=argv,
                cwd=cwd,
                env=env,
                pipe_ends=endpoints,
            )
            _make_non_inheritable(process_handle, AdmissionStage.CREATE_PROCESS)
            _make_non_inheritable(thread_handle, AdmissionStage.CREATE_PROCESS)
            endpoints.close_child_ends(api)
            api.assign_process(job_handle, process_handle)
            admitted = True
            if not api.is_process_in_job(process_handle, job_handle):
                raise _Win32CallError(AdmissionStage.VERIFY, None)
            api.active_processes(job_handle)
            try:
                stdin, stdout, stderr, transports = await endpoints.wire(asyncio.get_running_loop())
            except _Win32CallError:
                raise
            except BaseException as error:
                raise _Win32CallError(
                    AdmissionStage.WIRE_IO,
                    getattr(error, "winerror", None),
                ) from error
            # Resume is last.  Every capability-defining fact above is true
            # before any adapter code can execute in the child process.
            api.resume_thread(thread_handle)
            _close_ignoring_errors(api, thread_handle)
            thread_handle = None
            owner = OwnedProcessRef(owner_id=owner_id, generation=generation, root_pid=process_id)
            return cls(
                owner=owner,
                api=api,
                job_handle=job_handle,
                process_handle=process_handle,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                transports=transports,
            )
        except _Win32CallError as error:
            try:
                await _cleanup_failed_admission(
                    api=api,
                    owner_id=owner_id,
                    admission_stage=error.stage,
                    admission_winerror=error.winerror,
                    job_handle=job_handle,
                    process_handle=process_handle,
                    thread_handle=thread_handle,
                    pipe_ends=endpoints,
                    transports=transports,
                    admitted=admitted,
                )
            except AdmissionCleanupError as cleanup_error:
                raise cleanup_error from error
            raise ProcessAdmissionError(error.stage, owner_id, error.winerror) from error
        except BaseException as error:
            try:
                await _cleanup_failed_admission(
                    api=api,
                    owner_id=owner_id,
                    admission_stage=None,
                    admission_winerror=None,
                    job_handle=job_handle,
                    process_handle=process_handle,
                    thread_handle=thread_handle,
                    pipe_ends=endpoints,
                    transports=transports,
                    admitted=admitted,
                )
            except AdmissionCleanupError as cleanup_error:
                raise cleanup_error from error
            raise

    async def wait(self) -> int:
        """Match the small process-like surface consumed by the DAP observer."""

        return await self.wait_root()

    async def wait_root(self) -> int:
        """Wait for the retained root handle without reopening a PID."""

        if self._returncode is not None:
            return self._returncode
        handle = self._process_handle
        if handle is None:
            return 0
        await asyncio.to_thread(self._api.wait_for_process, handle, _INFINITE)
        result = self._api.exit_code(handle)
        if result is None:
            raise RuntimeError("signaled Windows process has no exit code")
        self._returncode = result
        return result

    def _query_active_processes(self) -> int:
        """Private accounting probe used by the drain and controlled fixture."""

        if self._job_handle is None:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        return self._api.active_processes(self._job_handle)

    def _root_is_active_before_force(self) -> bool | None:
        """Observe whether the retained root is still active before Job force."""

        if self._returncode is not None:
            return False
        handle = self._process_handle
        if handle is None:
            return None
        try:
            returncode = self._api.exit_code(handle)
        except _Win32CallError:
            return None
        if returncode is None:
            return True
        self._returncode = returncode
        return False

    async def drain_after_grace(
        self,
        *,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt:
        """Join one grace-then-Job-force operation for this capability."""

        return await self._join_drain(grace_timeout, force_timeout)

    async def force_and_drain(self, *, timeout: float) -> OwnerDrainReceipt:
        """Join one immediate Job-force accounting drain for this capability."""

        return await self._join_drain(0.0, timeout)

    async def _join_drain(
        self,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt:
        if (
            self._drain_receipt is not None
            and self._drain_receipt.status is DrainStatus.DRAINED
            and self._drain_receipt.active_processes == 0
        ):
            return self._drain_receipt
        task = self._drain_task
        if task is None or task.done():
            self._drain_receipt = None
            task = asyncio.create_task(self._drain(grace_timeout, force_timeout))
            self._drain_task = task
        return await asyncio.shield(task)

    async def _drain(self, grace_timeout: float, force_timeout: float) -> OwnerDrainReceipt:
        graceful = await self._wait_for_zero(
            grace_timeout,
            forced=False,
            root_was_forced=False,
        )
        if graceful.status is DrainStatus.DRAINED:
            self._drain_receipt = graceful
            return graceful
        if graceful.status is DrainStatus.FAILED:
            self._drain_receipt = graceful
            return graceful
        if self._job_handle is None:
            receipt = self._receipt(
                status=DrainStatus.FAILED,
                forced=False,
                active_processes=None,
                failure_stage=AdmissionStage.DRAIN,
                winerror=None,
            )
            self._drain_receipt = receipt
            return receipt
        root_was_forced = self._root_is_active_before_force()
        try:
            self._api.terminate_job(self._job_handle)
        except _Win32CallError as error:
            receipt = self._receipt(
                status=DrainStatus.FAILED,
                forced=True,
                active_processes=None,
                failure_stage=error.stage,
                winerror=error.winerror,
            )
            self._drain_receipt = receipt
            return receipt
        forced = await self._wait_for_zero(
            force_timeout,
            forced=True,
            root_was_forced=root_was_forced,
        )
        self._drain_receipt = forced
        return forced

    async def _wait_for_zero(
        self,
        timeout: float,
        *,
        forced: bool,
        root_was_forced: bool | None = None,
    ) -> OwnerDrainReceipt:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                active_processes = self._query_active_processes()
            except _Win32CallError as error:
                return self._receipt(
                    status=DrainStatus.FAILED,
                    forced=forced,
                    active_processes=None,
                    failure_stage=error.stage,
                    winerror=error.winerror,
                    root_was_forced=root_was_forced,
                )
            if active_processes == 0:
                try:
                    await self.wait_root()
                except (_Win32CallError, RuntimeError):
                    pass
                # Job accounting, not root exit or KILL_ON_JOB_CLOSE, is the
                # success fact. A DRAINED receipt always records literal zero.
                return self._receipt(
                    status=DrainStatus.DRAINED,
                    forced=forced,
                    active_processes=0,
                    failure_stage=None,
                    winerror=None,
                    root_was_forced=root_was_forced,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._receipt(
                    status=DrainStatus.TIMED_OUT,
                    forced=forced,
                    active_processes=active_processes,
                    failure_stage=None,
                    winerror=None,
                    root_was_forced=root_was_forced,
                )
            await asyncio.sleep(min(_ACCOUNTING_POLL_SECONDS, remaining))

    def _receipt(
        self,
        *,
        status: DrainStatus,
        forced: bool,
        active_processes: int | None,
        failure_stage: AdmissionStage | None,
        winerror: int | None,
        root_was_forced: bool | None = None,
    ) -> OwnerDrainReceipt:
        return OwnerDrainReceipt(
            owner=self.owner,
            status=status,
            forced=forced,
            root_returncode=self.returncode,
            active_processes=active_processes,
            failure_stage=failure_stage,
            winerror=winerror,
            root_was_forced=root_was_forced,
        )

    async def aclose(self) -> OwnerDrainReceipt:
        """Close resources and return the last truthful owner-drain receipt."""

        if self._closed:
            if self._drain_receipt is not None:
                return self._drain_receipt
            return self._receipt(
                status=DrainStatus.FAILED,
                forced=False,
                active_processes=None,
                failure_stage=AdmissionStage.DRAIN,
                winerror=None,
            )
        receipt = self._drain_receipt
        if (
            receipt is None
            or receipt.status is not DrainStatus.DRAINED
            or receipt.active_processes != 0
        ):
            receipt = await self.force_and_drain(timeout=_ADMISSION_CLEANUP_TIMEOUT)
        if self.stdin is not None:
            self.stdin.close()
        for transport in self._transports:
            transport.close()
        self._transports = ()
        if self._process_handle is not None:
            _close_ignoring_errors(self._api, self._process_handle)
            self._process_handle = None
        if self._job_handle is not None:
            _close_ignoring_errors(self._api, self._job_handle)
            self._job_handle = None
        self._closed = True
        return receipt


async def _cleanup_failed_admission(
    *,
    api: _WindowsApi,
    owner_id: str,
    admission_stage: AdmissionStage | None,
    admission_winerror: int | None,
    job_handle: int | None,
    process_handle: int | None,
    thread_handle: int | None,
    pipe_ends: _PipeEnds | None,
    transports: tuple[asyncio.BaseTransport, ...],
    admitted: bool,
) -> None:
    """Release admission resources only after the retained root exit is confirmed."""

    def cleanup_failure(error: _Win32CallError | None = None) -> AdmissionCleanupError:
        return AdmissionCleanupError(
            owner_id=owner_id,
            admission_stage=admission_stage,
            admission_winerror=admission_winerror,
            cleanup_stage=AdmissionStage.DRAIN if error is None else error.stage,
            cleanup_winerror=None if error is None else error.winerror,
        )

    for transport in transports:
        transport.close()
    if pipe_ends is not None:
        pipe_ends.close_unwired(api)
    if process_handle is None:
        if thread_handle is not None:
            _close_ignoring_errors(api, thread_handle)
        if job_handle is not None:
            _close_ignoring_errors(api, job_handle)
        return

    try:
        api.terminate_process(process_handle)
    except _Win32CallError as error:
        if not admitted or job_handle is None:
            raise cleanup_failure(error)
    if admitted:
        if job_handle is None:
            raise cleanup_failure()
        try:
            api.terminate_job(job_handle)
        except _Win32CallError as error:
            raise cleanup_failure(error)
    try:
        root_exited = await asyncio.to_thread(
            api.wait_for_process,
            process_handle,
            int(_ADMISSION_CLEANUP_TIMEOUT * 1000),
        )
    except _Win32CallError as error:
        raise cleanup_failure(error)
    if not root_exited:
        raise cleanup_failure()
    if thread_handle is not None:
        _close_ignoring_errors(api, thread_handle)
    _close_ignoring_errors(api, process_handle)
    if job_handle is not None:
        _close_ignoring_errors(api, job_handle)

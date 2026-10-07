"""Private, handle-backed Windows process-tree ownership.

This module is deliberately not re-exported.  A retained Job handle plus the
root process handle is the authority to drain one launched tree; a PID is only
an observation carried in ``OwnedProcessRef``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum
from functools import partial
from typing import Any, Literal, Protocol

_ADMISSION_CLEANUP_TIMEOUT = 5.0
_ACCOUNTING_POLL_SECONDS = 0.01
_FAILED_ADMISSION_REAPER_INITIAL_BACKOFF_SECONDS = 0.05
_FAILED_ADMISSION_REAPER_MAX_BACKOFF_SECONDS = 1.0
_INFINITE = 0xFFFFFFFF
_WAIT_TIMEOUT = 258
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION = 7
_JOB_OBJECT_MSG_NEW_PROCESS = 6
_JOB_OBJECT_MSG_EXIT_PROCESS = 7
_JOB_OBJECT_MSG_ABNORMAL_EXIT_PROCESS = 8
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_DEBUG_PROCESS = 0x00000001
_DBG_CONTINUE = 0x00010002
_DBG_EXCEPTION_NOT_HANDLED = 0x80010001
_EXCEPTION_DEBUG_EVENT = 1
_CREATE_PROCESS_DEBUG_EVENT = 3
_EXIT_PROCESS_DEBUG_EVENT = 5
_LOAD_DLL_DEBUG_EVENT = 6
_RIP_EVENT = 9
_EXCEPTION_BREAKPOINT = 0x80000003
_DEBUG_WAIT_MS = 50
_RESUME_FAILED = 0xFFFFFFFF
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_ERROR_MORE_DATA = 234
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x100000
_INVALID_HANDLE_VALUE = -1
_MAX_JOB_MEMBERS = 65536


logger = logging.getLogger(__name__)


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
    other variants report Job accounting and retained member-handle liveness.
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
    """Admission cleanup failed while a private reaper retains the controlling handles."""

    def __init__(
        self,
        *,
        owner_id: str,
        admission_stage: AdmissionStage | None,
        admission_winerror: int | None,
        cleanup_stage: AdmissionStage,
        cleanup_winerror: int | None,
        reaper: _FailedAdmissionReaper,
    ) -> None:
        self.owner_id = owner_id
        self.admission_stage = admission_stage
        self.admission_winerror = admission_winerror
        self.cleanup_stage = cleanup_stage
        self.cleanup_winerror = cleanup_winerror
        self._reaper = reaper
        self.controlling_handles_retained = not reaper.closed
        detail = "Windows process admission cleanup did not confirm root exit"
        if admission_stage is not None:
            detail = f"{detail} after {admission_stage.value}"
        detail = f"{detail} at {cleanup_stage.value}"
        if cleanup_winerror is not None:
            detail = f"{detail} (winerror {cleanup_winerror})"
        super().__init__(detail)

    async def wait_for_cleanup(self, timeout: float) -> bool:
        """Return whether the retained-owner reaper closes within ``timeout`` seconds."""
        return await self._reaper.wait_for_completion(timeout)


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
    def create_completion_port(self) -> int: ...

    def attach_completion_port(self, job_handle: int, port_handle: int) -> None: ...

    def job_messages(self, port_handle: int) -> tuple[tuple[int, int], ...]: ...

    def assign_process(self, job_handle: int, process_handle: int) -> None: ...

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool: ...

    def active_processes(self, job_handle: int) -> int: ...
    def total_processes(self, job_handle: int) -> int: ...

    def member_process_ids(self, job_handle: int) -> tuple[int, ...]: ...

    def open_job_member(self, job_handle: int, pid: int) -> int | None: ...

    def resume_thread(self, thread_handle: int) -> int: ...

    def terminate_job(self, job_handle: int) -> None: ...

    def terminate_process(self, process_handle: int) -> None: ...

    def wait_for_process(self, process_handle: int, timeout_ms: int) -> bool: ...

    def exit_code(self, process_handle: int) -> int | None: ...

    def close_handle(self, handle: int) -> None: ...

    def enable_debug_capture(self) -> None: ...
    def wait_debug_event(self, timeout_ms: int) -> Any | None: ...
    def continue_debug_event(self, event: Any, status: int) -> None: ...
    def duplicate_process(self, handle: int) -> int: ...
    def same_process(self, first: int, second: int) -> bool: ...
    def process_id(self, handle: int) -> int: ...
    def is_startup_breakpoint(self, event: Any) -> bool: ...


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

        class BasicProcessIdList(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", dword),
                ("NumberOfProcessIdsInList", dword),
                ("ProcessIdList", ctypes.c_size_t * 1),
            ]

        class AssociateCompletionPort(ctypes.Structure):
            _fields_ = [("CompletionKey", void_p), ("CompletionPort", handle)]

        self._extended_limit_information = ExtendedLimitInformation
        self._basic_accounting_information = BasicAccountingInformation
        self._basic_process_id_list = BasicProcessIdList
        self._associate_completion_port = AssociateCompletionPort
        self._completion_key: int | None = None
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
        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = (dword, bool_, dword)
        self._open_process.restype = handle
        self._create_completion_port = kernel32.CreateIoCompletionPort
        self._create_completion_port.argtypes = (handle, handle, ctypes.c_size_t, dword)
        self._create_completion_port.restype = handle
        self._get_queued_completion_status = kernel32.GetQueuedCompletionStatus
        self._get_queued_completion_status.argtypes = (
            handle,
            ctypes.POINTER(dword),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(void_p),
            dword,
        )
        self._get_queued_completion_status.restype = bool_
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

    def create_completion_port(self) -> int:
        port = self._create_completion_port(
            self._ctypes.c_void_p(_INVALID_HANDLE_VALUE), None, 0, 1
        )
        if not port:
            raise self._error(AdmissionStage.CREATE_JOB)
        return int(port)

    def attach_completion_port(self, job_handle: int, port_handle: int) -> None:
        info = self._associate_completion_port(job_handle, port_handle)
        if not self._set_information(
            job_handle,
            _JOB_OBJECT_ASSOCIATE_COMPLETION_PORT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        ):
            raise self._error(AdmissionStage.SET_LIMITS)
        self._completion_key = job_handle

    def job_messages(self, port_handle: int) -> tuple[tuple[int, int], ...]:
        messages: list[tuple[int, int]] = []
        for _ in range(_MAX_JOB_MEMBERS):
            code = self._wintypes.DWORD()
            key = self._ctypes.c_size_t()
            pid = self._ctypes.c_void_p()
            if not self._get_queued_completion_status(
                port_handle,
                self._ctypes.byref(code),
                self._ctypes.byref(key),
                self._ctypes.byref(pid),
                0,
            ):
                error = self._ctypes.get_last_error()
                if error == _WAIT_TIMEOUT:
                    return tuple(messages)
                raise _Win32CallError(AdmissionStage.DRAIN, error or None)
            if key.value != self._completion_key:
                raise _Win32CallError(AdmissionStage.DRAIN, None)
            messages.append((code.value, pid.value or 0))
        raise _Win32CallError(AdmissionStage.DRAIN, None)

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

    def total_processes(self, job_handle: int) -> int:
        info = self._basic_accounting_information()
        if not self._query_information(
            job_handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        ):
            raise self._error(AdmissionStage.DRAIN)
        return int(info.TotalProcesses)

    def member_process_ids(self, job_handle: int) -> tuple[int, ...]:
        capacity = 8
        header = self._basic_process_id_list
        while capacity <= _MAX_JOB_MEMBERS:
            storage = self._ctypes.create_string_buffer(
                header.ProcessIdList.offset + capacity * self._ctypes.sizeof(self._ctypes.c_size_t)
            )
            result = self._query_information(
                job_handle,
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                storage,
                self._ctypes.sizeof(storage),
                None,
            )
            info = self._ctypes.cast(storage, self._ctypes.POINTER(header)).contents
            if result and info.NumberOfAssignedProcesses <= capacity:
                count = info.NumberOfProcessIdsInList
                if count > capacity or count < info.NumberOfAssignedProcesses:
                    raise self._error(AdmissionStage.DRAIN)
                identifiers = (self._ctypes.c_size_t * count).from_buffer(
                    storage, header.ProcessIdList.offset
                )
                return tuple(identifiers)
            error = self._ctypes.get_last_error()
            if not result and error != _ERROR_MORE_DATA:
                raise _Win32CallError(AdmissionStage.DRAIN, error or None)
            capacity = max(capacity * 2, info.NumberOfAssignedProcesses)
        raise _Win32CallError(AdmissionStage.DRAIN, _ERROR_MORE_DATA)

    def open_job_member(self, job_handle: int, pid: int) -> int | None:
        handle = self._open_process(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise self._error(AdmissionStage.DRAIN)
        process_handle = int(handle)
        retained = False
        try:
            retained = self.is_process_in_job(process_handle, job_handle)
            if retained:
                return process_handle
            if not self.wait_for_process(process_handle, 0):
                raise _Win32CallError(AdmissionStage.DRAIN, None)
            return None
        except _Win32CallError as error:
            raise _Win32CallError(AdmissionStage.DRAIN, error.winerror) from error
        finally:
            if not retained:
                _close_ignoring_errors(self, process_handle)

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
        if result not in (0, _WAIT_TIMEOUT):
            raise self._error(AdmissionStage.DRAIN)
        return result == 0

    def exit_code(self, process_handle: int) -> int | None:
        wait_result = int(self._wait_for_single_object(process_handle, 0))
        if wait_result == _WAIT_TIMEOUT:
            return None
        if wait_result != 0:
            raise self._error(AdmissionStage.DRAIN)
        value = self._wintypes.DWORD()
        if not self._get_exit_code(process_handle, self._ctypes.byref(value)):
            raise self._error(AdmissionStage.DRAIN)
        return int(value.value)

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(handle):
            raise self._error(AdmissionStage.DRAIN)

    def enable_debug_capture(self) -> None:
        """Bind debug interop only for the collector's opt-in native debug chain."""
        ctypes, wintypes = self._ctypes, self._wintypes
        handle, dword, pointer = wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID

        class ExceptionRecord(ctypes.Structure):
            _fields_ = [
                ("ExceptionCode", dword),
                ("ExceptionFlags", dword),
                ("ExceptionRecord", pointer),
                ("ExceptionAddress", pointer),
                ("NumberParameters", dword),
                ("ExceptionInformation", ctypes.c_size_t * 15),
            ]

        class ExceptionInfo(ctypes.Structure):
            _fields_ = [("ExceptionRecord", ExceptionRecord), ("dwFirstChance", dword)]

        class CreateProcessInfo(ctypes.Structure):
            _fields_ = [
                ("hFile", handle),
                ("hProcess", handle),
                ("hThread", handle),
                ("lpBaseOfImage", pointer),
                ("dwDebugInfoFileOffset", dword),
                ("nDebugInfoSize", dword),
                ("lpThreadLocalBase", pointer),
                ("lpStartAddress", pointer),
                ("lpImageName", pointer),
                ("fUnicode", wintypes.WORD),
            ]

        class LoadDllInfo(ctypes.Structure):
            _fields_ = [
                ("hFile", handle),
                ("lpBaseOfDll", pointer),
                ("dwDebugInfoFileOffset", dword),
                ("nDebugInfoSize", dword),
                ("lpImageName", pointer),
                ("fUnicode", wintypes.WORD),
            ]

        class DebugInfo(ctypes.Union):
            _fields_ = [
                ("Exception", ExceptionInfo),
                ("CreateProcessInfo", CreateProcessInfo),
                ("LoadDll", LoadDllInfo),
            ]

        class DebugEvent(ctypes.Structure):
            _fields_ = [
                ("dwDebugEventCode", dword),
                ("dwProcessId", dword),
                ("dwThreadId", dword),
                ("u", DebugInfo),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._debug_event = DebugEvent
        self._wait_debug_event = kernel32.WaitForDebugEventEx
        self._wait_debug_event.argtypes = (ctypes.POINTER(DebugEvent), dword)
        self._wait_debug_event.restype = wintypes.BOOL
        self._continue_debug_event = kernel32.ContinueDebugEvent
        self._continue_debug_event.argtypes = (dword, dword, dword)
        self._continue_debug_event.restype = wintypes.BOOL
        self._duplicate_handle = kernel32.DuplicateHandle
        self._duplicate_handle.argtypes = (
            handle,
            handle,
            handle,
            ctypes.POINTER(handle),
            dword,
            wintypes.BOOL,
            dword,
        )
        self._duplicate_handle.restype = wintypes.BOOL
        self._compare_handles = ctypes.WinDLL(
            "kernelbase", use_last_error=True
        ).CompareObjectHandles
        self._compare_handles.argtypes = (handle, handle)
        self._compare_handles.restype = wintypes.BOOL
        self._get_process_id = kernel32.GetProcessId
        self._get_process_id.argtypes = (handle,)
        self._get_process_id.restype = dword
        ntdll = ctypes.WinDLL("ntdll")
        self._startup_breakpoint = ctypes.cast(ntdll.DbgBreakPoint, pointer).value

    def wait_debug_event(self, timeout_ms: int) -> Any | None:
        event = self._debug_event()
        if self._wait_debug_event(self._ctypes.byref(event), timeout_ms):
            return event
        error = self._ctypes.get_last_error()
        if error == 121:  # ERROR_SEM_TIMEOUT, not an IOCP WAIT_TIMEOUT.
            return None
        raise _Win32CallError(AdmissionStage.DRAIN, error or None)

    def continue_debug_event(self, event: Any, status: int) -> None:
        if not self._continue_debug_event(event.dwProcessId, event.dwThreadId, status):
            raise self._error(AdmissionStage.DRAIN)

    def duplicate_process(self, handle: int) -> int:
        duplicate = self._wintypes.HANDLE()
        current = self._ctypes.c_void_p(-1)
        if not handle or not self._duplicate_handle(
            current,
            handle,
            current,
            self._ctypes.byref(duplicate),
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            0,
        ):
            raise self._error(AdmissionStage.DRAIN)
        if duplicate.value is None:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        return int(duplicate.value)

    def same_process(self, first: int, second: int) -> bool:
        self._ctypes.set_last_error(0)
        result = bool(self._compare_handles(first, second))
        error = self._ctypes.get_last_error()
        if not result and error not in (0, 1656):  # ERROR_NOT_SAME_OBJECT.
            raise _Win32CallError(AdmissionStage.DRAIN, error)
        return result

    def process_id(self, handle: int) -> int:
        pid = int(self._get_process_id(handle))
        if not pid:
            raise self._error(AdmissionStage.DRAIN)
        return pid

    def is_startup_breakpoint(self, event: Any) -> bool:
        info = event.u.Exception
        return (
            info.dwFirstChance == 1
            and info.ExceptionRecord.ExceptionCode == _EXCEPTION_BREAKPOINT
            and info.ExceptionRecord.ExceptionAddress == self._startup_breakpoint
        )


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

    def close_child_ends(self, api: _WindowsApi, *, strict: bool = False) -> None:
        if self.stdin_child:
            if self.devnull_fd is not None:
                os.close(self.devnull_fd)
                self.devnull_fd = None
            else:
                (api.close_handle if strict else partial(_close_ignoring_errors, api))(
                    self.stdin_child
                )
            self.stdin_child = 0
        for name in ("stdout_child", "stderr_child"):
            handle = getattr(self, name)
            if handle:
                (api.close_handle if strict else partial(_close_ignoring_errors, api))(handle)
                setattr(self, name, 0)

    def close_unwired(self, api: _WindowsApi, *, strict: bool = False) -> None:
        self.close_child_ends(api, strict=strict)
        for name in ("stdin_parent", "stdout_parent", "stderr_parent"):
            handle = getattr(self, name)
            if handle is not None:
                (api.close_handle if strict else partial(_close_ignoring_errors, api))(handle)
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


@dataclass(frozen=True, slots=True)
class _OperationOutcome:
    value: Any = None
    error: BaseException | None = None


class _EffectProgress(Enum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    ACKNOWLEDGED = "acknowledged"


@dataclass(slots=True)
class _NativeEffect:
    call: Any
    arguments: tuple[Any, ...] = ()
    read_only: bool = False
    progress: _EffectProgress = _EffectProgress.READY
    value: Any = None
    future: asyncio.Future[Any] | None = None
    error: BaseException | None = None

    def invoke(self) -> Any:
        self.progress = _EffectProgress.IN_FLIGHT
        try:
            self.value = self.call(*self.arguments)
        except _Win32CallError:
            self.progress = _EffectProgress.READY
            raise
        self.progress = _EffectProgress.ACKNOWLEDGED
        return self.value


def _operation_outcome(future: asyncio.Future[Any]) -> _OperationOutcome:
    if future.cancelled():
        return _OperationOutcome(error=RuntimeError("owned native operation was cancelled"))
    error = future.exception()
    return _OperationOutcome(future.result() if error is None else None, error)


async def _native_effect_outcome(effect: _NativeEffect) -> _OperationOutcome:
    if effect.progress is _EffectProgress.ACKNOWLEDGED:
        return _OperationOutcome(effect.value)
    if effect.progress is _EffectProgress.IN_FLIGHT and effect.future is None:
        return _OperationOutcome(
            error=effect.error
            if effect.error is not None
            else _Win32CallError(AdmissionStage.DRAIN, None)
        )
    if effect.future is None:
        effect.future = asyncio.get_running_loop().run_in_executor(None, effect.invoke)
    await asyncio.wait((effect.future,))
    outcome = _operation_outcome(effect.future)
    if outcome.error is not None:
        effect.error = outcome.error
        if effect.read_only or effect.progress is _EffectProgress.READY:
            effect.progress = _EffectProgress.READY
            effect.future = None
    return outcome


@dataclass(slots=True)
class _PendingDebugEvent:
    event: Any
    continuation_status: int
    capture_attempted: bool = False
    capture_complete: bool = False
    capture_progress: str = "received"
    continuation_progress: _EffectProgress = _EffectProgress.READY
    retirement_progress: int = 0


@dataclass(frozen=True, slots=True)
class _FatalAdmission:
    error: BaseException


class _DebugCapture:
    """One persistent worker performs bounded, resumable debugger operations."""

    def __init__(self, api: _WindowsApi, job_handle: int) -> None:
        self.api = api
        self.job_handle = job_handle
        self.lock = threading.RLock()
        self.created: Future[_OperationOutcome] = Future()
        self.started: Future[_OperationOutcome] = Future()
        self._activated = asyncio.Event()
        self._stop = threading.Event()
        self._resume = False
        self._resume_possible = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="WindowsOwnedProcess-debug"
        )
        self._worker: threading.Thread | None = None
        self._driver: asyncio.Task[None] | None = None
        self._operation: asyncio.Future[Any] | None = None
        self._creation: _NativeEffect | None = None
        self._shutdown: _NativeEffect | None = None
        self._exit_probe: _NativeEffect | None = None
        self._admission_effects: list[_NativeEffect] = []
        self._joined = False
        self._cleanup_owner: Any = None
        self._acquisitions: list[_NativeEffect] = []
        self._closes: dict[int, _NativeEffect] = {}
        self._unresolved_capture = False
        self._wait_effect: _NativeEffect | None = None
        self.root_handle: int | None = None
        self.root_pid: int | None = None
        self.root_seen = False
        self.root_exit_continued = False
        self.handles: dict[int, list[int]] = {}
        self._qualified_count = 0
        self._extra_process_handles: list[int] = []
        self._image_files: set[int] = set()
        self._launch_thread_handle: int | None = None
        self.live: set[int] = set()
        self._startup_pending: set[int] = set()
        self._pending: _PendingDebugEvent | None = None
        self.failure: _Win32CallError | None = None
        self.fatal_error: BaseException | None = None

    @property
    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def known_no_child(self) -> bool:
        return (
            (self._driver is None or self.created.done())
            and self.root_handle is None
            and (self._creation is None or self._creation.progress is _EffectProgress.READY)
        )

    async def create(self, creator: Any, **kwargs: Any) -> _OperationOutcome:
        self._creation = _NativeEffect(partial(creator, capture_process_handles=True, **kwargs))
        self._driver = asyncio.create_task(self._drive())
        return await asyncio.wrap_future(self.created)

    def activate(self, *, resume: bool) -> None:
        if not self._activated.is_set():
            self._resume = resume
            self._activated.set()

    def attach_cleanup_owner(self, callback: Any) -> None:
        self._cleanup_owner = callback
        if self.failure is not None:
            callback()

    def record_failure(self, error: _Win32CallError) -> None:
        with self.lock:
            if self.failure is None:
                self.failure = error

    def record_error(self, error: BaseException, stage: AdmissionStage) -> None:
        self.record_failure(
            error
            if isinstance(error, _Win32CallError)
            else _Win32CallError(stage, getattr(error, "winerror", None))
        )
        if not isinstance(error, Exception) and self.fatal_error is None:
            self.fatal_error = error
        if self._cleanup_owner is not None:
            self._cleanup_owner()

    async def _submit(self, operation: Any) -> _OperationOutcome:
        self._operation = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        await asyncio.wait((self._operation,))
        outcome = _operation_outcome(self._operation)
        self._operation = None
        return outcome

    def _setup(self) -> None:
        self._worker = threading.current_thread()
        self.api.enable_debug_capture()

    def _create_process(self) -> tuple[int, int, int]:
        assert self._creation is not None
        process, thread, pid = self._creation.invoke()
        self.root_handle, self.root_pid = process, pid
        self._launch_thread_handle = thread
        return process, thread, pid

    def _resume_process(self) -> None:
        assert self._launch_thread_handle is not None
        self._resume_possible = True
        self.api.resume_thread(self._launch_thread_handle)

    def _close_private_handle(self, handle: int) -> None:
        effect = self._closes.setdefault(handle, _NativeEffect(self.api.close_handle, (handle,)))
        if effect.progress is _EffectProgress.ACKNOWLEDGED:
            return
        if effect.progress is _EffectProgress.IN_FLIGHT:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        effect.invoke()

    def _close_launch_thread(self) -> None:
        handle = self._launch_thread_handle
        if handle is not None:
            self._close_private_handle(handle)
            self._launch_thread_handle = None
            self._closes.pop(handle)

    async def _drive(self) -> None:
        setup = await self._submit(self._setup)
        creation = setup
        if setup.error is None:
            creation = await self._submit(self._create_process)
        if creation.error is not None:
            self.record_error(creation.error, AdmissionStage.CREATE_PROCESS)
            value = self._creation.value if self._creation is not None else None
            creation = _OperationOutcome(value, creation.error)
            self.started.set_result(_OperationOutcome(error=creation.error))
        self.created.set_result(creation)
        await self._activated.wait()
        if self.root_handle is not None:
            start = (
                await self._submit(self._resume_process) if self._resume else _OperationOutcome()
            )
            if start.error is not None:
                self.record_error(start.error, AdmissionStage.RESUME)
            if not self.started.done():
                self.started.set_result(start)
            closed = await self._submit(self._close_launch_thread)
            if closed.error is not None:
                self.record_error(closed.error, AdmissionStage.DRAIN)
        elif not self.started.done():
            self.started.set_result(_OperationOutcome(error=creation.error))
        while not self._stop.is_set():
            pending = self._pending
            if self.root_handle is None:
                await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)
                continue
            if (
                self._wait_effect is not None
                and self._wait_effect.progress is _EffectProgress.IN_FLIGHT
            ):
                await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)
                continue
            if pending is None or not pending.capture_attempted:
                outcome = await self._submit(self._capture_next_event)
            elif pending.continuation_progress is _EffectProgress.READY:
                outcome = await self._submit(self._continue_pending_event)
            elif pending.continuation_progress is _EffectProgress.ACKNOWLEDGED:
                outcome = await self._submit(self._retire_pending_event)
            else:
                await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)
                continue
            if outcome.error is not None:
                if self._pending is not None and self._pending.capture_progress == "acquiring":
                    self._unresolved_capture = True
                self.record_error(outcome.error, AdmissionStage.DRAIN)
                await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)

    def _capture_next_event(self) -> None:
        if self._pending is None:
            if self._wait_effect is None:
                self._wait_effect = _NativeEffect(self.api.wait_debug_event, (_DEBUG_WAIT_MS,))
            effect = self._wait_effect
            if effect.progress is _EffectProgress.IN_FLIGHT:
                return
            event = (
                effect.value if effect.progress is _EffectProgress.ACKNOWLEDGED else effect.invoke()
            )
            if event is None:
                self._wait_effect = None
                return
            with self.lock:
                status = (
                    _DBG_EXCEPTION_NOT_HANDLED
                    if event.dwDebugEventCode == _EXCEPTION_DEBUG_EVENT
                    else _DBG_CONTINUE
                )
                self._pending = _PendingDebugEvent(event, status)
                self._wait_effect = None
        with self.lock:
            pending = self._pending
            assert pending is not None
            if not pending.capture_attempted:
                pending.capture_attempted = True
                pending.continuation_status = self._capture_event(pending.event)
                pending.capture_complete = True

    def _continue_pending_event(self) -> None:
        with self.lock:
            pending = self._pending
            assert pending is not None and pending.continuation_progress is _EffectProgress.READY
            pending.continuation_progress = _EffectProgress.IN_FLIGHT
            try:
                self.api.continue_debug_event(pending.event, pending.continuation_status)
            except _Win32CallError:
                pending.continuation_progress = _EffectProgress.READY
                raise
            pending.continuation_progress = _EffectProgress.ACKNOWLEDGED

    def _retire_pending_event(self) -> None:
        with self.lock:
            pending = self._pending
            assert (
                pending is not None
                and pending.continuation_progress is _EffectProgress.ACKNOWLEDGED
            )
            event = pending.event
            if event.dwDebugEventCode == _EXIT_PROCESS_DEBUG_EVENT:
                pid = event.dwProcessId
                if pending.retirement_progress == 0:
                    self.live.discard(pid)
                    pending.retirement_progress = 1
                if pending.retirement_progress == 1:
                    self._startup_pending.discard(pid)
                    pending.retirement_progress = 2
                if pending.retirement_progress == 2:
                    if pid == self.root_pid:
                        self.root_exit_continued = True
                    pending.retirement_progress = 3
            self._pending = None

    def _capture_event(self, event: Any) -> int:
        code, pid = event.dwDebugEventCode, event.dwProcessId
        if code == _CREATE_PROCESS_DEBUG_EVENT:
            self._startup_pending.add(pid)
            info = event.u.CreateProcessInfo
            if info.hFile:
                self._image_files.add(info.hFile)
            try:
                self._retain_process(pid, info.hProcess)
            finally:
                self.live.add(pid)
            if info.hFile:
                self._close_image_file(info.hFile)
        elif code == _LOAD_DLL_DEBUG_EVENT:
            if event.u.LoadDll.hFile:
                self._close_image_file(event.u.LoadDll.hFile)
        elif code == _EXCEPTION_DEBUG_EVENT:
            if pid in self._startup_pending and self.api.is_startup_breakpoint(event):
                self._startup_pending.remove(pid)
                return _DBG_CONTINUE
            return _DBG_EXCEPTION_NOT_HANDLED
        elif code == _EXIT_PROCESS_DEBUG_EVENT:
            if pid not in self.live:
                raise _Win32CallError(AdmissionStage.DRAIN, None)
        elif code == _RIP_EVENT or pid not in self.live:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        return _DBG_CONTINUE

    def _retain_process(self, pid: int, event_handle: int) -> None:
        assert self._pending is not None
        self._pending.capture_progress = "acquiring"
        acquisition = _NativeEffect(self.api.duplicate_process, (event_handle,))
        self._acquisitions.append(acquisition)
        duplicate = acquisition.invoke()
        self._extra_process_handles.append(duplicate)
        self._acquisitions.remove(acquisition)
        _make_non_inheritable(duplicate, AdmissionStage.DRAIN)
        if self.api.process_id(duplicate) != pid or not self.api.is_process_in_job(
            duplicate, self.job_handle
        ):
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        for existing in self.handles.get(pid, ()):
            if self.api.same_process(existing, duplicate):
                self._pending.capture_progress = "qualified"
                self._close_private_handle(duplicate)
                self._extra_process_handles.remove(duplicate)
                self._closes.pop(duplicate)
                return
        if pid in self.live or self._qualified_count >= _MAX_JOB_MEMBERS:
            raise _Win32CallError(
                AdmissionStage.DRAIN,
                _ERROR_MORE_DATA if self._qualified_count >= _MAX_JOB_MEMBERS else None,
            )
        if pid == self.root_pid:
            if self.root_handle is None or not self.api.same_process(self.root_handle, duplicate):
                raise _Win32CallError(AdmissionStage.DRAIN, None)
            self.root_seen = True
            self.handles[pid] = [self.root_handle]
            self._qualified_count += 1
            self._pending.capture_progress = "qualified"
            self._close_private_handle(duplicate)
            self._extra_process_handles.remove(duplicate)
            self._closes.pop(duplicate)
            return
        self.handles.setdefault(pid, []).append(duplicate)
        self._qualified_count += 1
        self._pending.capture_progress = "qualified"
        self._extra_process_handles.remove(duplicate)

    def _close_image_file(self, handle: int) -> None:
        self._image_files.add(handle)
        self._close_private_handle(handle)
        self._image_files.remove(handle)
        self._closes.pop(handle)

    def retained_handles(self) -> tuple[int, ...]:
        return tuple(handle for group in self.handles.values() for handle in group)

    def _physical_exit_proven(self) -> bool:
        """Keep successful drainage bound to exact Job lifetime history."""
        with self.lock:
            if not self._physical_exit_for_close_proven():
                return False
            if self.known_no_child or not self._resume_possible:
                return True
            return (
                0
                < self.api.total_processes(self.job_handle)
                == len(self.retained_handles())
                == self._qualified_count
                <= _MAX_JOB_MEMBERS
            )

    def _physical_exit_for_close_proven(self) -> bool:
        """Prove physical exit without accepting incomplete lifetime history."""
        with self.lock:
            if any(
                effect.progress is _EffectProgress.IN_FLIGHT for effect in self._admission_effects
            ):
                return False
            if self.known_no_child:
                return True
            root = self.root_handle
            if (
                root is None
                or self._creation is None
                or self._creation.progress is not _EffectProgress.ACKNOWLEDGED
            ):
                return False
            if (
                self._wait_effect is not None
                and self._wait_effect.progress is _EffectProgress.IN_FLIGHT
            ):
                return False
            if any(
                effect.progress is _EffectProgress.IN_FLIGHT
                for effect in (*self._acquisitions, *self._closes.values())
            ):
                return False
            if not self.root_exit_continued or self.live or self._pending is not None:
                return False
            handles = self.retained_handles()
            if not all(
                self.api.wait_for_process(handle, 0)
                for handle in (root, *handles, *self._extra_process_handles)
            ):
                return False
            if not self._resume_possible:
                return True
            return (
                not self._unresolved_capture
                and self.root_seen
                and self.api.active_processes(self.job_handle) == 0
                and 0 < len(handles) == self._qualified_count <= _MAX_JOB_MEMBERS
            )

    async def join_exited(self, timeout: float) -> bool:
        if self._joined:
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        while not self._stop.is_set():
            if self._exit_probe is None:
                self._exit_probe = _NativeEffect(
                    self._physical_exit_for_close_proven, read_only=True
                )
                self._exit_probe.future = asyncio.get_running_loop().run_in_executor(
                    None, self._exit_probe.invoke
                )
            assert self._exit_probe.future is not None
            done, _ = await asyncio.wait(
                (self._exit_probe.future,), timeout=max(deadline - time.monotonic(), 0.0)
            )
            if not done:
                return False
            proof = _operation_outcome(self._exit_probe.future)
            self._exit_probe = None
            if proof.error is not None:
                self.record_error(proof.error, AdmissionStage.DRAIN)
            elif proof.value:
                self._stop.set()
                break
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)
        if self._driver is not None:
            done, _ = await asyncio.wait(
                (self._driver,), timeout=max(deadline - time.monotonic(), 0.0)
            )
            if not done:
                return False
        if self._shutdown is None:
            self._shutdown = _NativeEffect(partial(self._executor.shutdown, wait=True))
            self._shutdown.future = asyncio.get_running_loop().run_in_executor(
                None, self._shutdown.invoke
            )
        assert self._shutdown.future is not None
        done, _ = await asyncio.wait(
            (self._shutdown.future,), timeout=max(deadline - time.monotonic(), 0.0)
        )
        if not done:
            return False
        outcome = _operation_outcome(self._shutdown.future)
        if outcome.error is not None:
            self.record_error(outcome.error, AdmissionStage.DRAIN)
            return False
        self._joined = True
        return True

    async def _release_handle(self, handle: int) -> bool:
        effect = self._closes.setdefault(handle, _NativeEffect(self.api.close_handle, (handle,)))
        outcome = await _native_effect_outcome(effect)
        if outcome.error is not None:
            self.record_error(outcome.error, AdmissionStage.DRAIN)
            return False
        return True

    async def release_duplicates(self) -> bool:
        for group in self.handles.values():
            for handle in tuple(group):
                if handle != self.root_handle:
                    if not await self._release_handle(handle):
                        return False
                    group.remove(handle)
                    self._closes.pop(handle)
        while self._extra_process_handles:
            handle = self._extra_process_handles[-1]
            if not await self._release_handle(handle):
                return False
            self._extra_process_handles.pop()
            self._closes.pop(handle)
        for handle in tuple(self._image_files):
            if not await self._release_handle(handle):
                return False
            self._image_files.remove(handle)
            self._closes.pop(handle)
        if self._launch_thread_handle is not None:
            handle = self._launch_thread_handle
            if not await self._release_handle(handle):
                return False
            self._launch_thread_handle = None
            self._closes.pop(handle)
        self.handles.clear()
        return True


class _FailedAdmissionReaper:
    """Retain failed-admission handles until a root-exit observation permits closure."""

    def __init__(
        self,
        *,
        api: _WindowsApi,
        job_handle: int | None,
        process_handle: int | None,
        thread_handle: int | None,
        port_handle: int | None,
        pipe_ends: _PipeEnds | None,
        transports: tuple[asyncio.BaseTransport, ...],
        admitted: bool,
        debug_capture: _DebugCapture | None = None,
    ) -> None:
        self._api = api
        self._job_handle = job_handle
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self._pipe_ends = pipe_ends
        self._port_handle = port_handle
        self._transports = transports
        self._admitted = admitted
        self._debug_capture = debug_capture
        self._closed = False
        self._io_closed = False
        self._completed = asyncio.Event()
        self._effects: dict[str, _NativeEffect] = {}
        self._captured_cleanup_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        """Whether a retained root exit has allowed every controlling handle to close."""
        return self._closed

    def schedule(self) -> None:
        """Begin the private retry loop once the initial bounded attempt failed."""
        if self._closed or self._task is not None:
            return
        self._task = asyncio.create_task(self._retry_until_root_exit())

    async def wait_for_completion(self, timeout: float) -> bool:
        """Await only a bounded completion fact; never expose retained handle values."""
        if self._closed:
            return True
        try:
            await asyncio.wait_for(self._completed.wait(), timeout=max(timeout, 0.0))
        except asyncio.TimeoutError:
            return False
        return self._closed

    async def _retry_until_root_exit(self) -> None:
        delay = _FAILED_ADMISSION_REAPER_INITIAL_BACKOFF_SECONDS
        while not self._closed:
            await asyncio.sleep(delay)
            try:
                failure = await self._attempt_cleanup()
            except Exception:
                # A private retry owner must not abandon its handles because a
                # transient boundary adapter error escaped one attempt.
                logger.exception("Failed-admission reaper cleanup retry raised")
            else:
                if failure is not None:
                    logger.warning(
                        "Failed-admission reaper cleanup retry failed: stage=%s winerror=%s",
                        failure.stage.value,
                        failure.winerror,
                    )
            if not self._closed:
                delay = min(delay * 2, _FAILED_ADMISSION_REAPER_MAX_BACKOFF_SECONDS)

    async def _attempt_cleanup(self) -> _Win32CallError | None:
        if self._debug_capture is not None:
            async with self._captured_cleanup_lock:
                return await self._attempt_captured_cleanup()
        self._close_io()
        process_handle = self._process_handle
        if process_handle is None:
            self._close_after_root_exit()
            return None
        process_failure: _Win32CallError | None = None
        try:
            self._api.terminate_process(process_handle)
        except _Win32CallError as error:
            process_failure = error
        job_failure: _Win32CallError | None = None
        if self._admitted:
            job_handle = self._job_handle
            if job_handle is None:
                job_failure = _Win32CallError(AdmissionStage.DRAIN, None)
            else:
                try:
                    self._api.terminate_job(job_handle)
                except _Win32CallError as error:
                    job_failure = error
        try:
            root_exited = await asyncio.to_thread(
                self._api.wait_for_process,
                process_handle,
                int(_ADMISSION_CLEANUP_TIMEOUT * 1000),
            )
        except _Win32CallError as error:
            return error
        if root_exited:
            self._close_after_root_exit()
            return None
        return job_failure or process_failure or _Win32CallError(AdmissionStage.DRAIN, None)

    async def _captured_effect(self, key: str, call: Any, *arguments: Any) -> bool:
        capture = self._debug_capture
        assert capture is not None
        effect = self._effects.setdefault(key, _NativeEffect(call, arguments))
        outcome = await _native_effect_outcome(effect)
        if outcome.error is not None:
            capture.record_error(outcome.error, AdmissionStage.DRAIN)
            return False
        return True

    async def _attempt_captured_cleanup(self) -> _Win32CallError | None:
        capture = self._debug_capture
        assert capture is not None
        if self._closed:
            return None
        capture.activate(resume=False)
        self._thread_handle = None  # The capture retains the creator's primary-thread handle.
        for transport in self._transports:
            transport.close()
        self._transports = ()
        if self._pipe_ends is not None:
            if not await self._captured_effect(
                "pipes", partial(self._pipe_ends.close_unwired, strict=True), self._api
            ):
                return capture.failure
            self._pipe_ends = None
        self._io_closed = True
        process = self._process_handle
        if process is None:
            if not capture.known_no_child:
                return capture.failure or _Win32CallError(AdmissionStage.DRAIN, None)
        else:
            await self._captured_effect("terminate-root", self._api.terminate_process, process)
            if self._admitted and self._job_handle is not None:
                await self._captured_effect(
                    "terminate-job", self._api.terminate_job, self._job_handle
                )
        if any(effect.progress is _EffectProgress.IN_FLIGHT for effect in self._effects.values()):
            return capture.failure or _Win32CallError(AdmissionStage.DRAIN, None)
        if not await capture.join_exited(_ADMISSION_CLEANUP_TIMEOUT):
            return capture.failure or _Win32CallError(AdmissionStage.DRAIN, None)
        if not await capture.release_duplicates():
            return capture.failure
        for name in ("_process_handle", "_job_handle", "_port_handle"):
            handle = getattr(self, name)
            if handle is not None:
                if not await self._captured_effect(
                    f"close:{handle}", self._api.close_handle, handle
                ):
                    return capture.failure
                setattr(self, name, None)
        self._closed = True
        self._completed.set()
        return None

    def _close_io(self) -> None:
        if self._io_closed:
            return
        self._io_closed = True
        for transport in self._transports:
            transport.close()
        self._transports = ()
        if self._pipe_ends is not None:
            self._pipe_ends.close_unwired(self._api)
            self._pipe_ends = None

    def _close_after_root_exit(self) -> None:
        if self._closed:
            return
        if self._thread_handle is not None:
            _close_ignoring_errors(self._api, self._thread_handle)
            self._thread_handle = None
        if self._process_handle is not None:
            _close_ignoring_errors(self._api, self._process_handle)
            self._process_handle = None
        if self._job_handle is not None:
            _close_ignoring_errors(self._api, self._job_handle)
            self._job_handle = None
        if self._port_handle is not None:
            _close_ignoring_errors(self._api, self._port_handle)
            self._port_handle = None
        self._closed = True
        self._completed.set()


def _count_live_members_without_handle(
    live_pids: set[int], root_pid: int, retained_handles: Mapping[int, object]
) -> int:
    return sum(pid != root_pid and pid not in retained_handles for pid in live_pids)


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
    capture_process_handles: bool = False,
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
            _CREATE_SUSPENDED
            | _CREATE_UNICODE_ENVIRONMENT
            | (_DEBUG_PROCESS if capture_process_handles else 0),
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
        port_handle: int,
        stdin: asyncio.StreamWriter | None,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
        transports: tuple[asyncio.BaseTransport, ...],
        debug_capture: _DebugCapture | None = None,
    ) -> None:
        self.owner = owner
        self._api = api
        self._job_handle: int | None = job_handle
        self._port_handle: int | None = port_handle
        self._process_handle: int | None = process_handle
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._transports = transports
        self._returncode: int | None = None
        self._drain_task: asyncio.Task[OwnerDrainReceipt] | None = None
        self._drain_receipt: OwnerDrainReceipt | None = None
        self._member_handles: dict[int, int] = {}
        self._birth_notifications = 0
        self._root_birth_seen = False
        self._live_births: set[int] = set()
        self._birth_generations: dict[int, int] = {}
        self._retired_members: set[int] = set()
        self._unmatched_member_handles: list[int] = []
        self._unverified_membership = False
        self._debug_capture = debug_capture
        self._exit_notifications = 0
        self._final_snapshot: dict[str, object] | None = None
        self._close_task: asyncio.Task[OwnerDrainReceipt] | None = None
        self._close_reaper: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._cleanup_effects: dict[str, _NativeEffect] = {}
        self._captured_observation_future: asyncio.Future[Any] | None = None
        if debug_capture is not None:
            debug_capture.attach_cleanup_owner(self._schedule_close_reaper)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def fatal_error(self) -> BaseException | None:
        return self._debug_capture.fatal_error if self._debug_capture is not None else None

    def _schedule_close_reaper(self) -> None:
        if not self._closed and (self._close_reaper is None or self._close_reaper.done()):
            self._close_reaper = asyncio.create_task(self._retry_close())

    async def wait_closed(self) -> OwnerDrainReceipt:
        """Keep the current cleanup owner and its loop alive until physical closure."""
        while not self._closed:
            await self.aclose()
            if not self._closed:
                await asyncio.sleep(_ACCOUNTING_POLL_SECONDS)
        assert self._drain_receipt is not None
        return self._drain_receipt

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
        capture_process_handles: bool = False,
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
            capture_process_handles=capture_process_handles,
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
        capture_process_handles: bool = False,
    ) -> WindowsOwnedProcess:
        """Private injection seam for deterministic admission-order coverage."""
        admission = cls._admit_with(
            generation=generation,
            argv=argv,
            cwd=cwd,
            env=env,
            stdin_mode=stdin_mode,
            api=api,
            pipe_ends=pipe_ends,
            process_creator=process_creator,
            capture_process_handles=capture_process_handles,
        )
        if not capture_process_handles:
            result = await admission
            assert isinstance(result, WindowsOwnedProcess)
            return result
        task = asyncio.create_task(admission)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
        result = task.result()  # Fatal admission outcomes are values, never Task exceptions.
        if isinstance(result, _FatalAdmission):
            raise result.error
        if cancellation is not None:
            cleanup = asyncio.create_task(result.wait_closed())
            while not cleanup.done():
                try:
                    await asyncio.wait((cleanup,))
                except asyncio.CancelledError:
                    pass
            cleanup.result()
            if result.fatal_error is not None:
                raise result.fatal_error
            raise cancellation
        return result

    @classmethod
    async def _admit_with(
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
        capture_process_handles: bool,
    ) -> WindowsOwnedProcess | _FatalAdmission:
        owner_id = uuid.uuid4().hex
        job_handle: int | None = None
        port_handle: int | None = None
        process_handle: int | None = None
        thread_handle: int | None = None
        process_id: int | None = None
        endpoints = pipe_ends
        admitted = False
        transports: tuple[asyncio.BaseTransport, ...] = ()
        capture = _DebugCapture(api, 0) if capture_process_handles else None
        stage = AdmissionStage.CREATE_JOB

        async def admission_call(call: Any, *arguments: Any, read_only: bool = False) -> Any:
            if capture is None:
                return call(*arguments)
            effect = _NativeEffect(call, arguments, read_only=read_only)
            capture._admission_effects.append(effect)
            outcome = await _native_effect_outcome(effect)
            if outcome.error is not None:
                capture.record_error(outcome.error, stage)
                raise outcome.error
            capture._admission_effects.remove(effect)
            return outcome.value

        try:
            job_handle = await admission_call(api.create_job)
            await admission_call(_make_non_inheritable, job_handle, AdmissionStage.CREATE_JOB)
            await admission_call(api.set_kill_on_close, job_handle)
            port_handle = await admission_call(api.create_completion_port)
            await admission_call(_make_non_inheritable, port_handle, AdmissionStage.CREATE_JOB)
            await admission_call(api.attach_completion_port, job_handle, port_handle)
            endpoints = endpoints or await admission_call(_PipeEnds.create, stdin_mode)
            stage = AdmissionStage.CREATE_PROCESS
            if capture is not None:
                assert job_handle is not None
                capture.job_handle = job_handle
                creation = await capture.create(
                    process_creator, argv=argv, cwd=cwd, env=env, pipe_ends=endpoints
                )
                if creation.value is not None:
                    process_handle, thread_handle, process_id = creation.value
                if creation.error is not None:
                    raise creation.error
            else:
                process_handle, thread_handle, process_id = process_creator(
                    argv=argv, cwd=cwd, env=env, pipe_ends=endpoints
                )
            assert process_handle is not None and thread_handle is not None
            await admission_call(
                _make_non_inheritable, process_handle, AdmissionStage.CREATE_PROCESS
            )
            await admission_call(
                _make_non_inheritable, thread_handle, AdmissionStage.CREATE_PROCESS
            )
            assert endpoints is not None
            close_children = (
                partial(endpoints.close_child_ends, strict=True)
                if capture is not None
                else endpoints.close_child_ends
            )
            await admission_call(close_children, api)
            stage = AdmissionStage.ASSIGN
            await admission_call(api.assign_process, job_handle, process_handle)
            admitted = True
            stage = AdmissionStage.VERIFY
            if not await admission_call(
                api.is_process_in_job, process_handle, job_handle, read_only=True
            ):
                raise _Win32CallError(AdmissionStage.VERIFY, None)
            await admission_call(api.active_processes, job_handle, read_only=True)
            stage = AdmissionStage.WIRE_IO
            try:
                stdin, stdout, stderr, transports = await endpoints.wire(asyncio.get_running_loop())
            except _Win32CallError:
                raise
            except Exception as error:
                raise _Win32CallError(
                    AdmissionStage.WIRE_IO,
                    getattr(error, "winerror", None),
                ) from error
            # Resume is last.  Every capability-defining fact above is true
            # before any adapter code can execute in the child process.
            stage = AdmissionStage.RESUME
            if capture is not None:
                capture.activate(resume=True)
                thread_handle = None  # The creator thread closes its launch handle.
                start = await asyncio.wrap_future(capture.started)
                if start.error is not None:
                    raise start.error
            else:
                api.resume_thread(thread_handle)
                _close_ignoring_errors(api, thread_handle)
                thread_handle = None
            assert process_id is not None and job_handle is not None and port_handle is not None
            owner = OwnedProcessRef(owner_id=owner_id, generation=generation, root_pid=process_id)
            return cls(
                owner=owner,
                api=api,
                port_handle=port_handle,
                job_handle=job_handle,
                process_handle=process_handle,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                transports=transports,
                debug_capture=capture,
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
                    port_handle=port_handle,
                    pipe_ends=endpoints,
                    transports=transports,
                    admitted=admitted,
                    debug_capture=capture,
                )
            except AdmissionCleanupError as cleanup_error:
                raise cleanup_error from error
            if capture is not None and capture.fatal_error is not None:
                return _FatalAdmission(capture.fatal_error)
            raise ProcessAdmissionError(error.stage, owner_id, error.winerror) from error
        except BaseException as error:
            if capture is not None:
                capture.record_error(error, stage)
            try:
                await _cleanup_failed_admission(
                    api=api,
                    owner_id=owner_id,
                    admission_stage=None,
                    admission_winerror=None,
                    job_handle=job_handle,
                    process_handle=process_handle,
                    thread_handle=thread_handle,
                    port_handle=port_handle,
                    pipe_ends=endpoints,
                    transports=transports,
                    admitted=admitted,
                    debug_capture=capture,
                )
            except AdmissionCleanupError as cleanup_error:
                raise cleanup_error from error
            if capture is not None and capture.fatal_error is not None:
                return _FatalAdmission(capture.fatal_error)
            raise

    async def wait(self) -> int:
        """Match the small process-like surface consumed by the DAP observer."""

        return await self.wait_root()

    async def wait_root(self) -> int:
        """Wait for the retained root handle without reopening a PID."""

        capture = self._debug_capture
        if capture is not None:
            while not self._closed and self._returncode is None:
                handle = self._process_handle
                assert handle is not None
                waited = await _native_effect_outcome(
                    _NativeEffect(
                        self._api.wait_for_process, (handle, _DEBUG_WAIT_MS), read_only=True
                    )
                )
                if waited.error is not None:
                    capture.record_error(waited.error, AdmissionStage.DRAIN)
                    await self.wait_closed()
                elif waited.value:
                    code = await _native_effect_outcome(
                        _NativeEffect(self._api.exit_code, (handle,), read_only=True)
                    )
                    if code.error is not None:
                        capture.record_error(code.error, AdmissionStage.DRAIN)
                        await self.wait_closed()
                    elif code.value is not None:
                        self._returncode = code.value
                    else:
                        capture.record_failure(_Win32CallError(AdmissionStage.DRAIN, None))
                        await self.wait_closed()
            return self._returncode if self._returncode is not None else 0
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

    def _observe_job_messages(self) -> None:
        if self._port_handle is None:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        for message, pid in self._api.job_messages(self._port_handle):
            if self._debug_capture is not None:
                # Raw IOCP traffic is diagnostic, not capability admission.
                if message == _JOB_OBJECT_MSG_NEW_PROCESS:
                    self._birth_notifications += 1
                    self._live_births.add(pid)
                    if pid == self.pid:
                        self._root_birth_seen = True
                elif message in (
                    _JOB_OBJECT_MSG_EXIT_PROCESS,
                    _JOB_OBJECT_MSG_ABNORMAL_EXIT_PROCESS,
                ):
                    self._exit_notifications += 1
                    self._live_births.discard(pid)
                continue
            if message == _JOB_OBJECT_MSG_NEW_PROCESS:
                if (
                    not pid
                    or pid in self._live_births
                    or (pid == self.owner.root_pid and self._root_birth_seen)
                    or self._birth_notifications >= _MAX_JOB_MEMBERS
                ):
                    self._unverified_membership = True
                    continue
                self._birth_notifications += 1
                self._live_births.add(pid)
                self._birth_generations[pid] = self._birth_notifications
                self._retired_members.discard(pid)
                if pid == self.owner.root_pid:
                    self._root_birth_seen = True
            elif message in (_JOB_OBJECT_MSG_EXIT_PROCESS, _JOB_OBJECT_MSG_ABNORMAL_EXIT_PROCESS):
                self._exit_notifications += 1
                if pid not in self._live_births:
                    self._unverified_membership = True
                    continue
                self._live_births.remove(pid)
                if pid != self.owner.root_pid:
                    self._retired_members.add(pid)
                    handle = self._member_handles.pop(pid, None)
                    if handle is not None:
                        if self._api.wait_for_process(handle, 0):
                            _close_ignoring_errors(self._api, handle)
                        else:
                            self._unmatched_member_handles.append(handle)

    def _snapshot_members(self) -> None:
        job_handle = self._job_handle
        if job_handle is None:
            raise _Win32CallError(AdmissionStage.DRAIN, None)
        self._observe_job_messages()
        if self._debug_capture is not None:
            return
        member_pids = self._api.member_process_ids(job_handle)
        self._observe_job_messages()
        for pid in member_pids:
            if pid == self.owner.root_pid or pid in self._member_handles:
                continue
            if pid not in self._live_births or pid in self._retired_members:
                continue
            birth = self._birth_generations[pid]
            handle = self._api.open_job_member(job_handle, pid)
            try:
                self._observe_job_messages()
            except _Win32CallError:
                if handle is not None:
                    self._unmatched_member_handles.append(handle)
                raise
            if handle is not None:
                if pid in self._live_births and self._birth_generations[pid] == birth:
                    self._member_handles[pid] = handle
                else:
                    self._unmatched_member_handles.append(handle)
                    if self._api.wait_for_process(handle, 0):
                        self._unmatched_member_handles.pop()
                        _close_ignoring_errors(self._api, handle)

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
        """Join one immediate Job-force, handle-confirmed drain."""

        return await self._join_drain(0.0, timeout)

    async def _join_drain(
        self,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt:
        if self._closed:
            assert self._drain_receipt is not None
            return self._drain_receipt
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
        if self._debug_capture is not None:
            return await self._drain_captured(grace_timeout, force_timeout)
        graceful = await self._wait_for_zero(
            grace_timeout,
            forced=False,
            root_was_forced=False,
        )
        if graceful.status is DrainStatus.DRAINED:
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
            self._snapshot_members()
        except _Win32CallError as error:
            # A denied observation cannot veto termination of the retained Job.
            if self._debug_capture is not None:
                self._debug_capture.record_failure(error)
        try:
            self._api.terminate_job(self._job_handle)
        except _Win32CallError as error:
            if self._debug_capture is not None:
                self._debug_capture.record_failure(error)
                error = self._debug_capture.failure
                assert error is not None
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

    async def _drain_captured(
        self, grace_timeout: float, force_timeout: float
    ) -> OwnerDrainReceipt:
        capture = self._debug_capture
        assert capture is not None
        receipt = await self._wait_for_captured(grace_timeout, forced=False, root_was_forced=False)
        if receipt.status is DrainStatus.DRAINED:
            self._drain_receipt = receipt
            return receipt
        root = await _native_effect_outcome(
            _NativeEffect(self._root_is_active_before_force, read_only=True)
        )
        if root.error is not None:
            capture.record_error(root.error, AdmissionStage.DRAIN)
        job = self._job_handle
        if job is not None:
            effect = self._cleanup_effects.setdefault(
                "terminate-job", _NativeEffect(self._api.terminate_job, (job,))
            )
            outcome = await _native_effect_outcome(effect)
            if outcome.error is not None:
                capture.record_error(outcome.error, AdmissionStage.DRAIN)
        receipt = await self._wait_for_captured(
            force_timeout, forced=True, root_was_forced=root.value if root.error is None else None
        )
        self._drain_receipt = receipt
        return receipt

    async def _wait_for_zero(
        self,
        timeout: float,
        *,
        forced: bool,
        root_was_forced: bool | None = None,
    ) -> OwnerDrainReceipt:
        if self._debug_capture is not None:
            return await self._wait_for_captured(
                timeout,
                forced=forced,
                root_was_forced=root_was_forced,
            )
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                self._snapshot_members()
            except _Win32CallError as error:
                if not forced:
                    return self._receipt(
                        status=DrainStatus.FAILED,
                        forced=False,
                        active_processes=None,
                        failure_stage=error.stage,
                        winerror=error.winerror,
                        root_was_forced=root_was_forced,
                    )
                # The Job is already terminating; the child's retirement message
                # may still supply exact evidence without an OpenProcess handle.
            try:
                active_processes = self._query_active_processes()
                signaled = self._process_handle is not None and self._api.wait_for_process(
                    self._process_handle, 0
                )
                for handle in self._member_handles.values():
                    signaled = self._api.wait_for_process(handle, 0) and signaled
                for handle in self._unmatched_member_handles:
                    signaled = self._api.wait_for_process(handle, 0) and signaled
            except _Win32CallError as error:
                return self._receipt(
                    status=DrainStatus.FAILED,
                    forced=forced,
                    active_processes=None,
                    failure_stage=error.stage,
                    winerror=error.winerror,
                    root_was_forced=root_was_forced,
                )
            if active_processes == 0 and signaled:
                try:
                    await self.wait_root()
                except (_Win32CallError, RuntimeError):
                    return self._receipt(
                        status=DrainStatus.FAILED,
                        forced=forced,
                        active_processes=0,
                        failure_stage=AdmissionStage.DRAIN,
                        winerror=None,
                        root_was_forced=root_was_forced,
                    )
                try:
                    job_handle = self._job_handle
                    if job_handle is None:
                        raise _Win32CallError(AdmissionStage.DRAIN, None)
                    total = self._api.total_processes(job_handle)
                    self._observe_job_messages()
                except _Win32CallError as error:
                    return self._receipt(
                        status=DrainStatus.FAILED,
                        forced=forced,
                        active_processes=0,
                        failure_stage=AdmissionStage.DRAIN,
                        winerror=error.winerror,
                        root_was_forced=root_was_forced,
                    )
                if (
                    self._unverified_membership
                    or total != self._birth_notifications
                    or not self._root_birth_seen
                    or any(
                        pid not in self._member_handles
                        for pid in self._live_births
                        if pid != self.owner.root_pid
                    )
                ):
                    remaining = deadline - time.monotonic()
                    if not self._unverified_membership and remaining > 0:
                        await asyncio.sleep(min(_ACCOUNTING_POLL_SECONDS, remaining))
                        continue
                    return self._receipt(
                        status=DrainStatus.FAILED,
                        forced=forced,
                        active_processes=0,
                        failure_stage=AdmissionStage.DRAIN,
                        winerror=None,
                        root_was_forced=root_was_forced,
                    )
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

    def _captured_observation(self) -> tuple[int, bool]:
        capture = self._debug_capture
        assert capture is not None
        with capture.lock:
            self._observe_job_messages()
            job, root = self._job_handle, self._process_handle
            if job is None or root is None:
                raise _Win32CallError(AdmissionStage.DRAIN, None)
            active = self._api.active_processes(job)
            complete = capture._physical_exit_proven()
            if complete:
                self._returncode = self._api.exit_code(root)
                if self._returncode is None:
                    raise _Win32CallError(AdmissionStage.DRAIN, None)
            return active, complete

    async def _wait_for_captured(
        self, timeout: float, *, forced: bool, root_was_forced: bool | None
    ) -> OwnerDrainReceipt:
        capture = self._debug_capture
        assert capture is not None
        deadline = time.monotonic() + max(timeout, 0.0)
        active: int | None = None
        while True:
            complete = False
            if capture.failure is None:
                if self._captured_observation_future is None:
                    self._captured_observation_future = asyncio.get_running_loop().run_in_executor(
                        None, self._captured_observation
                    )
                done, _ = await asyncio.wait(
                    (self._captured_observation_future,),
                    timeout=max(deadline - time.monotonic(), 0.0),
                )
                if done:
                    outcome = _operation_outcome(self._captured_observation_future)
                    self._captured_observation_future = None
                    if outcome.error is not None:
                        capture.record_error(outcome.error, AdmissionStage.DRAIN)
                    else:
                        active, complete = outcome.value
            if capture.failure is not None:
                return self._receipt(
                    status=DrainStatus.FAILED,
                    forced=forced,
                    active_processes=active,
                    failure_stage=capture.failure.stage,
                    winerror=capture.failure.winerror,
                    root_was_forced=root_was_forced,
                )
            if active == 0 and complete:
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
                    status=DrainStatus.FAILED if active == 0 else DrainStatus.TIMED_OUT,
                    forced=forced,
                    active_processes=active,
                    failure_stage=AdmissionStage.DRAIN if active == 0 else None,
                    winerror=None,
                    root_was_forced=root_was_forced,
                )
            await asyncio.sleep(min(_ACCOUNTING_POLL_SECONDS, remaining))

    def drain_snapshot(self, receipt: OwnerDrainReceipt) -> dict[str, object]:
        """Return safe diagnostics, never an independent drain admission oracle."""
        if receipt.owner != self.owner:
            raise ValueError("drain receipt belongs to a different owner")
        capture = self._debug_capture
        if capture is not None:
            with capture.lock:
                return self._drain_snapshot(receipt)
        return self._drain_snapshot(receipt)

    def _drain_snapshot(self, receipt: OwnerDrainReceipt) -> dict[str, object]:
        if self._final_snapshot is not None:
            snapshot = dict(self._final_snapshot)
        else:
            capture = self._debug_capture
            handles = (
                capture.retained_handles()
                if capture is not None
                else tuple(
                    handle
                    for handle in (
                        self._process_handle,
                        *self._member_handles.values(),
                        *self._unmatched_member_handles,
                    )
                    if handle is not None
                )
            )
            total = None
            if self._job_handle is not None:
                try:
                    total = self._api.total_processes(self._job_handle)
                except _Win32CallError:
                    pass
            signaled = 0
            probe_failed = False
            for handle in handles:
                try:
                    signaled += bool(self._api.wait_for_process(handle, 0))
                except _Win32CallError:
                    probe_failed = True
            snapshot = {
                "total_processes": total,
                "birth_notifications": self._birth_notifications,
                "exit_notifications": self._exit_notifications,
                "unverified_membership": self._unverified_membership
                or (capture is not None and capture.failure is not None),
                "root_birth_seen": capture.root_seen if capture else self._root_birth_seen,
                "live_members_without_handle": _count_live_members_without_handle(
                    self._live_births,
                    self.pid,
                    capture.handles if capture else self._member_handles,
                ),
                "retained_exact_handles": len(handles),
                "signaled_exact_handles": signaled,
                "handle_probe_failed": probe_failed,
            }
        snapshot.update(
            {
                "status": receipt.status.value,
                "forced": receipt.forced,
                "root_was_forced": receipt.root_was_forced,
                "active_processes": receipt.active_processes,
                "failure_stage": receipt.failure_stage.value if receipt.failure_stage else None,
                "winerror": receipt.winerror,
            }
        )
        return snapshot

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
            root_returncode=self._returncode
            if self._debug_capture is not None
            else self.returncode,
            active_processes=active_processes,
            failure_stage=failure_stage,
            winerror=winerror,
            root_was_forced=root_was_forced,
        )

    async def aclose(self) -> OwnerDrainReceipt:
        """Join a retained close worker; caller cancellation cannot orphan cleanup."""

        if self._closed:
            assert self._drain_receipt is not None
            return self._drain_receipt
        task = self._close_task
        if task is None or task.done():
            task = asyncio.create_task(self._close_once())
            self._close_task = task
            task.add_done_callback(self._close_finished)
        return await asyncio.shield(task)

    def _close_finished(self, task: asyncio.Task[OwnerDrainReceipt]) -> None:
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "Owned Job close raised", exc_info=(type(error), error, error.__traceback__)
            )
        if (task.cancelled() or error is not None) and not self._closed:
            if self._close_reaper is None or self._close_reaper.done():
                self._close_reaper = asyncio.create_task(self._retry_close())

    async def _close_once(self) -> OwnerDrainReceipt:
        async with self._close_lock:
            if self._closed:
                assert self._drain_receipt is not None
                return self._drain_receipt
            if self._debug_capture is not None:
                return await self._close_captured()
            receipt = self._drain_receipt
            if (
                receipt is None
                or receipt.status is not DrainStatus.DRAINED
                or receipt.active_processes != 0
            ):
                receipt = await self.force_and_drain(timeout=_ADMISSION_CLEANUP_TIMEOUT)
            if receipt.status is not DrainStatus.DRAINED:
                if self._close_reaper is None:
                    self._close_reaper = asyncio.create_task(self._retry_close())
                return receipt
            self._final_snapshot = self.drain_snapshot(receipt)
            for handle in self._member_handles.values():
                _close_ignoring_errors(self._api, handle)
            self._member_handles.clear()
            for handle in self._unmatched_member_handles:
                _close_ignoring_errors(self._api, handle)
            self._unmatched_member_handles.clear()
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
            if self._port_handle is not None:
                _close_ignoring_errors(self._api, self._port_handle)
                self._port_handle = None
            self._closed = True
            return receipt

    async def _close_captured(self) -> OwnerDrainReceipt:
        capture = self._debug_capture
        assert capture is not None
        receipt = self._drain_receipt
        prior_failure = (
            receipt if receipt is not None and receipt.status is DrainStatus.FAILED else None
        )
        if receipt is None or receipt.status is not DrainStatus.DRAINED:
            receipt = await self.force_and_drain(timeout=_ADMISSION_CLEANUP_TIMEOUT)
        if prior_failure is not None:
            receipt = prior_failure
            self._drain_receipt = receipt
        assert receipt is not None

        def incomplete() -> OwnerDrainReceipt:
            assert receipt is not None
            if capture.failure is not None:
                failure = replace(
                    receipt,
                    status=DrainStatus.FAILED,
                    failure_stage=capture.failure.stage,
                    winerror=capture.failure.winerror,
                )
                self._drain_receipt = failure
                if self._final_snapshot is not None:
                    self._final_snapshot.update(
                        status=failure.status.value,
                        failure_stage=failure.failure_stage.value
                        if failure.failure_stage
                        else None,
                        winerror=failure.winerror,
                    )
            else:
                failure = receipt
            self._schedule_close_reaper()
            return failure

        termination = self._cleanup_effects.get("terminate-job")
        if termination is not None and termination.progress is _EffectProgress.READY:
            return incomplete()

        if any(
            effect.progress is _EffectProgress.IN_FLIGHT
            for effect in self._cleanup_effects.values()
        ):
            return incomplete()
        if self._captured_observation_future is not None:
            done, _ = await asyncio.wait(
                (self._captured_observation_future,), timeout=_ADMISSION_CLEANUP_TIMEOUT
            )
            if not done:
                return incomplete()
            observed = _operation_outcome(self._captured_observation_future)
            self._captured_observation_future = None
            if observed.error is not None:
                capture.record_error(observed.error, AdmissionStage.DRAIN)
        if not await capture.join_exited(_ADMISSION_CLEANUP_TIMEOUT):
            return incomplete()
        if self._final_snapshot is None:
            facts = await _native_effect_outcome(
                _NativeEffect(partial(self.drain_snapshot, receipt), read_only=True)
            )
            if facts.error is not None:
                capture.record_error(facts.error, AdmissionStage.DRAIN)
                return incomplete()
            snapshot = facts.value
            total = snapshot["total_processes"]
            if receipt.status is DrainStatus.DRAINED and (
                receipt.active_processes != 0
                or receipt.root_returncode is None
                or total is None
                or not (
                    0
                    < total
                    == snapshot["retained_exact_handles"]
                    == capture._qualified_count
                    <= _MAX_JOB_MEMBERS
                )
                or snapshot["retained_exact_handles"] != snapshot["signaled_exact_handles"]
                or snapshot["handle_probe_failed"]
                or snapshot["unverified_membership"]
                or not snapshot["root_birth_seen"]
                or snapshot["live_members_without_handle"]
            ):
                receipt = replace(
                    receipt, status=DrainStatus.FAILED, failure_stage=AdmissionStage.DRAIN
                )
            if capture.failure is not None:
                receipt = replace(
                    receipt,
                    status=DrainStatus.FAILED,
                    failure_stage=capture.failure.stage,
                    winerror=capture.failure.winerror,
                )
            self._drain_receipt = receipt
            snapshot.update(
                status=receipt.status.value,
                failure_stage=receipt.failure_stage.value if receipt.failure_stage else None,
                winerror=receipt.winerror,
            )
            self._final_snapshot = snapshot
        if not await capture.release_duplicates():
            return incomplete()
        if self.stdin is not None:
            self.stdin.close()
            self.stdin = None
        for transport in self._transports:
            transport.close()
        self._transports = ()
        for name in ("_process_handle", "_job_handle", "_port_handle"):
            handle = getattr(self, name)
            if handle is not None:
                effect = self._cleanup_effects.setdefault(
                    f"close:{handle}", _NativeEffect(self._api.close_handle, (handle,))
                )
                outcome = await _native_effect_outcome(effect)
                if outcome.error is not None:
                    capture.record_error(outcome.error, AdmissionStage.DRAIN)
                    return incomplete()
                setattr(self, name, None)
        if capture.failure is not None:
            receipt = replace(
                receipt,
                status=DrainStatus.FAILED,
                active_processes=0,
                failure_stage=capture.failure.stage,
                winerror=capture.failure.winerror,
            )
        self._drain_receipt = receipt
        assert self._final_snapshot is not None
        self._final_snapshot.update(
            status=receipt.status.value,
            active_processes=receipt.active_processes,
            failure_stage=receipt.failure_stage.value if receipt.failure_stage else None,
            winerror=receipt.winerror,
        )
        self._closed = True
        return receipt

    async def _retry_close(self) -> None:
        delay = _FAILED_ADMISSION_REAPER_INITIAL_BACKOFF_SECONDS
        while not self._closed:
            await asyncio.sleep(delay)
            try:
                receipt = await self.aclose()
            except Exception:
                logger.exception("Owned Job close retry raised")
            else:
                if receipt.status is not DrainStatus.DRAINED:
                    logger.warning(
                        "Owned Job close retry did not drain: status=%s active=%s",
                        receipt.status.value,
                        receipt.active_processes,
                    )
            delay = min(delay * 2, _FAILED_ADMISSION_REAPER_MAX_BACKOFF_SECONDS)


async def _cleanup_failed_admission(
    *,
    api: _WindowsApi,
    owner_id: str,
    admission_stage: AdmissionStage | None,
    admission_winerror: int | None,
    job_handle: int | None,
    port_handle: int | None,
    process_handle: int | None,
    thread_handle: int | None,
    pipe_ends: _PipeEnds | None,
    transports: tuple[asyncio.BaseTransport, ...],
    admitted: bool,
    debug_capture: _DebugCapture | None = None,
) -> None:
    """Transfer failed admission to one private retry owner before this frame unwinds."""
    reaper = _FailedAdmissionReaper(
        api=api,
        port_handle=port_handle,
        job_handle=job_handle,
        process_handle=process_handle,
        thread_handle=thread_handle,
        pipe_ends=pipe_ends,
        transports=transports,
        admitted=admitted,
        debug_capture=debug_capture,
    )
    if debug_capture is not None:
        debug_capture.attach_cleanup_owner(reaper.schedule)
        failure = await reaper._attempt_cleanup()
        if failure is not None:
            reaper.schedule()
            await reaper._completed.wait()
        return
    try:
        failure = await reaper._attempt_cleanup()
    except BaseException:
        reaper.schedule()
        raise
    if failure is None:
        return
    reaper.schedule()
    raise AdmissionCleanupError(
        owner_id=owner_id,
        admission_stage=admission_stage,
        admission_winerror=admission_winerror,
        cleanup_stage=failure.stage,
        cleanup_winerror=failure.winerror,
        reaper=reaper,
    )

"""One-shot provider-value replacement without shared UI or input admission."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from ..windows_process_owner import DrainStatus, WindowsOwnedProcess

PROVIDER_TEXT_TIMEOUT_SECONDS = 30.0
_PROVIDER_RESPONSE_LIMIT = 16 * 1024
_SELECTOR_KEYS = frozenset({"automation_id", "name", "control_type"})
_PROVIDER_REASONS = frozenset(
    {
        "invalid_request",
        "process_unavailable",
        "process_changed",
        "target_unavailable",
        "target_ambiguous",
        "target_unreadable",
        "tree_limit_exceeded",
        "target_changed",
        "value_unsupported",
        "value_read_only",
        "target_password",
        "target_disabled",
        "setter_failed",
        "readback_unavailable",
        "readback_mismatch",
        "verified",
    }
)
_RESULT_REASONS = _PROVIDER_REASONS | {
    "invalid_text",
    "invalid_selector",
    "no_active_session",
    "unsupported_platform",
    "process_identity_unavailable",
    "provider_helper_unavailable",
    "provider_unavailable",
    "provider_operation_unresolved",
    "provider_outcome_unknown",
    "provider_replacement_not_verified",
    "replacement_mode_required",
    "replacement_mode_requires_set_text",
    "provider_session_unavailable",
    "provider_session_changed",
}
_OUTCOMES = frozenset(
    {
        ("PASS", "completed", True),
        ("FAIL", "completed", False),
        ("FAIL", "unknown", False),
        ("BLOCKED", "not_started", False),
    }
)
_cleanup_tasks: set[asyncio.Task[None]] = set()


def blocked(reason: str) -> dict[str, Any]:
    return {"status": "BLOCKED", "effect_state": "not_started", "verified": False, "reason": reason}


def unknown() -> dict[str, Any]:
    return {
        "status": "FAIL",
        "effect_state": "unknown",
        "verified": False,
        "reason": "provider_outcome_unknown",
    }


def _valid_unicode(value: str) -> bool:
    pending_high = False
    for character in value:
        codepoint = ord(character)
        if pending_high:
            if not 0xDC00 <= codepoint <= 0xDFFF:
                return False
            pending_high = False
        elif 0xD800 <= codepoint <= 0xDBFF:
            pending_high = True
        elif codepoint == 0 or 0xDC00 <= codepoint <= 0xDFFF:
            return False
    return not pending_high


def validate_request(selector: Any, text: Any) -> dict[str, Any] | None:
    """Reject unsupported criteria and invalid UTF-16 without changing valid text."""
    if not isinstance(text, str) or not _valid_unicode(text):
        return blocked("invalid_text")
    if not isinstance(selector, dict) or not selector or set(selector) - _SELECTOR_KEYS:
        return blocked("invalid_selector")
    for value in selector.values():
        if not isinstance(value, str) or not value.strip() or not _valid_unicode(value):
            return blocked("invalid_selector")
    return None


def sanitize_result(value: Any) -> dict[str, Any]:
    """Accept only coherent semantic evidence, never raw provider diagnostics."""
    if not isinstance(value, dict):
        return unknown()
    status, effect, verified = value.get("status"), value.get("effect_state"), value.get("verified")
    if not isinstance(status, str) or not isinstance(effect, str) or type(verified) is not bool:
        return unknown()
    if (status, effect, verified) not in _OUTCOMES:
        return unknown()
    result = {"status": status, "effect_state": effect, "verified": verified}
    reason = value.get("reason")
    if isinstance(reason, str) and reason in _RESULT_REASONS:
        result["reason"] = reason
    elif status != "PASS":
        result["reason"] = "provider_replacement_not_verified"
    return result


async def replace_for_session(
    session: Any, *, selector: Any, text: Any, replacement_mode: Any
) -> dict[str, Any]:
    """Admit provider replacement before any generic UI connection or restore."""
    if replacement_mode != "provider_value":
        return blocked("replacement_mode_required")
    refused = validate_request(selector, text)
    if refused is not None:
        return refused
    replace = getattr(session, "replace_text_provider", None)
    if not callable(replace):
        return blocked("provider_session_unavailable")
    try:
        return sanitize_result(await replace(selector, text))
    except asyncio.CancelledError:
        raise
    except Exception:
        return unknown()


class ProcessBinding:
    """Read-only retained process handle; a PID never authorizes cleanup."""

    def __init__(self, pid: int, creation_time: str, handle: Any, kernel32: Any) -> None:
        self.pid = pid
        self.creation_time = creation_time
        self.identity = (pid, creation_time)
        self._handle = handle
        self._kernel32 = kernel32

    def is_signaled(self) -> bool:
        if self._handle is None:
            raise RuntimeError("process_binding_closed")
        result = self._kernel32.WaitForSingleObject(self._handle, 0)
        if result == 0:
            return True
        if result == 258:
            return False
        raise RuntimeError("process_liveness_unavailable")

    def close(self) -> None:
        if self._handle is not None:
            if not self._kernel32.CloseHandle(self._handle):
                raise RuntimeError("process_binding_close_failed")
            self._handle = None


def capture_process(pid: int) -> ProcessBinding:
    """Open QUERY_LIMITED_INFORMATION | SYNCHRONIZE, retaining exact FILETIME."""
    if os.name != "nt":
        raise RuntimeError("unsupported_platform")
    if type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF:
        raise RuntimeError("process_unavailable")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        raise RuntimeError("process_unavailable")
    try:
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise RuntimeError("process_identity_unavailable")
        creation_time = str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
        binding = ProcessBinding(pid, creation_time, handle, kernel32)
        if binding.is_signaled():
            raise RuntimeError("process_unavailable")
        return binding
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_response_key")
        result[key] = value
    return result


def _ready_banner(line: bytes) -> bool:
    if not line.endswith(b"\n") or len(line) > _PROVIDER_RESPONSE_LIMIT:
        return False
    try:
        value = json.loads(line, object_pairs_hook=_unique_object)
        return (
            isinstance(value, dict)
            and set(value) == {"protocol", "ready"}
            and value["protocol"] == "netcoredbg.provider-text.v1"
            and value["ready"] is True
        )
    except (ValueError, TypeError, UnicodeError):
        return False


def _response(line: bytes, request_id: str) -> dict[str, Any]:
    try:
        value = json.loads(line, object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value) != {"jsonrpc", "id", "result"}
            or value["jsonrpc"] != "2.0"
            or value["id"] != request_id
        ):
            return unknown()
        result = value["result"]
        if not isinstance(result, dict) or set(result) - {
            "status",
            "effect_state",
            "verified",
            "reason",
        }:
            return unknown()
        if "reason" in result and result["reason"] not in _PROVIDER_REASONS:
            return unknown()
        return sanitize_result(result)
    except (ValueError, TypeError, UnicodeError):
        return unknown()


async def _discard_stderr(process: WindowsOwnedProcess) -> None:
    try:
        while await process.stderr.read(64 * 1024):
            pass
    except Exception:
        pass


async def _finish_helper(
    launch: asyncio.Task[WindowsOwnedProcess],
    stderr_task: asyncio.Task[None] | None,
) -> None:
    """Retain this helper's private Job until its own accounting proves drain."""
    try:
        process = await asyncio.shield(launch)
    except (Exception, asyncio.CancelledError):
        return
    if stderr_task is None:
        stderr_task = asyncio.create_task(_discard_stderr(process))
    try:
        if process.stdin is not None:
            process.stdin.close()
    except Exception:
        pass
    while True:
        try:
            receipt = await process.drain_after_grace(grace_timeout=0.2, force_timeout=5.0)
            if receipt.status is DrainStatus.DRAINED and receipt.active_processes == 0:
                await process.aclose()
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                return
        except Exception:
            pass
        await asyncio.sleep(1.0)


def _retain_cleanup(task: asyncio.Task[None]) -> None:
    _cleanup_tasks.add(task)

    def completed(done: asyncio.Task[None]) -> None:
        _cleanup_tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)


async def run_provider_text(
    *,
    binding: ProcessBinding,
    selector: Mapping[str, str],
    text: str,
    on_dispatch: Callable[[], None],
) -> dict[str, Any]:
    """Run one private helper and at most one request; never use the shared bridge."""
    refused = validate_request(dict(selector), text)
    if refused is not None:
        return refused
    if os.name != "nt":
        return blocked("unsupported_platform")
    launch: asyncio.Task[WindowsOwnedProcess] | None = None
    stderr_task: asyncio.Task[None] | None = None
    dispatched = False
    try:
        if binding.is_signaled():
            return blocked("process_unavailable")
        from ..setup.bridge import find_or_build_bridge

        bridge_path = await asyncio.to_thread(find_or_build_bridge)
        if not bridge_path:
            return blocked("provider_helper_unavailable")
        request_id = uuid.uuid4().hex
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "provider_text.set",
                    "params": {
                        "pid": binding.pid,
                        "creation_time": binding.creation_time,
                        "selector": dict(selector),
                        "text": text,
                    },
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        launch = asyncio.create_task(
            WindowsOwnedProcess.launch(
                generation=object(),
                argv=(bridge_path, "--provider-text-stdio"),
                cwd=None,
                env=None,
                stdin_mode="pipe",
                stdout_limit=_PROVIDER_RESPONSE_LIMIT,
            )
        )
        process = await asyncio.shield(launch)
        stderr_task = asyncio.create_task(_discard_stderr(process))
        banner = await asyncio.wait_for(
            process.stdout.readline(), timeout=PROVIDER_TEXT_TIMEOUT_SECONDS
        )
        if not _ready_banner(banner):
            return blocked("provider_helper_unavailable")
        if process.stdin is None or binding.is_signaled():
            return blocked("process_unavailable")
        on_dispatch()
        dispatched = True
        process.stdin.write(request)
        await asyncio.wait_for(process.stdin.drain(), timeout=PROVIDER_TEXT_TIMEOUT_SECONDS)
        process.stdin.close()
        line = await asyncio.wait_for(
            process.stdout.readline(), timeout=PROVIDER_TEXT_TIMEOUT_SECONDS
        )
        if not line.endswith(b"\n") or len(line) > _PROVIDER_RESPONSE_LIMIT:
            return unknown()
        trailing = await asyncio.wait_for(
            process.stdout.read(1),
            timeout=PROVIDER_TEXT_TIMEOUT_SECONDS,
        )
        if trailing:
            return unknown()
        return _response(line, request_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        return unknown() if dispatched else blocked("provider_unavailable")
    finally:
        if launch is not None:
            cleanup = asyncio.create_task(_finish_helper(launch, stderr_task))
            _retain_cleanup(cleanup)
            try:
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=6.0)
            except TimeoutError:
                pass

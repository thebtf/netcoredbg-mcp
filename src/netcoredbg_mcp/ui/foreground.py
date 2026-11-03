"""Windows foreground window helpers."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def get_foreground_window() -> int | None:
    """Return the current foreground window HWND on Windows."""
    if os.name != "nt":
        return None

    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception as exc:
        logger.debug("Unable to read foreground window: %s", exc)
        return None


def get_window_process_id(hwnd: int | None) -> int | None:
    """Return the owning process id for a native HWND."""
    if os.name != "nt" or not hwnd:
        return None

    try:
        import ctypes

        pid = ctypes.c_ulong()
        thread_id = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread_id:
            return None
        return int(pid.value)
    except Exception as exc:
        logger.debug("Unable to read process id for HWND %s: %s", hwnd, exc)
        return None


def restore_foreground_window(hwnd: int | None) -> bool:
    """Restore a foreground HWND captured earlier in the same desktop session."""
    if os.name != "nt" or not hwnd:
        return False

    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground_hwnd = int(user32.GetForegroundWindow())
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground_hwnd, None))
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached_threads: list[tuple[int, int]] = []

        for thread_id in {foreground_thread, target_thread}:
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append((current_thread, thread_id))

        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            return int(user32.GetForegroundWindow()) == hwnd
        finally:
            for source_thread, attached_thread in reversed(attached_threads):
                user32.AttachThreadInput(source_thread, attached_thread, False)
    except Exception as exc:
        logger.debug("Unable to restore foreground window %s: %s", hwnd, exc)
        return False


class ForegroundActivation:
    """Pin one HWND and report a single ordinary foreground request."""

    def __init__(self, expected_pid: int) -> None:
        if os.name != "nt":
            raise RuntimeError("Foreground activation is available only on Windows")

        import ctypes
        from ctypes import wintypes

        from .screenshot import get_hwnd_for_pid

        self.expected_pid = expected_pid
        self.hwnd = get_hwnd_for_pid(expected_pid)
        if not self.hwnd:
            raise RuntimeError(f"No visible window for process {expected_pid}")

        # Private declarations preserve pointer-sized HWNDs without changing the
        # shared windll functions used by launch-owned foreground restoration.
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._requested = False
        self._attempted = False
        self._api_result: bool | None = None
        self._ownership_retained = self._window_pid(self.hwnd) == self.expected_pid
        self._activation_error: str | None = None

    def _window_pid(self, hwnd: int | None) -> int | None:
        if not hwnd:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            if not self._user32.IsWindow(hwnd):
                return None
            pid = wintypes.DWORD()
            thread_id = self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value) if thread_id and pid.value else None
        except Exception as exc:
            logger.debug("Unable to observe HWND %s ownership: %s", hwnd, exc)
            return None

    def owns_target(self) -> bool:
        """Revalidate the pinned owner; once lost, ownership is not reacquired."""
        if self._window_pid(self.hwnd) != self.expected_pid:
            self._ownership_retained = False
        return self._ownership_retained

    def request(self, *, is_current: Callable[[], bool]) -> None:
        """Request activation at most once, without input attachment or retries."""
        if self._requested:
            return
        self._requested = True
        if not is_current() or not self.owns_target():
            return

        try:
            self._user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
            if not is_current() or not self.owns_target():
                return
            self._attempted = True
            returned = self._user32.SetForegroundWindow(self.hwnd)
            self._api_result = bool(returned) if returned is not None else None
        except Exception as exc:
            self._activation_error = str(exc) or type(exc).__name__

    def observe(self, *, is_current: Callable[[], bool]) -> dict[str, Any]:
        """Read current ownership; make no claim about foreground after return."""
        current = is_current()
        try:
            foreground_hwnd = int(self._user32.GetForegroundWindow() or 0) or None
        except Exception as exc:
            logger.debug("Unable to observe final foreground window: %s", exc)
            foreground_hwnd = None
        foreground_pid = self._window_pid(foreground_hwnd)
        observed_pid = (
            foreground_pid if foreground_hwnd == self.hwnd else self._window_pid(self.hwnd)
        )
        if observed_pid != self.expected_pid:
            self._ownership_retained = False
        matches = None
        if observed_pid is not None and foreground_hwnd is not None and foreground_pid is not None:
            matches = (
                observed_pid == self.expected_pid
                and foreground_hwnd == self.hwnd
                and foreground_pid == self.expected_pid
            )
        result = {
            "activated": (
                self._attempted
                and self._ownership_retained
                and current
                and is_current()
                and matches is True
            ),
            "hwnd": self.hwnd,
            "target": {
                "hwnd": self.hwnd,
                "expected_pid": self.expected_pid,
                "observed_pid": observed_pid,
            },
            "foreground": {"hwnd": foreground_hwnd, "pid": foreground_pid},
            "foreground_matches_target": matches,
            "activation_requested": True,
            "activation_attempted": self._attempted,
            "activation_api_result": self._api_result,
        }
        if self._activation_error is not None:
            result["activation_error"] = self._activation_error
        return result

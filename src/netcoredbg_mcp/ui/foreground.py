"""Windows foreground window helpers."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def get_foreground_window() -> int | None:
    """Return the current foreground window HWND on Windows."""
    if os.name != "nt":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0)
    except Exception as exc:
        logger.debug("Unable to read foreground window: %s", exc)
        return None


def get_window_process_id(hwnd: int | None) -> int | None:
    """Return the owning process id for a native HWND."""
    if os.name != "nt" or not hwnd:
        return None

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        pid = wintypes.DWORD()
        thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
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
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        kernel32 = ctypes.windll.kernel32
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground_hwnd = int(user32.GetForegroundWindow() or 0)
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
            restored = user32.GetForegroundWindow() == hwnd
        finally:
            for source_thread, attached_thread in reversed(attached_threads):
                user32.AttachThreadInput(source_thread, attached_thread, False)
        return restored
    except Exception as exc:
        logger.debug("Unable to restore foreground window %s: %s", hwnd, exc)
        return False

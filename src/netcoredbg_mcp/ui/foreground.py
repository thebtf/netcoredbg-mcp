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

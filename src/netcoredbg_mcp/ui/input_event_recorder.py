"""Win32 input event recorders for runtime smoke provenance windows."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Any

from .input_monitor import (
    InputEventRecorder,
    InputMonitorUnavailableError,
    InputProvenanceEvent,
)

_RID_INPUT = 0x10000003
_RIM_TYPEMOUSE = 0
_RIM_TYPEKEYBOARD = 1
_RIDEV_INPUTSINK = 0x00000100
_HID_USAGE_PAGE_GENERIC = 0x01
_HID_USAGE_GENERIC_MOUSE = 0x02
_HID_USAGE_GENERIC_KEYBOARD = 0x06
_HWND_MESSAGE = -3
_WM_INPUT = 0x00FF
_WM_QUIT = 0x0012
_WH_KEYBOARD_LL = 13
_WH_MOUSE_LL = 14
_LLMHF_INJECTED = 0x00000001
_LLKHF_INJECTED = 0x00000010
_INFINITE = -1

_LRESULT = getattr(wintypes, "LRESULT", wintypes.LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RawMouseButtonData(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _RawMouseButtons(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("buttons", _RawMouseButtonData)]


class _RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("buttons", _RawMouseButtons),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class _RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", wintypes.DWORD),
        ("dwCount", wintypes.DWORD),
        ("bRawData", ctypes.c_byte * 1),
    ]


class _RawInputData(ctypes.Union):
    _fields_ = [("mouse", _RAWMOUSE), ("keyboard", _RAWKEYBOARD), ("hid", _RAWHID)]


class _RAWINPUT(ctypes.Structure):
    _fields_ = [("header", _RAWINPUTHEADER), ("data", _RawInputData)]


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class Win32CompositeInputEventRecorder:
    """Raw Input primary recorder with low-level-hook fallback."""

    def __init__(
        self,
        *,
        raw_recorder: InputEventRecorder | None = None,
        hook_recorder: InputEventRecorder | None = None,
    ) -> None:
        self._raw = raw_recorder or RawInputEventRecorder()
        self._hook = hook_recorder or LowLevelHookInputEventRecorder()
        self._active: dict[tuple[str, str], InputEventRecorder] = {}

    def start(self, key: tuple[str, str]) -> None:
        try:
            self._raw.start(key)
        except InputMonitorUnavailableError:
            self._hook.start(key)
            self._active[key] = self._hook
        else:
            self._active[key] = self._raw

    def stop(self, key: tuple[str, str]) -> None:
        recorder = self._active.get(key)
        if recorder is None:
            raise InputMonitorUnavailableError(
                "input event recorder missing active window"
            )
        recorder.stop(key)

    def drain_events(self, key: tuple[str, str]) -> list[InputProvenanceEvent]:
        recorder = self._active.pop(key, None)
        if recorder is None:
            return []
        return recorder.drain_events(key)


class _ThreadedWin32Recorder:
    _backend_name = "win32.input_event_recorder"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], list[InputProvenanceEvent]] = {}
        self._active: set[tuple[str, str]] = set()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._startup_error: str | None = None
        self._callbacks: list[Any] = []
        self._handles: list[Any] = []

    def start(self, key: tuple[str, str]) -> None:
        self._ensure_thread()
        with self._lock:
            self._events[key] = []
            self._active.add(key)

    def stop(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._active.discard(key)

    def drain_events(self, key: tuple[str, str]) -> list[InputProvenanceEvent]:
        with self._lock:
            return list(self._events.pop(key, []))

    def shutdown(self) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive() or self._thread_id == 0:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        thread.join(timeout=2.0)

    def _ensure_thread(self) -> None:
        if os.name != "nt":
            raise InputMonitorUnavailableError(
                f"{self._backend_name} requires a Windows desktop session"
            )
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name=self._backend_name,
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise InputMonitorUnavailableError(f"{self._backend_name} did not start")
        if self._startup_error is not None:
            raise InputMonitorUnavailableError(self._startup_error)

    def _record(self, event: InputProvenanceEvent) -> None:
        with self._lock:
            for key in tuple(self._active):
                self._events.setdefault(key, []).append(event)

    def _fail_startup(self, reason: str) -> None:
        self._startup_error = reason
        self._ready.set()

    def _thread_main(self) -> None:
        raise NotImplementedError


class RawInputEventRecorder(_ThreadedWin32Recorder):
    """Primary recorder backed by RegisterRawInputDevices/RIDEV_INPUTSINK."""

    _backend_name = "win32.raw_input"

    def _thread_main(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_raw_input_functions(user32, kernel32)
        self._thread_id = int(kernel32.GetCurrentThreadId())

        def wnd_proc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
            if msg == _WM_INPUT:
                self._record_raw_input(user32, lparam)
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        callback = _WNDPROC(wnd_proc)
        self._callbacks.append(callback)
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"netcoredbg_mcp_raw_input_{id(self):x}"
        window_class = _WNDCLASS(
            0,
            callback,
            0,
            0,
            hinstance,
            None,
            None,
            None,
            None,
            class_name,
        )
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._fail_startup(
                f"RegisterClassW failed for Raw Input: {ctypes.get_last_error()}"
            )
            return
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            self._fail_startup(
                f"CreateWindowExW failed for Raw Input: {ctypes.get_last_error()}"
            )
            return
        devices = (_RAWINPUTDEVICE * 2)(
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_MOUSE,
                _RIDEV_INPUTSINK,
                hwnd,
            ),
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_KEYBOARD,
                _RIDEV_INPUTSINK,
                hwnd,
            ),
        )
        if not user32.RegisterRawInputDevices(
            devices, len(devices), ctypes.sizeof(_RAWINPUTDEVICE)
        ):
            self._fail_startup(
                f"RegisterRawInputDevices failed: {ctypes.get_last_error()}"
            )
            return
        self._handles.append(hwnd)
        self._ready.set()
        self._message_loop(user32)

    @staticmethod
    def _configure_raw_input_functions(user32: Any, kernel32: Any) -> None:
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(_RAWINPUTDEVICE),
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.GetRawInputData.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.UINT),
            wintypes.UINT,
        ]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.DefWindowProcW.restype = _LRESULT

    def _record_raw_input(self, user32: Any, raw_input_handle: int) -> None:
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        result = user32.GetRawInputData(
            raw_input_handle, _RID_INPUT, None, ctypes.byref(size), header_size
        )
        if result == wintypes.UINT(-1).value or size.value == 0:
            return
        buffer = ctypes.create_string_buffer(size.value)
        result = user32.GetRawInputData(
            raw_input_handle, _RID_INPUT, buffer, ctypes.byref(size), header_size
        )
        if result == wintypes.UINT(-1).value:
            return
        raw_input = ctypes.cast(buffer, ctypes.POINTER(_RAWINPUT)).contents
        injected = raw_input.header.hDevice in (None, 0)
        if raw_input.header.dwType == _RIM_TYPEMOUSE:
            self._record(
                InputProvenanceEvent(
                    kind="mouse",
                    injected=bool(injected),
                    extra_info=int(raw_input.data.mouse.ulExtraInformation),
                )
            )
        elif raw_input.header.dwType == _RIM_TYPEKEYBOARD:
            self._record(
                InputProvenanceEvent(
                    kind="keyboard",
                    injected=bool(injected),
                    extra_info=int(raw_input.data.keyboard.ExtraInformation),
                )
            )

    def _message_loop(self, user32: Any) -> None:
        msg = _MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


class LowLevelHookInputEventRecorder(_ThreadedWin32Recorder):
    """Fallback recorder backed by WH_MOUSE_LL/WH_KEYBOARD_LL hooks."""

    _backend_name = "win32.low_level_hook"

    def _thread_main(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_hook_functions(user32, kernel32)
        self._thread_id = int(kernel32.GetCurrentThreadId())

        def mouse_proc(n_code: int, wparam: int, lparam: int) -> int:
            if n_code >= 0:
                data = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                self._record(
                    InputProvenanceEvent(
                        kind="mouse",
                        injected=bool(data.flags & _LLMHF_INJECTED),
                        extra_info=int(data.dwExtraInfo),
                    )
                )
            return int(user32.CallNextHookEx(None, n_code, wparam, lparam))

        def keyboard_proc(n_code: int, wparam: int, lparam: int) -> int:
            if n_code >= 0:
                data = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                self._record(
                    InputProvenanceEvent(
                        kind="keyboard",
                        injected=bool(data.flags & _LLKHF_INJECTED),
                        extra_info=int(data.dwExtraInfo),
                    )
                )
            return int(user32.CallNextHookEx(None, n_code, wparam, lparam))

        mouse_callback = _HOOKPROC(mouse_proc)
        keyboard_callback = _HOOKPROC(keyboard_proc)
        self._callbacks.extend([mouse_callback, keyboard_callback])
        hinstance = kernel32.GetModuleHandleW(None)
        mouse_hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, mouse_callback, hinstance, 0
        )
        keyboard_hook = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, keyboard_callback, hinstance, 0
        )
        if not mouse_hook or not keyboard_hook:
            if mouse_hook:
                user32.UnhookWindowsHookEx(mouse_hook)
            if keyboard_hook:
                user32.UnhookWindowsHookEx(keyboard_hook)
            self._fail_startup(f"SetWindowsHookExW failed: {ctypes.get_last_error()}")
            return
        self._handles.extend([mouse_hook, keyboard_hook])
        self._ready.set()
        try:
            self._message_loop(user32)
        finally:
            user32.UnhookWindowsHookEx(mouse_hook)
            user32.UnhookWindowsHookEx(keyboard_hook)

    @staticmethod
    def _configure_hook_functions(user32: Any, kernel32: Any) -> None:
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.restype = _LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

    def _message_loop(self, user32: Any) -> None:
        msg = _MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

"""Runtime input monitor for no-operator smoke confidence."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_DWORD_MODULUS = 2**32
_DWORD_HALF_RANGE = 2**31


class InputMonitorUnavailableError(RuntimeError):
    """Raised when the runtime input monitor cannot read host input state."""


@dataclass(frozen=True)
class LastInputSample:
    """Current desktop-session last-input evidence."""

    last_input_tick_ms: int
    current_tick_ms: int
    backend: str = "windows.GetLastInputInfo"
    scope: str = "current_desktop_session"


LastInputReader = Callable[[], LastInputSample]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _get_last_input_info = _user32.GetLastInputInfo
    _get_last_input_info.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _get_last_input_info.restype = ctypes.c_int
    _get_tick_count = _kernel32.GetTickCount
    _get_tick_count.argtypes = []
    _get_tick_count.restype = ctypes.c_uint32
else:
    _get_last_input_info = None
    _get_tick_count = None


class RuntimeInputMonitor:
    """Stateful monitor backing runtime.input_monitor.check."""

    def __init__(self, *, reader: LastInputReader | None = None) -> None:
        self._reader = reader or read_last_input_sample
        self._baselines: dict[tuple[str, str], LastInputSample] = {}

    def check(self, **kwargs: Any) -> dict[str, Any]:
        window = str(kwargs.get("window") or "unknown")
        key = _transition_key(kwargs)
        try:
            sample = self._reader()
        except InputMonitorUnavailableError as exc:
            return _blocked(str(exc), window=window)
        except Exception as exc:
            return _blocked(f"input monitor read failed: {exc}", window=window)

        if window != "after_action":
            self._baselines[key] = sample
            return {
                "status": "PASS",
                "basis": "windows_last_input_info",
                "window": window,
                "monitor": {"baseline": _sample_payload(sample)},
            }

        baseline = self._baselines.pop(key, None)
        if baseline is None:
            return {
                **_blocked("input monitor missing baseline", window=window),
                "monitor": {"current": _sample_payload(sample)},
            }

        comparison = _compare_dword_ticks(
            baseline.last_input_tick_ms,
            sample.last_input_tick_ms,
        )
        monitor = {
            "baseline": _sample_payload(baseline),
            "current": _sample_payload(sample),
        }
        if comparison == "advanced":
            return {
                "status": "DIRTY",
                "basis": "windows_last_input_info",
                "source": "global_input",
                "window": window,
                "summary": "Windows last-input tick advanced during no-operator window.",
                "monitor": monitor,
            }
        if comparison == "regressed":
            return {
                **_blocked("input monitor tick regressed", window=window),
                "monitor": monitor,
            }
        return {
            "status": "PASS",
            "basis": "windows_last_input_info",
            "window": window,
            "monitor": monitor,
        }


def create_default_runtime_input_monitor() -> RuntimeInputMonitor:
    return RuntimeInputMonitor()


def read_last_input_sample() -> LastInputSample:
    """Read Windows LASTINPUTINFO using the current desktop session."""
    if os.name != "nt" or _get_last_input_info is None or _get_tick_count is None:
        raise InputMonitorUnavailableError(
            "runtime input monitor requires a Windows desktop session"
        )

    last_input = _LASTINPUTINFO()
    last_input.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _get_last_input_info(ctypes.byref(last_input)):
        error = ctypes.get_last_error()
        raise InputMonitorUnavailableError(f"GetLastInputInfo failed: {error}")

    return LastInputSample(
        last_input_tick_ms=int(last_input.dwTime),
        current_tick_ms=int(_get_tick_count()),
    )


def _transition_key(kwargs: dict[str, Any]) -> tuple[str, str]:
    case_id = str(kwargs.get("case_id") or "")
    transition_id = kwargs.get("transition_id")
    if transition_id is not None:
        return case_id, str(transition_id)
    return case_id, f"index:{kwargs.get('transition_index')}"


def _sample_payload(sample: LastInputSample) -> dict[str, Any]:
    return {
        "last_input_tick_ms": int(sample.last_input_tick_ms),
        "current_tick_ms": int(sample.current_tick_ms),
        "idle_ms": _dword_delta(sample.last_input_tick_ms, sample.current_tick_ms),
        "backend": sample.backend,
        "scope": sample.scope,
    }


def _compare_dword_ticks(start: int, end: int) -> str:
    # Microsoft documents LASTINPUTINFO.dwTime and GetTickCount as DWORD ticks;
    # compare modulo 2^32 to handle the normal 49.7-day wrap boundary.
    delta = _dword_delta(start, end)
    if delta == 0:
        return "same"
    if delta < _DWORD_HALF_RANGE:
        return "advanced"
    return "regressed"


def _dword_delta(start: int, end: int) -> int:
    return (int(end) - int(start)) % _DWORD_MODULUS


def _blocked(reason: str, *, window: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "basis": "windows_last_input_info",
        "window": window,
    }

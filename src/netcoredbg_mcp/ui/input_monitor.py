"""Runtime input monitor for no-operator smoke confidence."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .input_signature import RUNNER_INPUT_SIGNATURE

_DWORD_MODULUS = 2**32
_DWORD_HALF_RANGE = 2**31
_VALID_WINDOWS = frozenset({"before_action", "after_action"})


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


@dataclass(frozen=True)
class InputProvenanceEvent:
    """Input event captured during a no-operator confidence window."""

    kind: str
    injected: bool
    extra_info: int | None = None


class InputEventRecorder(Protocol):
    """Lifecycle seam for recording input events across an action window."""

    def start(self, key: tuple[str, str]) -> None: ...

    def stop(self, key: tuple[str, str]) -> None: ...

    def drain_events(self, key: tuple[str, str]) -> list[InputProvenanceEvent]: ...


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

    def __init__(
        self,
        *,
        reader: LastInputReader | None = None,
        event_recorder: InputEventRecorder | None = None,
    ) -> None:
        self._reader = reader or read_last_input_sample
        self._event_recorder = event_recorder
        self._baselines: dict[tuple[str, str], LastInputSample] = {}
        self._event_windows: set[tuple[str, str]] = set()
        self._last_sample: LastInputSample | None = None

    def check(self, **kwargs: Any) -> dict[str, Any]:
        window = str(kwargs.get("window") or "").strip()
        if window not in _VALID_WINDOWS:
            return _blocked(
                "input monitor unsupported window", window=window or "unknown"
            )
        if not str(kwargs.get("case_id") or "").strip():
            return _blocked("input monitor missing case identity", window=window)
        if (
            kwargs.get("transition_id") is None
            and kwargs.get("transition_index") is None
        ):
            return _blocked("input monitor missing transition identity", window=window)

        key = _transition_key(kwargs)

        if self._event_recorder is not None:
            return self._check_event_stream(key=key, window=window)
        try:
            sample = self._reader()
        except InputMonitorUnavailableError as exc:
            return _blocked(str(exc), window=window)
        except Exception as exc:
            return _blocked(f"input monitor read failed: {exc}", window=window)

        if window != "after_action":
            previous = self._last_sample
            if previous is not None:
                comparison = _compare_dword_ticks(
                    previous.last_input_tick_ms,
                    sample.last_input_tick_ms,
                )
                monitor = {
                    "baseline": _sample_payload(previous),
                    "current": _sample_payload(sample),
                }
                if comparison == "advanced":
                    return _dirty(
                        window=window,
                        summary=(
                            "Windows last-input tick advanced between monitored windows."
                        ),
                        monitor=monitor,
                    )
                if comparison == "regressed":
                    return {
                        **_blocked("input monitor tick regressed", window=window),
                        "monitor": monitor,
                    }
            self._baselines[key] = sample
            self._last_sample = sample
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
            return _dirty(
                window=window,
                summary="Windows last-input tick advanced during no-operator window.",
                monitor=monitor,
            )
        if comparison == "regressed":
            return {
                **_blocked("input monitor tick regressed", window=window),
                "monitor": monitor,
            }
        self._last_sample = sample
        return {
            "status": "PASS",
            "basis": "windows_last_input_info",
            "window": window,
            "monitor": monitor,
        }

    def _check_event_stream(
        self, *, key: tuple[str, str], window: str
    ) -> dict[str, Any]:
        recorder = self._event_recorder
        if recorder is None:
            return _blocked("input event recorder unavailable", window=window)
        if window != "after_action":
            try:
                recorder.start(key)
            except InputMonitorUnavailableError as exc:
                return _blocked(str(exc), window=window, basis="input_event_stream")
            except Exception as exc:
                return _blocked(
                    f"input event recorder start failed: {exc}",
                    window=window,
                    basis="input_event_stream",
                )
            self._event_windows.add(key)
            return {
                "status": "PASS",
                "basis": "input_event_stream",
                "window": window,
                "monitor": {"events": []},
            }
        if key not in self._event_windows:
            return _blocked(
                "input event recorder missing active window",
                window=window,
                basis="input_event_stream",
            )
        try:
            recorder.stop(key)
            events = recorder.drain_events(key)
        except InputMonitorUnavailableError as exc:
            return _blocked(str(exc), window=window, basis="input_event_stream")
        except Exception as exc:
            return _blocked(
                f"input event recorder stop failed: {exc}",
                window=window,
                basis="input_event_stream",
            )
        finally:
            self._event_windows.discard(key)
        return {
            "status": "PASS",
            "basis": "input_event_stream",
            "window": window,
            "monitor": {"events": [_event_payload(event) for event in events]},
        }


def create_default_input_event_recorder() -> InputEventRecorder:
    from .input_event_recorder import Win32CompositeInputEventRecorder

    return Win32CompositeInputEventRecorder()


def create_default_runtime_input_monitor() -> RuntimeInputMonitor:
    return RuntimeInputMonitor(event_recorder=create_default_input_event_recorder())


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


def _event_payload(event: InputProvenanceEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": str(event.kind),
        "injected": bool(event.injected),
        "source": _event_source(event),
    }
    if event.extra_info is not None:
        payload["extra_info"] = int(event.extra_info)
    return payload


def _event_source(event: InputProvenanceEvent) -> str:
    if not event.injected:
        return "physical"
    if event.extra_info == RUNNER_INPUT_SIGNATURE:
        return "runner_injected"
    return "foreign_injected"


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


def _blocked(
    reason: str,
    *,
    window: str,
    basis: str = "windows_last_input_info",
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": reason,
        "basis": basis,
        "window": window,
    }


def _dirty(*, window: str, summary: str, monitor: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "DIRTY",
        "basis": "windows_last_input_info",
        "source": "global_input",
        "window": window,
        "summary": summary,
        "monitor": monitor,
    }

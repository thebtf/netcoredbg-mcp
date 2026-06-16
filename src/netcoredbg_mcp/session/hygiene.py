"""Runtime smoke hygiene preflight service."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .state import TerminalStatus


class _BreakpointRegistry(Protocol):
    def get_all(self) -> Mapping[str, Sequence[Any]]: ...
    def get_for_file(self, file: str) -> Sequence[Any]: ...


class _HygieneSession(Protocol):
    @property
    def breakpoints(self) -> _BreakpointRegistry: ...

    @property
    def state(self) -> Any: ...

    @property
    def is_active(self) -> bool: ...

    async def clear_breakpoints(self, file: str | None = None) -> int: ...
    async def configure_exception_breakpoints(self, filters: list[str]) -> bool: ...


@dataclass(frozen=True)
class CleanupError:
    """Serializable cleanup failure evidence."""

    operation: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"operation": self.operation, "error": self.error}


@dataclass(frozen=True)
class BreakpointEvidence:
    """Compact evidence for a breakpoint that survived cleanup."""

    file: str
    line: int
    dap_line: int | None
    condition: str | None
    verified: bool

    @classmethod
    def from_breakpoint(cls, file_hint: str, bp: Any) -> BreakpointEvidence:
        file_path = getattr(bp, "file", file_hint)
        return cls(
            file=os.path.normpath(str(file_path)),
            line=int(getattr(bp, "line", 0)),
            dap_line=getattr(bp, "dap_line", None),
            condition=getattr(bp, "condition", None),
            verified=bool(getattr(bp, "verified", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "dap_line": self.dap_line,
            "condition": self.condition,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class HygienePreflightResult:
    """Terminal hygiene preflight result."""

    status: TerminalStatus
    reason: str
    cleared: dict[str, int] = field(default_factory=dict)
    remaining_breakpoints: tuple[BreakpointEvidence, ...] = ()
    cleanup_errors: tuple[CleanupError, ...] = ()
    validation_error: str | None = None
    tracepoints_removed: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TerminalStatus(self.status))
        object.__setattr__(self, "cleared", dict(self.cleared))
        object.__setattr__(self, "remaining_breakpoints", tuple(self.remaining_breakpoints))
        object.__setattr__(self, "cleanup_errors", tuple(self.cleanup_errors))
        object.__setattr__(self, "tracepoints_removed", int(self.tracepoints_removed))

    @classmethod
    def validation_failed(cls, error: str) -> HygienePreflightResult:
        return cls(
            status=TerminalStatus.FAIL,
            reason="invalid file scope",
            cleared=_empty_counts(),
            validation_error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "reason": self.reason,
            "cleared": dict(self.cleared),
            "remaining_breakpoints": [bp.to_dict() for bp in self.remaining_breakpoints],
            "cleanup_errors": [error.to_dict() for error in self.cleanup_errors],
        }
        if self.validation_error is not None:
            result["validation_error"] = self.validation_error
        if self.tracepoints_removed:
            result["tracepoints_removed"] = self.tracepoints_removed
        return result


def _empty_counts() -> dict[str, int]:
    return {
        "breakpoints": 0,
        "trace_log_entries": 0,
        "exception_filters": 0,
    }


class RuntimeHygieneService:
    """Clear debugger state that can contaminate runtime smoke scenarios."""

    def __init__(self, session: _HygieneSession) -> None:
        self._session = session

    async def preflight(
        self,
        *,
        file: str | None = None,
        clear_breakpoints: bool = True,
        clear_trace_log: bool = True,
        clear_exception_filters: bool = False,
    ) -> HygienePreflightResult:
        cleared = _empty_counts()
        cleanup_errors: list[CleanupError] = []
        tracepoints_removed = 0

        if clear_trace_log:
            try:
                cleared["trace_log_entries"] = self._clear_trace_log()
            except Exception as exc:
                cleanup_errors.append(CleanupError("clear_trace_log", str(exc)))

        if clear_breakpoints:
            try:
                tracepoints_removed = self._clear_tracepoints(file)
            except Exception as exc:
                cleanup_errors.append(CleanupError("clear_tracepoints", str(exc)))
            try:
                cleared["breakpoints"] = await self._session.clear_breakpoints(file)
            except Exception as exc:
                cleanup_errors.append(CleanupError("clear_breakpoints", str(exc)))

        if clear_exception_filters:
            try:
                cleared["exception_filters"] = await self._clear_exception_filters()
            except Exception as exc:
                cleanup_errors.append(CleanupError("clear_exception_filters", str(exc)))

        remaining = self._remaining_breakpoints(file)
        if remaining:
            return HygienePreflightResult(
                status=TerminalStatus.FAIL,
                reason="targeted breakpoints remain after hygiene preflight",
                cleared=cleared,
                remaining_breakpoints=tuple(remaining),
                cleanup_errors=tuple(cleanup_errors),
                tracepoints_removed=tracepoints_removed,
            )
        if cleanup_errors:
            return HygienePreflightResult(
                status=TerminalStatus.FAIL,
                reason="hygiene cleanup failed",
                cleared=cleared,
                cleanup_errors=tuple(cleanup_errors),
                tracepoints_removed=tracepoints_removed,
            )
        return HygienePreflightResult(
            status=TerminalStatus.PASS,
            reason="no targeted breakpoints remain after hygiene preflight",
            cleared=cleared,
            tracepoints_removed=tracepoints_removed,
        )

    def _clear_trace_log(self) -> int:
        manager = getattr(self._session, "_tracepoint_manager", None)
        if manager is None:
            return 0
        return int(manager.clear_log())

    def _clear_tracepoints(self, file: str | None) -> int:
        manager = getattr(self._session, "_tracepoint_manager", None)
        remove = getattr(manager, "remove", None)
        if manager is None or not callable(remove):
            return 0
        tracepoints = getattr(manager, "tracepoints", {})
        if not isinstance(tracepoints, Mapping):
            return 0
        removed = 0
        for tracepoint_id, tracepoint in list(tracepoints.items()):
            if not _tracepoint_in_scope(tracepoint, file):
                continue
            if remove(str(tracepoint_id)) is not None:
                removed += 1
        return removed

    async def _clear_exception_filters(self) -> int:
        if not bool(getattr(self._session, "is_active", False)):
            return 0
        success = await self._session.configure_exception_breakpoints([])
        if not success:
            raise RuntimeError("Failed to clear exception filters")
        return 1

    def _remaining_breakpoints(self, file: str | None) -> list[BreakpointEvidence]:
        if file is not None:
            return [
                BreakpointEvidence.from_breakpoint(file, bp)
                for bp in self._session.breakpoints.get_for_file(file)
            ]

        remaining: list[BreakpointEvidence] = []
        for file_path, breakpoints in self._session.breakpoints.get_all().items():
            remaining.extend(
                BreakpointEvidence.from_breakpoint(file_path, bp) for bp in breakpoints
            )
        return remaining


def _tracepoint_in_scope(tracepoint: Any, file: str | None) -> bool:
    if file is None:
        return True
    tracepoint_file = str(getattr(tracepoint, "file", "") or "")
    if not tracepoint_file:
        return False
    return _normalize_tracepoint_path(tracepoint_file) == _normalize_tracepoint_path(file)


def _normalize_tracepoint_path(path: str) -> str:
    return os.path.normcase(path.replace("\\", "/"))

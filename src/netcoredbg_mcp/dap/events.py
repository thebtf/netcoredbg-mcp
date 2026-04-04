"""DAP Event types and constants."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StopReason(str, Enum):
    """Reasons for stopped event."""
    BREAKPOINT = "breakpoint"
    STEP = "step"
    EXCEPTION = "exception"
    PAUSE = "pause"
    ENTRY = "entry"
    GOTO = "goto"
    FUNCTION_BREAKPOINT = "function breakpoint"
    DATA_BREAKPOINT = "data breakpoint"


class OutputCategory(str, Enum):
    """Output event categories."""
    CONSOLE = "console"
    STDOUT = "stdout"
    STDERR = "stderr"
    TELEMETRY = "telemetry"


class ThreadReason(str, Enum):
    """Thread event reasons."""
    STARTED = "started"
    EXITED = "exited"


@dataclass
class StoppedEventBody:
    """Body of stopped event."""
    reason: StopReason
    thread_id: int | None = None
    all_threads_stopped: bool = True
    description: str | None = None
    text: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoppedEventBody":
        return cls(
            reason=StopReason(data.get("reason", "breakpoint")),
            thread_id=data.get("threadId"),
            all_threads_stopped=data.get("allThreadsStopped", True),
            description=data.get("description"),
            text=data.get("text"),
        )


@dataclass
class OutputEventBody:
    """Body of output event."""
    category: OutputCategory
    output: str
    source: str | None = None
    line: int | None = None
    column: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutputEventBody":
        return cls(
            category=OutputCategory(data.get("category", "console")),
            output=data.get("output", ""),
            source=data.get("source", {}).get("path") if data.get("source") else None,
            line=data.get("line"),
            column=data.get("column"),
        )


@dataclass
class ThreadEventBody:
    """Body of thread event."""
    reason: ThreadReason
    thread_id: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadEventBody":
        return cls(
            reason=ThreadReason(data.get("reason", "started")),
            thread_id=data.get("threadId", 0),
        )


@dataclass
class ExitedEventBody:
    """Body of exited event."""
    exit_code: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExitedEventBody":
        return cls(exit_code=data.get("exitCode", 0))


@dataclass
class BreakpointEventBody:
    """Body of breakpoint event (breakpoint added/changed/removed by adapter)."""
    reason: str  # "new", "changed", "removed"
    breakpoint_id: int | None = None
    verified: bool = False
    line: int | None = None
    source_path: str | None = None
    message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BreakpointEventBody":
        bp = data.get("breakpoint", {})
        source = bp.get("source", {})
        return cls(
            reason=data.get("reason", "changed"),
            breakpoint_id=bp.get("id"),
            verified=bp.get("verified", False),
            line=bp.get("line"),
            source_path=source.get("path") if source else None,
            message=bp.get("message"),
        )


@dataclass
class ModuleEventBody:
    """Body of module event (assembly loaded/changed/unloaded)."""
    reason: str  # "new", "changed", "removed"
    module_id: int | str = 0
    name: str = ""
    path: str | None = None
    version: str | None = None
    is_optimized: bool = False
    symbol_status: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModuleEventBody":
        module = data.get("module", {})
        return cls(
            reason=data.get("reason", "new"),
            module_id=module.get("id", 0),
            name=module.get("name", ""),
            path=module.get("path"),
            version=module.get("version"),
            is_optimized=module.get("isOptimized", False),
            symbol_status=module.get("symbolStatus"),
        )

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

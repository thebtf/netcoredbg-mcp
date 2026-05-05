"""DAP Event types and constants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


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
    def from_dict(cls, data: dict[str, Any]) -> StoppedEventBody:
        return cls(
            reason=StopReason(data.get("reason", "breakpoint")),
            thread_id=data.get("threadId"),
            all_threads_stopped=data.get("allThreadsStopped", True),
            description=data.get("description"),
            text=data.get("text"),
        )


@dataclass
class InitializedEventBody:
    """Body of initialized event."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InitializedEventBody:
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {}


@dataclass
class ContinuedEventBody:
    """Body of continued event."""
    thread_id: int | None = None
    all_threads_continued: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContinuedEventBody:
        return cls(
            thread_id=data.get("threadId"),
            all_threads_continued=data.get("allThreadsContinued", True),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"allThreadsContinued": self.all_threads_continued}
        if self.thread_id is not None:
            result["threadId"] = self.thread_id
        return result


@dataclass
class TerminatedEventBody:
    """Body of terminated event."""
    restart: Any | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TerminatedEventBody:
        return cls(restart=data.get("restart"))

    def to_dict(self) -> dict[str, Any]:
        return {} if self.restart is None else {"restart": self.restart}


@dataclass
class ProcessEventBody:
    """Body of process event."""
    name: str | None = None
    system_process_id: int | None = None
    is_local_process: bool = True
    start_method: str = "launch"
    pointer_size: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessEventBody:
        return cls(
            name=str(data["name"]) if data.get("name") is not None else None,
            system_process_id=_optional_int(data.get("systemProcessId")),
            is_local_process=data.get("isLocalProcess", True),
            start_method=data.get("startMethod", "launch"),
            pointer_size=_optional_int(data.get("pointerSize")),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "isLocalProcess": self.is_local_process,
            "startMethod": self.start_method,
        }
        if self.name is not None:
            result["name"] = self.name
        if self.system_process_id is not None:
            result["systemProcessId"] = self.system_process_id
        if self.pointer_size is not None:
            result["pointerSize"] = self.pointer_size
        return result


@dataclass
class OutputEventBody:
    """Body of output event."""
    category: OutputCategory
    output: str
    source: str | None = None
    line: int | None = None
    column: int | None = None
    variables_reference: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputEventBody:
        return cls(
            category=OutputCategory(data.get("category", "console")),
            output=data.get("output", ""),
            source=data.get("source", {}).get("path") if data.get("source") else None,
            line=data.get("line"),
            column=data.get("column"),
            variables_reference=data.get("variablesReference", 0),
        )


@dataclass
class ThreadEventBody:
    """Body of thread event."""
    reason: ThreadReason
    thread_id: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadEventBody:
        return cls(
            reason=ThreadReason(data.get("reason", "started")),
            thread_id=data.get("threadId", 0),
        )


@dataclass
class ExitedEventBody:
    """Body of exited event."""
    exit_code: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExitedEventBody:
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
    def from_dict(cls, data: dict[str, Any]) -> BreakpointEventBody:
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
    def from_dict(cls, data: dict[str, Any]) -> ModuleEventBody:
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


@dataclass
class CapabilitiesEventBody:
    """Body of capabilities event."""
    capabilities: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilitiesEventBody:
        capabilities = data.get("capabilities", {})
        return cls(capabilities=dict(capabilities) if isinstance(capabilities, dict) else {})

    def to_dict(self) -> dict[str, Any]:
        return {"capabilities": dict(self.capabilities)}


@dataclass
class InvalidatedEventBody:
    """Body of invalidated event."""
    areas: list[str]
    thread_id: int | None = None
    stack_frame_id: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvalidatedEventBody:
        return cls(
            areas=list(data.get("areas", [])),
            thread_id=data.get("threadId"),
            stack_frame_id=data.get("stackFrameId"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"areas": list(self.areas)}
        if self.thread_id is not None:
            result["threadId"] = self.thread_id
        if self.stack_frame_id is not None:
            result["stackFrameId"] = self.stack_frame_id
        return result


@dataclass
class LoadedSourceEventBody:
    """Body of loadedSource event."""
    reason: Literal["new", "changed", "removed"]
    source: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoadedSourceEventBody:
        reason = data.get("reason", "new")
        if reason not in ("new", "changed", "removed"):
            reason = "changed"
        source = data.get("source", {})
        return cls(
            reason=reason,
            source=dict(source) if isinstance(source, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "source": dict(self.source)}


@dataclass
class ProgressStartEventBody:
    """Body of progressStart event."""
    progress_id: str
    title: str
    request_id: int | None = None
    cancellable: bool = False
    message: str | None = None
    percentage: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressStartEventBody:
        return cls(
            progress_id=str(data.get("progressId", "")),
            title=str(data.get("title", "")),
            request_id=data.get("requestId"),
            cancellable=data.get("cancellable", False),
            message=data.get("message"),
            percentage=_optional_float(data.get("percentage")),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "progressId": self.progress_id,
            "title": self.title,
            "cancellable": self.cancellable,
        }
        if self.request_id is not None:
            result["requestId"] = self.request_id
        if self.message is not None:
            result["message"] = self.message
        if self.percentage is not None:
            result["percentage"] = self.percentage
        return result


@dataclass
class ProgressUpdateEventBody:
    """Body of progressUpdate event."""
    progress_id: str
    message: str | None = None
    percentage: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressUpdateEventBody:
        return cls(
            progress_id=str(data.get("progressId", "")),
            message=data.get("message"),
            percentage=_optional_float(data.get("percentage")),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"progressId": self.progress_id}
        if self.message is not None:
            result["message"] = self.message
        if self.percentage is not None:
            result["percentage"] = self.percentage
        return result


@dataclass
class ProgressEndEventBody:
    """Body of progressEnd event."""
    progress_id: str
    message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressEndEventBody:
        return cls(
            progress_id=str(data.get("progressId", "")),
            message=data.get("message"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"progressId": self.progress_id}
        if self.message is not None:
            result["message"] = self.message
        return result


@dataclass
class MemoryEventBody:
    """Body of memory event."""
    memory_reference: str
    offset: int = 0
    count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEventBody:
        return cls(
            memory_reference=str(data.get("memoryReference", "")),
            offset=data.get("offset", 0),
            count=data.get("count", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "memoryReference": self.memory_reference,
            "offset": self.offset,
            "count": self.count,
        }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

"""Debug session state management."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DebugState(str, Enum):
    """Debug session states."""
    IDLE = "idle"  # No active session
    INITIALIZING = "initializing"  # DAP initializing
    CONFIGURED = "configured"  # Breakpoints set, ready to run
    RUNNING = "running"  # Program executing
    STOPPED = "stopped"  # Hit breakpoint/paused
    TERMINATED = "terminated"  # Program ended


@dataclass
class Breakpoint:
    """Represents a breakpoint."""
    file: str
    line: int
    condition: str | None = None
    hit_condition: str | None = None
    log_message: str | None = None
    verified: bool = False
    id: int | None = None

    def to_dap(self) -> dict[str, Any]:
        """Convert to DAP breakpoint format."""
        bp: dict[str, Any] = {"line": self.line}
        if self.condition:
            bp["condition"] = self.condition
        if self.hit_condition:
            bp["hitCondition"] = self.hit_condition
        if self.log_message:
            bp["logMessage"] = self.log_message
        return bp


@dataclass
class FunctionBreakpoint:
    """Represents a function breakpoint."""
    name: str
    condition: str | None = None
    hit_condition: str | None = None
    verified: bool = False
    id: int | None = None

    def to_dap(self) -> dict[str, Any]:
        bp: dict[str, Any] = {"name": self.name}
        if self.condition:
            bp["condition"] = self.condition
        if self.hit_condition:
            bp["hitCondition"] = self.hit_condition
        return bp


class BreakpointRegistry:
    """Manages breakpoints across files."""

    def __init__(self):
        self._breakpoints: dict[str, list[Breakpoint]] = {}  # file -> breakpoints
        self._function_breakpoints: list[FunctionBreakpoint] = []

    def add(self, breakpoint: Breakpoint) -> None:
        """Add a breakpoint."""
        file_path = self._normalize_path(breakpoint.file)
        if file_path not in self._breakpoints:
            self._breakpoints[file_path] = []

        # Check for duplicate (immutable update — replace with new object)
        for i, bp in enumerate(self._breakpoints[file_path]):
            if bp.line == breakpoint.line:
                self._breakpoints[file_path][i] = Breakpoint(
                    file=bp.file,
                    line=bp.line,
                    condition=breakpoint.condition,
                    hit_condition=breakpoint.hit_condition,
                    log_message=breakpoint.log_message,
                    verified=bp.verified,
                    id=bp.id,
                )
                return

        self._breakpoints[file_path].append(breakpoint)

    def remove(self, file: str, line: int) -> bool:
        """Remove a breakpoint. Returns True if found."""
        file_path = self._normalize_path(file)
        if file_path not in self._breakpoints:
            return False

        original_count = len(self._breakpoints[file_path])
        self._breakpoints[file_path] = [
            bp for bp in self._breakpoints[file_path] if bp.line != line
        ]

        if not self._breakpoints[file_path]:
            del self._breakpoints[file_path]

        return len(self._breakpoints.get(file_path, [])) < original_count

    def clear(self, file: str | None = None) -> int:
        """Clear breakpoints. Returns count removed."""
        if file:
            file_path = self._normalize_path(file)
            if file_path in self._breakpoints:
                count = len(self._breakpoints[file_path])
                del self._breakpoints[file_path]
                return count
            return 0
        else:
            count = sum(len(bps) for bps in self._breakpoints.values())
            self._breakpoints.clear()
            return count

    def get_for_file(self, file: str) -> list[Breakpoint]:
        """Get breakpoints for a file."""
        file_path = self._normalize_path(file)
        return self._breakpoints.get(file_path, [])

    def get_all(self) -> dict[str, list[Breakpoint]]:
        """Get all breakpoints."""
        return dict(self._breakpoints)

    def get_files(self) -> list[str]:
        """Get files with breakpoints."""
        return list(self._breakpoints.keys())

    def update_from_dap(self, file: str, dap_breakpoints: list[dict[str, Any]]) -> None:
        """Update breakpoints from DAP response."""
        file_path = self._normalize_path(file)
        if file_path not in self._breakpoints:
            return

        for i, dap_bp in enumerate(dap_breakpoints):
            if i < len(self._breakpoints[file_path]):
                self._breakpoints[file_path][i].verified = dap_bp.get("verified", False)
                self._breakpoints[file_path][i].id = dap_bp.get("id")
                # Update line if adjusted by debugger
                if "line" in dap_bp:
                    self._breakpoints[file_path][i].line = dap_bp["line"]

    def add_function_breakpoint(self, bp: FunctionBreakpoint) -> None:
        """Add a function breakpoint."""
        # Replace existing if same name (immutable update — create new object)
        for i, existing in enumerate(self._function_breakpoints):
            if existing.name == bp.name:
                self._function_breakpoints[i] = FunctionBreakpoint(
                    name=bp.name,
                    condition=bp.condition,
                    hit_condition=bp.hit_condition,
                    verified=existing.verified,
                    id=existing.id,
                )
                return
        self._function_breakpoints.append(bp)

    def remove_function_breakpoint(self, name: str) -> bool:
        """Remove a function breakpoint by name. Returns True if found."""
        original_count = len(self._function_breakpoints)
        self._function_breakpoints = [
            bp for bp in self._function_breakpoints if bp.name != name
        ]
        return len(self._function_breakpoints) < original_count

    def get_function_breakpoints(self) -> list[FunctionBreakpoint]:
        """Get all function breakpoints."""
        return list(self._function_breakpoints)

    def clear_function_breakpoints(self) -> int:
        """Clear all function breakpoints. Returns count removed."""
        count = len(self._function_breakpoints)
        self._function_breakpoints = []
        return count

    def _normalize_path(self, path: str) -> str:
        """Normalize file path for consistent lookup."""
        # Convert backslashes to forward slashes and lowercase on Windows
        import os
        normalized = os.path.normpath(path)
        if os.name == "nt":
            normalized = normalized.lower()
        return normalized


@dataclass
class ThreadInfo:
    """Thread information."""
    id: int
    name: str


@dataclass
class StackFrame:
    """Stack frame information."""
    id: int
    name: str
    source: str | None
    line: int
    column: int


@dataclass
class Variable:
    """Variable information."""
    name: str
    value: str
    type: str | None
    variables_reference: int
    named_variables: int = 0
    indexed_variables: int = 0


@dataclass
class StoppedSnapshot:
    """Snapshot of state when execution stops (or times out).

    Returned by SessionManager.wait_for_stopped() to give the agent
    a complete picture of what happened after an execution command.
    """
    state: DebugState
    stop_reason: str | None = None
    thread_id: int | None = None
    timed_out: bool = False
    exit_code: int | None = None
    exception_info: dict[str, Any] | None = None
    process_alive: bool = True


@dataclass
class SessionState:
    """Complete debug session state."""
    state: DebugState = DebugState.IDLE
    current_thread_id: int | None = None
    stop_reason: str | None = None
    threads: list[ThreadInfo] = field(default_factory=list)
    current_frame_id: int | None = None
    output_buffer: deque[str] = field(default_factory=deque)
    exit_code: int | None = None
    exception_info: dict[str, Any] | None = None
    process_id: int | None = None
    process_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "state": self.state.value,
            "currentThreadId": self.current_thread_id,
            "stopReason": self.stop_reason,
            "threads": [{"id": t.id, "name": t.name} for t in self.threads],
            "currentFrameId": self.current_frame_id,
            "exitCode": self.exit_code,
            "exceptionInfo": self.exception_info,
            "processId": self.process_id,
            "processName": self.process_name,
        }

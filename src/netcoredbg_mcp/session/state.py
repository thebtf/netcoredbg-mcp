"""Debug session state management."""

from __future__ import annotations

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


class BreakpointRegistry:
    """Manages breakpoints across files."""

    def __init__(self):
        self._breakpoints: dict[str, list[Breakpoint]] = {}  # file -> breakpoints

    def add(self, breakpoint: Breakpoint) -> None:
        """Add a breakpoint."""
        file_path = self._normalize_path(breakpoint.file)
        if file_path not in self._breakpoints:
            self._breakpoints[file_path] = []

        # Check for duplicate
        for bp in self._breakpoints[file_path]:
            if bp.line == breakpoint.line:
                # Update existing
                bp.condition = breakpoint.condition
                bp.hit_condition = breakpoint.hit_condition
                bp.log_message = breakpoint.log_message
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
class SessionState:
    """Complete debug session state."""
    state: DebugState = DebugState.IDLE
    current_thread_id: int | None = None
    stop_reason: str | None = None
    threads: list[ThreadInfo] = field(default_factory=list)
    current_frame_id: int | None = None
    output_buffer: list[str] = field(default_factory=list)
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

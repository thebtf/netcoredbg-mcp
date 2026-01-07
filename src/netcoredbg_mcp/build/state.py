"""Build state management and result types.

State machine for build sessions:
IDLE → BUILDING → READY | FAILED
     ↑__________________|
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BuildState(str, Enum):
    """Build session state machine states."""

    IDLE = "idle"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BuildErrorSeverity(str, Enum):
    """MSBuild error severity levels."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class BuildDiagnostic:
    """Parsed MSBuild diagnostic (error/warning)."""

    severity: BuildErrorSeverity
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    project: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
        }
        if self.file:
            result["file"] = self.file
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.project:
            result["project"] = self.project
        return result


# MSBuild output patterns
# Format: path(line,col): severity code: message [project]
MSBUILD_DIAGNOSTIC_PATTERN = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?P<severity>error|warning|info)\s+(?P<code>\w+):\s*"
    r"(?P<message>.+?)(?:\s+\[(?P<project>[^\]]+)\])?$",
    re.IGNORECASE,
)

# Simple format without location: severity code: message
MSBUILD_SIMPLE_PATTERN = re.compile(
    r"^(?P<severity>error|warning|info)\s+(?P<code>\w+):\s*(?P<message>.+)$",
    re.IGNORECASE,
)


def parse_msbuild_output(output: str) -> list[BuildDiagnostic]:
    """Parse MSBuild output into structured diagnostics.

    Args:
        output: MSBuild console output

    Returns:
        List of parsed diagnostics
    """
    diagnostics: list[BuildDiagnostic] = []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Try detailed format first
        match = MSBUILD_DIAGNOSTIC_PATTERN.match(line)
        if match:
            severity_str = match.group("severity").lower()
            severity = BuildErrorSeverity(severity_str)
            diagnostics.append(
                BuildDiagnostic(
                    severity=severity,
                    code=match.group("code"),
                    message=match.group("message"),
                    file=match.group("file"),
                    line=int(match.group("line")),
                    column=int(match.group("col")),
                    project=match.group("project"),
                )
            )
            continue

        # Try simple format
        match = MSBUILD_SIMPLE_PATTERN.match(line)
        if match:
            severity_str = match.group("severity").lower()
            severity = BuildErrorSeverity(severity_str)
            diagnostics.append(
                BuildDiagnostic(
                    severity=severity,
                    code=match.group("code"),
                    message=match.group("message"),
                )
            )

    return diagnostics


class BuildError(Exception):
    """Build operation error with diagnostics."""

    def __init__(
        self,
        message: str,
        diagnostics: list[BuildDiagnostic] | None = None,
        exit_code: int | None = None,
    ):
        super().__init__(message)
        self.diagnostics = diagnostics or []
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "error": str(self),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
        if self.exit_code is not None:
            result["exitCode"] = self.exit_code
        return result


@dataclass
class BuildResult:
    """Result of a build operation."""

    success: bool
    state: BuildState
    command: str
    project_path: str
    configuration: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    diagnostics: list[BuildDiagnostic] = field(default_factory=list)
    duration_ms: float = 0.0
    cancelled: bool = False

    def __post_init__(self) -> None:
        """Parse diagnostics from output if not provided."""
        if not self.diagnostics and (self.stdout or self.stderr):
            # Parse from both stdout and stderr
            self.diagnostics = parse_msbuild_output(self.stdout + "\n" + self.stderr)

    @property
    def errors(self) -> list[BuildDiagnostic]:
        """Get only error diagnostics."""
        return [d for d in self.diagnostics if d.severity == BuildErrorSeverity.ERROR]

    @property
    def warnings(self) -> list[BuildDiagnostic]:
        """Get only warning diagnostics."""
        return [d for d in self.diagnostics if d.severity == BuildErrorSeverity.WARNING]

    @property
    def error_count(self) -> int:
        """Count of errors."""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """Count of warnings."""
        return len(self.warnings)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "success": self.success,
            "state": self.state.value,
            "command": self.command,
            "projectPath": self.project_path,
            "configuration": self.configuration,
            "errorCount": self.error_count,
            "warningCount": self.warning_count,
            "durationMs": round(self.duration_ms, 2),
        }
        if self.exit_code is not None:
            result["exitCode"] = self.exit_code
        if self.diagnostics:
            result["diagnostics"] = [d.to_dict() for d in self.diagnostics]
        if self.cancelled:
            result["cancelled"] = True
        return result

    def to_summary(self) -> str:
        """Generate human-readable summary."""
        status = "[OK] Build succeeded" if self.success else "[FAILED] Build failed"
        if self.cancelled:
            status = "[CANCELLED] Build cancelled"

        parts = [
            f"{status}",
            f"  Project: {self.project_path}",
            f"  Configuration: {self.configuration}",
            f"  Duration: {self.duration_ms:.0f}ms",
        ]

        if self.error_count > 0:
            parts.append(f"  Errors: {self.error_count}")
        if self.warning_count > 0:
            parts.append(f"  Warnings: {self.warning_count}")

        # Show first few errors
        for i, err in enumerate(self.errors[:5]):
            location = ""
            if err.file:
                location = f"{err.file}"
                if err.line:
                    location += f"({err.line},{err.column or 0})"
                location += ": "
            parts.append(f"    {location}{err.code}: {err.message}")

        if self.error_count > 5:
            parts.append(f"    ... and {self.error_count - 5} more errors")

        return "\n".join(parts)

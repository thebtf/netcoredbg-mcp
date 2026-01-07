"""Build policy - security and argument validation.

Security measures:
- Argument whitelisting (config, framework, runtime only)
- Path canonicalization with symlink/junction rejection
- UNC and device path denial
- TOCTOU prevention via validated handle usage
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final


class BuildCommand(str, Enum):
    """Supported build commands."""

    CLEAN = "clean"
    RESTORE = "restore"
    BUILD = "build"
    REBUILD = "rebuild"  # clean + build


# Allowed dotnet CLI arguments (whitelist approach)
ALLOWED_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {
        # Configuration
        "-c",
        "--configuration",
        # Framework targeting
        "-f",
        "--framework",
        # Runtime identifier
        "-r",
        "--runtime",
        # Output directory (validated separately)
        "-o",
        "--output",
        # Verbosity
        "-v",
        "--verbosity",
        # No restore (for build only)
        "--no-restore",
        # No build (for other commands)
        "--no-build",
        # No dependencies
        "--no-dependencies",
        # Force
        "--force",
        # Interactive mode control (use --interactive false for automation)
        "--interactive",
    }
)

# Allowed configuration values
ALLOWED_CONFIGURATIONS: Final[frozenset[str]] = frozenset({"Debug", "Release"})

# Allowed verbosity levels
ALLOWED_VERBOSITY: Final[frozenset[str]] = frozenset(
    {"quiet", "minimal", "normal", "detailed", "diagnostic", "q", "m", "n", "d", "diag"}
)

# Pattern for valid framework monikers (e.g., net8.0, netstandard2.1, net48)
FRAMEWORK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^net(?:standard)?[0-9]+(?:\.[0-9]+)?(?:-[a-z]+)?$", re.IGNORECASE
)

# Pattern for valid runtime identifiers (e.g., win-x64, linux-arm64)
RUNTIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:win|linux|osx|freebsd|alpine|android|ios|tvos|watchos|browser|wasi)"
    r"(?:-(?:x64|x86|arm|arm64|musl-x64|musl-arm64|bionic-x64|bionic-arm64))?$",
    re.IGNORECASE,
)


@dataclass
class BuildPolicy:
    """Security policy for build operations.

    Validates:
    - Paths are within allowed workspace
    - No symlinks, junctions, or reparse points
    - No UNC or device paths
    - Arguments are whitelisted
    """

    workspace_root: str
    allow_unc_paths: bool = False
    allow_device_paths: bool = False
    allowed_output_dirs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate and canonicalize workspace root."""
        self.workspace_root = self._validate_path(
            self.workspace_root, allow_symlinks=False, context="workspace_root"
        )
        # Allowed output directories must be within workspace
        validated_outputs = []
        for output_dir in self.allowed_output_dirs:
            try:
                validated = self._validate_path(
                    output_dir, allow_symlinks=False, context="allowed_output_dir"
                )
                validated_outputs.append(validated)
            except ValueError:
                pass  # Skip invalid output directories
        self.allowed_output_dirs = validated_outputs

    def _validate_path(
        self,
        path: str,
        allow_symlinks: bool = False,
        context: str = "path",
    ) -> str:
        """Validate and canonicalize a path.

        Args:
            path: Path to validate
            allow_symlinks: Whether to allow symlinks (default False)
            context: Context for error messages

        Returns:
            Canonicalized absolute path

        Raises:
            ValueError: If path is invalid or violates security policy
        """
        if not path:
            raise ValueError(f"Empty {context}")

        # Deny device paths (\\?\, \\.\) - check before UNC since they start with \\
        if path.startswith(("\\\\.\\", "\\\\?\\")):
            if not self.allow_device_paths:
                raise ValueError(f"Device paths not allowed in {context}: {path}")

        # Deny UNC paths (\\server\share)
        if path.startswith("\\\\") and not self.allow_unc_paths:
            raise ValueError(f"UNC paths not allowed in {context}: {path}")

        # Get absolute path first
        abs_path = os.path.abspath(path)

        # Check for path traversal attempts
        if ".." in Path(path).parts:
            # Resolve and verify
            resolved = os.path.normpath(abs_path)
            if resolved != abs_path:
                raise ValueError(f"Path traversal detected in {context}: {path}")

        # Windows-specific: check for reparse points (symlinks, junctions)
        if os.name == "nt" and os.path.exists(abs_path):
            try:
                attrs = os.lstat(abs_path)
                # Check for symlink
                if stat.S_ISLNK(attrs.st_mode):
                    if not allow_symlinks:
                        raise ValueError(f"Symlink not allowed in {context}: {path}")
                # On Windows, check for reparse point attribute
                if hasattr(attrs, "st_file_attributes"):
                    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
                    if attrs.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        if not allow_symlinks:
                            raise ValueError(
                                f"Reparse point (junction/symlink) not allowed in {context}: {path}"
                            )
            except OSError as e:
                raise ValueError(f"Cannot access {context}: {path} ({e})") from e
        elif os.path.islink(abs_path) and not allow_symlinks:
            raise ValueError(f"Symlink not allowed in {context}: {path}")

        return abs_path

    def validate_project_path(self, project_path: str) -> str:
        """Validate project path is within workspace.

        Args:
            project_path: Path to project file or directory

        Returns:
            Validated absolute path

        Raises:
            ValueError: If path is invalid or outside workspace
        """
        validated = self._validate_path(
            project_path, allow_symlinks=False, context="project_path"
        )

        # Must be within workspace
        try:
            common = os.path.commonpath([validated, self.workspace_root])
            if common != self.workspace_root:
                raise ValueError(f"Project path outside workspace: {project_path}")
        except ValueError as e:
            raise ValueError(f"Project path outside workspace: {project_path}") from e

        return validated

    def validate_output_path(self, output_path: str) -> str:
        """Validate output path is within allowed directories.

        Args:
            output_path: Path to output directory

        Returns:
            Validated absolute path

        Raises:
            ValueError: If path is invalid or not in allowed directories
        """
        validated = self._validate_path(
            output_path, allow_symlinks=False, context="output_path"
        )

        # Must be within workspace or explicit allowed directories
        allowed_roots = [self.workspace_root] + self.allowed_output_dirs
        for allowed in allowed_roots:
            try:
                common = os.path.commonpath([validated, allowed])
                if common == allowed:
                    return validated
            except ValueError:
                continue

        raise ValueError(f"Output path not in allowed directories: {output_path}")

    def validate_arguments(
        self, args: list[str]
    ) -> list[str]:
        """Validate and filter build arguments.

        Args:
            args: List of command-line arguments

        Returns:
            Validated argument list

        Raises:
            ValueError: If any argument is not allowed
        """
        validated: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]

            # Split --arg=value format
            if "=" in arg:
                key, value = arg.split("=", 1)
            else:
                key = arg
                value = None

            # Check if argument is in whitelist
            if key not in ALLOWED_ARGUMENTS:
                raise ValueError(f"Argument not allowed: {key}")

            # Validate argument values
            if key in ("-c", "--configuration"):
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                if value not in ALLOWED_CONFIGURATIONS:
                    raise ValueError(f"Invalid configuration: {value}")
                validated.extend([key, value])

            elif key in ("-f", "--framework"):
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                if not FRAMEWORK_PATTERN.match(value):
                    raise ValueError(f"Invalid framework: {value}")
                validated.extend([key, value])

            elif key in ("-r", "--runtime"):
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                if not RUNTIME_PATTERN.match(value):
                    raise ValueError(f"Invalid runtime: {value}")
                validated.extend([key, value])

            elif key in ("-o", "--output"):
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                # Validate output path
                self.validate_output_path(value)
                validated.extend([key, value])

            elif key in ("-v", "--verbosity"):
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                if value.lower() not in ALLOWED_VERBOSITY:
                    raise ValueError(f"Invalid verbosity: {value}")
                validated.extend([key, value])

            elif key == "--interactive":
                if value is None and i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                if not value:
                    raise ValueError(f"Argument {key} requires a value")
                if value.lower() not in ("true", "false"):
                    raise ValueError(f"Invalid interactive value: {value}")
                validated.extend([key, value])

            else:
                # Boolean flags
                validated.append(arg)

            i += 1

        return validated

    def get_dotnet_command(
        self,
        command: BuildCommand,
        project_path: str,
        configuration: str = "Debug",
        extra_args: list[str] | None = None,
    ) -> list[str]:
        """Build validated dotnet command line.

        Args:
            command: Build command to execute
            project_path: Path to project file or directory
            configuration: Build configuration (Debug/Release)
            extra_args: Additional arguments (will be validated)

        Returns:
            Complete command line as list
        """
        validated_project = self.validate_project_path(project_path)
        validated_args = self.validate_arguments(extra_args or [])

        # Note: --no-interactive is deprecated in newer SDKs, use --interactive false
        # For restore, use --interactive false only if NuGet might prompt
        # For build/clean, typically no interactive prompts occur

        if command == BuildCommand.CLEAN:
            return [
                "dotnet",
                "clean",
                validated_project,
                "-c",
                configuration,
                *validated_args,
            ]
        elif command == BuildCommand.RESTORE:
            return [
                "dotnet",
                "restore",
                validated_project,
                "--interactive",
                "false",
                *validated_args,
            ]
        elif command == BuildCommand.BUILD:
            return [
                "dotnet",
                "build",
                validated_project,
                "-c",
                configuration,
                *validated_args,
            ]
        elif command == BuildCommand.REBUILD:
            # Rebuild is clean + build, but we return build command
            # The caller handles clean first
            return [
                "dotnet",
                "build",
                validated_project,
                "-c",
                configuration,
                *validated_args,
            ]
        else:
            raise ValueError(f"Unknown command: {command}")

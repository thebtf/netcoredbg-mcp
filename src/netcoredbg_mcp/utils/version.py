"""Version detection and compatibility checking for .NET debugging."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VersionInfo:
    """Version information with major.minor.patch components."""

    major: int
    minor: int
    patch: int
    build: int | None = None
    raw: str = ""

    def __str__(self) -> str:
        if self.build is not None:
            return f"{self.major}.{self.minor}.{self.patch}.{self.build}"
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def from_string(cls, version_str: str) -> VersionInfo | None:
        """Parse version from string like '6.0.36' or '9.0.13.2701'."""
        if not version_str:
            return None

        # Match version patterns: major.minor.patch or major.minor.patch.build
        match = re.match(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", version_str)
        if not match:
            return None

        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3))
        build = int(match.group(4)) if match.group(4) else None

        return cls(major=major, minor=minor, patch=patch, build=build, raw=version_str)


def get_target_runtime_version(program_path: str) -> VersionInfo | None:
    """Get .NET runtime version from program's runtimeconfig.json.

    Args:
        program_path: Path to the program (.dll or .exe)

    Returns:
        VersionInfo if detected, None otherwise
    """
    # Find runtimeconfig.json
    base_path = os.path.splitext(program_path)[0]
    runtimeconfig_path = f"{base_path}.runtimeconfig.json"

    if not os.path.isfile(runtimeconfig_path):
        logger.debug(f"No runtimeconfig.json found at {runtimeconfig_path}")
        return None

    try:
        with open(runtimeconfig_path, encoding="utf-8") as f:
            config = json.load(f)

        # Extract version from runtimeOptions.framework.version
        # or from runtimeOptions.frameworks[0].version
        runtime_options = config.get("runtimeOptions", {})

        # Single framework (most common)
        framework = runtime_options.get("framework", {})
        version = framework.get("version")

        # Multiple frameworks (less common)
        if not version:
            frameworks = runtime_options.get("frameworks", [])
            if frameworks:
                # Use Microsoft.NETCore.App version
                for fw in frameworks:
                    if fw.get("name") == "Microsoft.NETCore.App":
                        version = fw.get("version")
                        break
                # Fallback to first framework
                if not version:
                    version = frameworks[0].get("version")

        if version:
            result = VersionInfo.from_string(version)
            if result:
                logger.debug(f"Detected target runtime version: {result}")
                return result

    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read runtimeconfig.json: {e}")

    return None


def get_dbgshim_version(netcoredbg_path: str) -> VersionInfo | None:
    """Get version of dbgshim.dll in netcoredbg directory.

    Uses Windows API to read file version info.

    Args:
        netcoredbg_path: Path to netcoredbg executable

    Returns:
        VersionInfo if detected, None otherwise
    """
    netcoredbg_dir = os.path.dirname(netcoredbg_path)
    dbgshim_path = os.path.join(netcoredbg_dir, "dbgshim.dll")

    if not os.path.isfile(dbgshim_path):
        logger.debug(f"dbgshim.dll not found at {dbgshim_path}")
        return None

    # Try to get file version using ctypes (Windows-only)
    try:
        import ctypes
        from ctypes import wintypes

        # GetFileVersionInfoSizeW
        version_dll = ctypes.windll.version
        size = version_dll.GetFileVersionInfoSizeW(dbgshim_path, None)
        if size == 0:
            logger.debug("GetFileVersionInfoSizeW returned 0")
            return None

        # GetFileVersionInfoW
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(dbgshim_path, 0, size, buffer):
            logger.debug("GetFileVersionInfoW failed")
            return None

        # VerQueryValueW for VS_FIXEDFILEINFO
        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info_ptr = ctypes.POINTER(VS_FIXEDFILEINFO)()
        info_len = wintypes.UINT()

        if not version_dll.VerQueryValueW(
            buffer, "\\", ctypes.byref(info_ptr), ctypes.byref(info_len)
        ):
            logger.debug("VerQueryValueW failed")
            return None

        info = info_ptr.contents
        major = (info.dwFileVersionMS >> 16) & 0xFFFF
        minor = info.dwFileVersionMS & 0xFFFF
        patch = (info.dwFileVersionLS >> 16) & 0xFFFF
        build = info.dwFileVersionLS & 0xFFFF

        result = VersionInfo(major=major, minor=minor, patch=patch, build=build)
        logger.debug(f"Detected dbgshim.dll version: {result}")
        return result

    except (OSError, AttributeError) as e:
        logger.debug(f"Failed to get dbgshim.dll version via Windows API: {e}")

    # Fallback: try to extract version from path patterns
    # e.g., C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll
    try:
        parts = os.path.normpath(dbgshim_path).split(os.sep)
        for part in reversed(parts):
            version = VersionInfo.from_string(part)
            if version:
                logger.debug(f"Detected dbgshim.dll version from path: {version}")
                return version
    except Exception as e:
        logger.debug(f"Failed to extract version from path: {e}")

    return None


@dataclass
class VersionCompatibility:
    """Result of version compatibility check."""

    compatible: bool
    target_version: VersionInfo | None
    dbgshim_version: VersionInfo | None
    warning: str | None = None


def check_version_compatibility(
    program_path: str, netcoredbg_path: str
) -> VersionCompatibility:
    """Check if dbgshim.dll version is compatible with target runtime.

    dbgshim.dll must have the same major version as the target runtime
    for ICorDebugThread3::CreateStackWalk to work correctly.

    Args:
        program_path: Path to the program being debugged
        netcoredbg_path: Path to netcoredbg executable

    Returns:
        VersionCompatibility with check results
    """
    target_version = get_target_runtime_version(program_path)
    dbgshim_version = get_dbgshim_version(netcoredbg_path)

    # Can't check if we don't have both versions
    if target_version is None or dbgshim_version is None:
        return VersionCompatibility(
            compatible=True,  # Assume compatible if can't check
            target_version=target_version,
            dbgshim_version=dbgshim_version,
            warning=None,
        )

    # Major version must match
    if target_version.major != dbgshim_version.major:
        warning = (
            f"dbgshim.dll version mismatch: dbgshim is v{dbgshim_version.major}.x "
            f"but target is .NET {target_version.major}. "
            f"This may cause E_NOINTERFACE (0x80004002) errors in get_call_stack. "
            f"Copy dbgshim.dll from .NET {target_version.major} SDK to fix: "
            f'"C:\\Program Files\\dotnet\\shared\\Microsoft.NETCore.App\\{target_version.major}.x.x\\"'
        )
        return VersionCompatibility(
            compatible=False,
            target_version=target_version,
            dbgshim_version=dbgshim_version,
            warning=warning,
        )

    return VersionCompatibility(
        compatible=True,
        target_version=target_version,
        dbgshim_version=dbgshim_version,
        warning=None,
    )

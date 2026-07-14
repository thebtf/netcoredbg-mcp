"""Version detection and compatibility checking for .NET debugging."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

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


def inspect_target_runtime_version(program_path: str) -> dict[str, object]:
    """Read target runtime evidence without mutating launch or session state."""
    runtimeconfig_path = Path(program_path).with_suffix(".runtimeconfig.json")
    evidence: dict[str, object] = {
        "version": None,
        "major": None,
        "runtimeconfigPath": str(runtimeconfig_path),
        "source": None,
        "status": "missing",
    }
    try:
        runtimeconfig_path = runtimeconfig_path.resolve()
    except OSError:
        evidence["status"] = "unreadable"
        return evidence
    evidence["runtimeconfigPath"] = str(runtimeconfig_path)

    try:
        with runtimeconfig_path.open(encoding="utf-8") as runtimeconfig_file:
            config = json.load(runtimeconfig_file)
    except FileNotFoundError:
        return evidence
    except json.JSONDecodeError:
        evidence["status"] = "malformed"
        return evidence
    except OSError:
        evidence["status"] = "unreadable"
        return evidence

    if not isinstance(config, dict):
        evidence["status"] = "malformed"
        return evidence

    runtime_options = config.get("runtimeOptions")
    if not isinstance(runtime_options, dict):
        evidence["status"] = "malformed"
        return evidence

    source: str | None = None
    version_value: object = None
    framework_present = "framework" in runtime_options
    framework = runtime_options.get("framework")
    if framework_present and not isinstance(framework, dict):
        evidence["status"] = "malformed"
        return evidence
    if isinstance(framework, dict) and framework.get("version"):
        source = "runtimeconfig_framework"
        version_value = framework["version"]
    else:
        frameworks_present = "frameworks" in runtime_options
        frameworks = runtime_options.get("frameworks")
        if frameworks_present and not isinstance(frameworks, list):
            evidence["status"] = "malformed"
            return evidence
        if isinstance(frameworks, list) and frameworks:
            selected: dict[str, object] | None = None
            fallback: dict[str, object] | None = None
            for item in frameworks:
                if not isinstance(item, dict):
                    evidence["status"] = "malformed"
                    return evidence
                if fallback is None:
                    fallback = item
                if item.get("name") == "Microsoft.NETCore.App":
                    selected = item
                    break
            if selected is None:
                selected = fallback
            if selected is None:
                evidence["status"] = "malformed"
                return evidence
            source = "runtimeconfig_frameworks"
            version_value = selected.get("version")

    if not isinstance(version_value, str):
        evidence["status"] = "malformed"
        return evidence

    version = VersionInfo.from_string(version_value)
    if version is None:
        evidence["status"] = "malformed"
        return evidence

    evidence.update(
        version=str(version),
        major=version.major,
        source=source,
        status="known",
    )
    return evidence


def get_target_runtime_version(program_path: str) -> VersionInfo | None:
    """Get .NET runtime version from program's runtimeconfig.json.

    Args:
        program_path: Path to the program (.dll or .exe)

    Returns:
        VersionInfo if detected, None otherwise
    """
    base_path = os.path.splitext(program_path)[0]
    runtimeconfig_path = f"{base_path}.runtimeconfig.json"

    if not os.path.isfile(runtimeconfig_path):
        logger.debug(f"No runtimeconfig.json found at {runtimeconfig_path}")
        return None

    try:
        with open(runtimeconfig_path, encoding="utf-8") as f:
            config = json.load(f)

        runtime_options = config.get("runtimeOptions", {})
        framework = runtime_options.get("framework", {})
        version = framework.get("version")

        if not version:
            frameworks = runtime_options.get("frameworks", [])
            if frameworks:
                for fw in frameworks:
                    if fw.get("name") == "Microsoft.NETCore.App":
                        version = fw.get("version")
                        break
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


def get_dbgshim_version(
    netcoredbg_path: str,
    *,
    allow_path_fallback: bool = True,
) -> VersionInfo | None:
    """Get version of dbgshim.dll in netcoredbg directory.

    Uses Windows API to read file version info.

    Args:
        netcoredbg_path: Path to netcoredbg executable
        allow_path_fallback: Whether a version-like parent directory may be used.

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
        class VsFixedFileInfo(ctypes.Structure):
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

        info_ptr = ctypes.POINTER(VsFixedFileInfo)()
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

    if not allow_path_fallback:
        return None

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


def inspect_active_dbgshim_version(netcoredbg_path: str) -> dict[str, object]:
    """Read active dbgshim evidence without falling back on non-Windows hosts."""
    system = platform.system()
    filename = "dbgshim.dll" if system == "Windows" else "libdbgshim.so"
    dbgshim_path = Path(netcoredbg_path).parent / filename
    if system != "Windows":
        return {
            "version": None,
            "major": None,
            "path": str(dbgshim_path),
            "source": "unsupported_platform",
            "status": "unsupported_platform",
        }

    try:
        shim_exists = dbgshim_path.is_file()
    except OSError:
        return {
            "version": None,
            "major": None,
            "path": str(dbgshim_path),
            "source": "windows_file_version",
            "status": "unreadable",
        }

    if not shim_exists:
        return {
            "version": None,
            "major": None,
            "path": str(dbgshim_path),
            "source": "windows_file_version",
            "status": "missing",
        }

    version = get_dbgshim_version(netcoredbg_path, allow_path_fallback=False)
    if version is None:
        return {
            "version": None,
            "major": None,
            "path": str(dbgshim_path),
            "source": "windows_file_version",
            "status": "unreadable",
        }

    return {
        "version": str(version),
        "major": version.major,
        "path": str(dbgshim_path),
        "source": "windows_file_version",
        "status": "known",
    }


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
            '"C:\\Program Files\\dotnet\\shared\\Microsoft.NETCore.App\\'
            f'{target_version.major}.x.x\\"'
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

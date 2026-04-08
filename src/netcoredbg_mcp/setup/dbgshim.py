"""dbgshim.dll version management.

Scans installed .NET runtimes for dbgshim.dll, caches versions in
~/.netcoredbg-mcp/dbgshim/<version>/, and dynamically swaps the
correct version into the netcoredbg directory at launch time.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from pathlib import Path

from .home import get_home_dir

logger = logging.getLogger(__name__)

_DBGSHIM_FILENAME = "dbgshim.dll" if os.name == "nt" else "libdbgshim.so"


def _get_runtime_scan_paths() -> list[Path]:
    """Return platform-specific paths where .NET runtimes install dbgshim.

    Each path is the parent containing version subdirectories like:
    <path>/6.0.36/dbgshim.dll
    """
    system = platform.system()
    paths: list[Path] = []

    if system == "Windows":
        # Standard install: C:\Program Files\dotnet\...
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        paths.append(
            Path(program_files) / "dotnet" / "shared" / "Microsoft.NETCore.App"
        )
        # x86 on 64-bit Windows
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        if program_files_x86:
            paths.append(
                Path(program_files_x86) / "dotnet" / "shared" / "Microsoft.NETCore.App"
            )
    elif system == "Linux":
        paths.extend([
            Path("/usr/share/dotnet/shared/Microsoft.NETCore.App"),
            Path("/usr/local/share/dotnet/shared/Microsoft.NETCore.App"),
            Path.home() / ".dotnet" / "shared" / "Microsoft.NETCore.App",
        ])
    elif system == "Darwin":
        paths.extend([
            Path("/usr/local/share/dotnet/shared/Microsoft.NETCore.App"),
            Path.home() / ".dotnet" / "shared" / "Microsoft.NETCore.App",
        ])

    # DOTNET_ROOT override (all platforms)
    dotnet_root = os.environ.get("DOTNET_ROOT")
    if dotnet_root:
        custom = Path(dotnet_root) / "shared" / "Microsoft.NETCore.App"
        if custom not in paths:
            paths.insert(0, custom)

    return paths


def scan_installed_runtimes() -> dict[str, Path]:
    """Scan installed .NET runtime directories for dbgshim files.

    Returns:
        Dict mapping version string (e.g. "6.0.36") to the full path
        of the dbgshim file in that runtime directory.
    """
    results: dict[str, Path] = {}

    for scan_root in _get_runtime_scan_paths():
        if not scan_root.is_dir():
            continue
        try:
            for entry in sorted(scan_root.iterdir()):
                if not entry.is_dir():
                    continue
                dbgshim = entry / _DBGSHIM_FILENAME
                if dbgshim.is_file():
                    version = entry.name
                    if version not in results:
                        results[version] = dbgshim
                        logger.debug("Found dbgshim %s at %s", version, dbgshim)
        except OSError as e:
            logger.debug("Cannot scan %s: %s", scan_root, e)

    logger.info("Scanned %d dbgshim versions: %s", len(results), list(results.keys()))
    return results


def extract_dbgshim_versions(target_dir: Path | None = None) -> list[str]:
    """Copy all discovered dbgshim files to the cache directory.

    Each version is stored as:
      <target_dir>/<version>/dbgshim.dll

    Skips versions where the cached file has the same size as the source
    (avoids redundant copies on repeated runs).

    Args:
        target_dir: Cache directory. Defaults to ~/.netcoredbg-mcp/dbgshim/

    Returns:
        List of extracted version strings.
    """
    if target_dir is None:
        target_dir = get_home_dir() / "dbgshim"

    runtimes = scan_installed_runtimes()
    extracted: list[str] = []

    for version, source_path in runtimes.items():
        version_dir = target_dir / version
        dest = version_dir / _DBGSHIM_FILENAME

        # Skip if already cached with same size
        if dest.is_file():
            try:
                if dest.stat().st_size == source_path.stat().st_size:
                    extracted.append(version)
                    continue
            except OSError:
                pass

        # Atomic copy: write to .tmp then rename
        version_dir.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".tmp")
        try:
            shutil.copy2(str(source_path), str(tmp_dest))
            os.replace(str(tmp_dest), str(dest))
            extracted.append(version)
            logger.info("Cached dbgshim %s → %s", version, dest)
        except OSError as e:
            logger.warning("Failed to cache dbgshim %s: %s", version, e)
            # Clean up partial copy
            try:
                tmp_dest.unlink(missing_ok=True)
            except OSError:
                pass

    return extracted


def select_dbgshim(
    target_version: str,
    cache_dir: Path | None = None,
) -> Path | None:
    """Find the best matching cached dbgshim for a target .NET version.

    Matching strategy: same major version, highest patch number.
    E.g., target "6.0" matches "6.0.36" over "6.0.20".

    Args:
        target_version: Target .NET version (e.g. "6.0.36", "6.0", "8")
        cache_dir: dbgshim cache directory. Defaults to ~/.netcoredbg-mcp/dbgshim/

    Returns:
        Path to best matching dbgshim file, or None if no match.
    """
    if cache_dir is None:
        cache_dir = get_home_dir() / "dbgshim"

    if not cache_dir.is_dir():
        return None

    # Parse target major version
    parts = target_version.split(".")
    try:
        target_major = int(parts[0])
    except (ValueError, IndexError):
        logger.warning("Cannot parse target version: %s", target_version)
        return None

    # Find all cached versions with same major
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        dbgshim = entry / _DBGSHIM_FILENAME
        if not dbgshim.is_file():
            continue

        version_parts = entry.name.split(".")
        try:
            major = int(version_parts[0])
        except (ValueError, IndexError):
            continue

        if major != target_major:
            continue

        # Parse full version for sorting (higher = better)
        version_tuple = tuple(
            int(p) for p in version_parts if p.isdigit()
        )
        candidates.append((version_tuple, dbgshim))

    if not candidates:
        logger.debug("No cached dbgshim for major version %d", target_major)
        return None

    # Return highest patch version
    candidates.sort(key=lambda c: c[0], reverse=True)
    best = candidates[0][1]
    logger.info(
        "Selected dbgshim %s for target %s",
        best.parent.name, target_version,
    )
    return best


def swap_dbgshim(netcoredbg_dir: Path, dbgshim_path: Path) -> None:
    """Copy a dbgshim file to the netcoredbg directory.

    Uses atomic copy (write to .tmp, then os.replace) to prevent
    partial writes if interrupted.

    Args:
        netcoredbg_dir: Directory containing netcoredbg executable.
        dbgshim_path: Source dbgshim file to install.

    Raises:
        OSError: If copy fails.
    """
    dest = netcoredbg_dir / _DBGSHIM_FILENAME
    tmp_dest = dest.with_suffix(".tmp")

    shutil.copy2(str(dbgshim_path), str(tmp_dest))
    os.replace(str(tmp_dest), str(dest))
    logger.info("Swapped dbgshim: %s → %s", dbgshim_path.parent.name, dest)


def select_and_swap_dbgshim(
    program: str,
    netcoredbg_path: str,
) -> bool:
    """High-level: select and swap the correct dbgshim for a target program.

    Reads the target's runtimeconfig.json to determine the .NET version,
    finds the best matching cached dbgshim, and copies it to the netcoredbg
    directory.

    Args:
        program: Path to the .NET program being debugged.
        netcoredbg_path: Path to netcoredbg executable.

    Returns:
        True if dbgshim was swapped, False if no match or error.
    """
    from ..utils.version import get_target_runtime_version

    target_version = get_target_runtime_version(program)
    if target_version is None:
        logger.debug("Cannot determine target runtime version for %s", program)
        return False

    version_str = str(target_version)
    best = select_dbgshim(version_str)
    if best is None:
        logger.debug("No cached dbgshim matches target %s", version_str)
        return False

    netcoredbg_dir = Path(netcoredbg_path).parent
    try:
        swap_dbgshim(netcoredbg_dir, best)
        return True
    except OSError as e:
        logger.warning("Failed to swap dbgshim: %s", e)
        return False

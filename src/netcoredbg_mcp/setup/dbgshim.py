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
from dataclasses import dataclass
from pathlib import Path

from ..utils.version import (
    VersionInfo,
    inspect_active_dbgshim_version,
    inspect_target_runtime_version,
)
from .home import get_home_dir

logger = logging.getLogger(__name__)

_DBGSHIM_FILENAME = "dbgshim.dll" if os.name == "nt" else "libdbgshim.so"


@dataclass(frozen=True)
class DbgshimSelectionDecision:
    """Read-only result of applying the launch path's cache selection rule."""

    path: Path | None
    status: str
    version: str | None = None
    major: int | None = None
    _lookup_error: OSError | None = None

    def as_candidate(self) -> dict[str, object]:
        """Serialize a fixed-shape cached-candidate diagnostic."""
        selected = self.path is not None
        return {
            "version": self.version,
            "major": self.major,
            "path": str(self.path) if selected else None,
            "provenance": "cache_directory_name" if selected else None,
            "selection": "same_major_highest_numeric_tuple",
            "status": self.status,
        }


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
        paths.extend(
            [
                Path("/usr/share/dotnet/shared/Microsoft.NETCore.App"),
                Path("/usr/local/share/dotnet/shared/Microsoft.NETCore.App"),
                Path.home() / ".dotnet" / "shared" / "Microsoft.NETCore.App",
            ]
        )
    elif system == "Darwin":
        paths.extend(
            [
                Path("/usr/local/share/dotnet/shared/Microsoft.NETCore.App"),
                Path.home() / ".dotnet" / "shared" / "Microsoft.NETCore.App",
            ]
        )

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


def select_dbgshim_decision(
    target_version: str,
    cache_dir: Path,
) -> DbgshimSelectionDecision:
    """Apply the existing same-major/highest-numeric-tuple selector read-only."""
    try:
        target_major = int(target_version.split(".")[0])
    except (ValueError, IndexError):
        logger.warning("Cannot parse target version: %s", target_version)
        return DbgshimSelectionDecision(path=None, status="malformed")

    candidates: list[tuple[tuple[int, ...], Path]] = []
    try:
        if not cache_dir.exists():
            return DbgshimSelectionDecision(path=None, status="missing")
        if not cache_dir.is_dir():
            return DbgshimSelectionDecision(path=None, status="no_match")

        for entry in cache_dir.iterdir():
            if not entry.is_dir():
                continue
            candidate_path = entry / _DBGSHIM_FILENAME
            if not candidate_path.is_file():
                continue

            version_parts = entry.name.split(".")
            try:
                major = int(version_parts[0])
            except (ValueError, IndexError):
                continue
            if major != target_major:
                continue

            version_tuple = tuple(int(part) for part in version_parts if part.isdigit())
            candidates.append((version_tuple, candidate_path))
    except OSError as exc:
        logger.debug("Cannot inspect dbgshim cache %s: %s", cache_dir, exc)
        return DbgshimSelectionDecision(
            path=None,
            status="unreadable",
            _lookup_error=exc,
        )

    if not candidates:
        logger.debug("No cached dbgshim for major version %d", target_major)
        return DbgshimSelectionDecision(path=None, status="no_match")

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    best = candidates[0][1]
    parsed_version = VersionInfo.from_string(best.parent.name)
    logger.info("Selected dbgshim %s for target %s", best.parent.name, target_version)
    if parsed_version is None:
        return DbgshimSelectionDecision(path=best, status="malformed")
    return DbgshimSelectionDecision(
        path=best,
        status="known",
        version=str(parsed_version),
        major=parsed_version.major,
    )


def select_dbgshim(
    target_version: str,
    cache_dir: Path | None = None,
) -> Path | None:
    """Find the launch path's best matching cached dbgshim, if any."""
    if cache_dir is None:
        cache_dir = get_home_dir() / "dbgshim"
    decision = select_dbgshim_decision(target_version, cache_dir)
    if decision._lookup_error is not None:
        raise decision._lookup_error
    return decision.path


def build_debug_launch_compatibility(
    *,
    program: str,
    target_runtime: dict[str, object],
    active_dbgshim: dict[str, object],
    cached_candidate: dict[str, object],
) -> dict[str, object]:
    """Build the bounded public verdict from already-collected read-only evidence."""
    target_known = target_runtime.get("status") == "known"
    active_known = active_dbgshim.get("status") == "known"
    candidate_status = cached_candidate.get("status")
    selected = cached_candidate.get("path") is not None
    verdict: str
    compatible: bool | None
    will_mutate: bool
    warning: str | None

    if not target_known:
        verdict = "unknown"
        compatible = None
        will_mutate = False
        warning = (
            "Target runtime version is unavailable; compatibility cannot be determined."
        )
    elif selected and candidate_status != "known":
        verdict = "unknown"
        compatible = None
        will_mutate = True
        warning = (
            "The selected cached dbgshim version is malformed; "
            "compatibility cannot be determined."
        )
    elif not active_known:
        verdict = "unknown"
        compatible = None
        will_mutate = selected
        warning = (
            "Active dbgshim version is unavailable; "
            "start_debug would select the cached candidate."
            if selected
            else (
                "Active dbgshim version is unavailable; "
                "compatibility cannot be determined."
            )
        )
    elif target_runtime.get("major") == active_dbgshim.get("major"):
        verdict = "compatible"
        compatible = True
        will_mutate = selected
        warning = (
            "A compatible cached dbgshim is available; "
            "start_debug would replace the shared debugger copy."
            if selected
            else None
        )
    elif selected:
        verdict = "compatible_after_swap"
        compatible = True
        will_mutate = True
        warning = (
            "A compatible cached dbgshim is available; "
            "start_debug would replace the shared debugger copy."
        )
    elif candidate_status == "unreadable":
        verdict = "unknown"
        compatible = None
        will_mutate = False
        warning = (
            "The dbgshim cache could not be read; compatibility cannot be determined."
        )
    else:
        verdict = "blocked_no_matching_shim"
        compatible = False
        will_mutate = False
        warning = "No cached dbgshim matches the target runtime; start_debug remains fail-open."

    return {
        "verdict": verdict,
        "program": program,
        "targetRuntime": target_runtime,
        "activeDbgshim": active_dbgshim,
        "cachedCandidate": cached_candidate,
        "compatible": compatible,
        "willMutateSharedDebugger": will_mutate,
        "mutationPerformed": False,
        "warning": warning,
    }


def inspect_debug_launch_compatibility(
    program: str,
    netcoredbg_path: str,
    cache_dir: Path,
) -> dict[str, object]:
    """Inspect launch compatibility without creating cache state or swapping files."""
    target_runtime = inspect_target_runtime_version(program)
    active_dbgshim = inspect_active_dbgshim_version(netcoredbg_path)
    target_version = target_runtime.get("version")
    if target_runtime.get("status") == "known" and isinstance(target_version, str):
        cached_candidate = select_dbgshim_decision(
            target_version,
            cache_dir,
        ).as_candidate()
    else:
        cached_candidate = {
            "version": None,
            "major": None,
            "path": None,
            "provenance": None,
            "selection": "not_attempted",
            "status": "not_attempted",
        }

    return build_debug_launch_compatibility(
        program=program,
        target_runtime=target_runtime,
        active_dbgshim=active_dbgshim,
        cached_candidate=cached_candidate,
    )


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

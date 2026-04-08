"""FlaUI bridge auto-build management.

Builds the FlaUI bridge from C# source to ~/.netcoredbg-mcp/bridge/.
Source can come from package_data (pip install) or the repo (dev mode).
Rebuilds only when source files are newer than the built EXE.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .home import get_home_dir

logger = logging.getLogger(__name__)

_BRIDGE_FILENAME = "FlaUIBridge.exe"
_CSPROJ_NAME = "FlaUIBridge.csproj"


def find_bridge_source() -> Path | None:
    """Find the bridge C# source directory.

    Search order:
    1. Package data — sibling to this package in site-packages
       (pip install puts bridge/ at <site-packages>/netcoredbg_mcp/bridge/)
    2. Repository — bridge/ next to src/ in the repo root

    Returns:
        Path to directory containing FlaUIBridge.csproj, or None.
    """
    # 1. Package data: <site-packages>/netcoredbg_mcp/bridge/
    pkg_dir = Path(__file__).resolve().parent.parent  # setup/ → netcoredbg_mcp/
    pkg_bridge = pkg_dir / "bridge" / _CSPROJ_NAME
    if pkg_bridge.is_file():
        logger.debug("Bridge source from package_data: %s", pkg_bridge.parent)
        return pkg_bridge.parent

    # 2. Repository: walk up to find bridge/ dir
    for parent in Path(__file__).resolve().parents:
        csproj = parent / "bridge" / _CSPROJ_NAME
        if csproj.is_file():
            logger.debug("Bridge source from repo: %s", csproj.parent)
            return csproj.parent

    return None


def _needs_rebuild(csproj: Path, exe: Path) -> bool:
    """Check if bridge EXE is stale relative to source files."""
    if not exe.is_file():
        return True
    exe_mtime = exe.stat().st_mtime
    source_dir = csproj.parent
    for f in source_dir.rglob("*.cs"):
        if f.stat().st_mtime > exe_mtime:
            return True
    if csproj.stat().st_mtime > exe_mtime:
        return True
    manifest = source_dir / "app.manifest"
    if manifest.is_file() and manifest.stat().st_mtime > exe_mtime:
        return True
    return False


def build_bridge(
    source_dir: Path,
    output_dir: Path | None = None,
) -> Path | None:
    """Build FlaUI bridge from source if needed.

    Only rebuilds when source files (.cs, .csproj, app.manifest) are
    newer than the built EXE.

    Args:
        source_dir: Directory containing FlaUIBridge.csproj.
        output_dir: Build output directory. Defaults to ~/.netcoredbg-mcp/bridge/

    Returns:
        Path to built FlaUIBridge.exe, or None if build fails.
    """
    if output_dir is None:
        output_dir = get_home_dir() / "bridge"

    csproj = source_dir / _CSPROJ_NAME
    if not csproj.is_file():
        logger.warning("Bridge csproj not found: %s", csproj)
        return None

    exe_path = output_dir / _BRIDGE_FILENAME

    if not _needs_rebuild(csproj, exe_path):
        logger.debug("Bridge up to date: %s", exe_path)
        return exe_path

    logger.info("Building FlaUI bridge from %s", source_dir)
    try:
        result = subprocess.run(
            [
                "dotnet", "publish", str(csproj),
                "-c", "Release",
                "-r", "win-x64",
                "--self-contained",
                "-o", str(output_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and exe_path.is_file():
            logger.info("Bridge built: %s", exe_path)
            return exe_path
        else:
            logger.warning(
                "Bridge build failed (exit %d): %s",
                result.returncode,
                result.stderr[:500] if result.stderr else "no output",
            )
            return None
    except subprocess.TimeoutExpired:
        logger.warning("Bridge build timed out (120s)")
        return None
    except FileNotFoundError:
        logger.info("dotnet CLI not found — cannot build bridge")
        return None


def find_or_build_bridge() -> str | None:
    """Find or build FlaUIBridge.exe.

    Search order:
    1. FLAUI_BRIDGE_PATH environment variable (explicit override)
    2. ~/.netcoredbg-mcp/bridge/ (managed build — rebuild if stale)
    3. Auto-build from source if source available
    4. System PATH

    Returns:
        Absolute path to FlaUIBridge.exe, or None if not found.
    """
    # 1. Environment variable override
    env_path = os.environ.get("FLAUI_BRIDGE_PATH")
    if env_path and Path(env_path).is_file():
        logger.info("Bridge via FLAUI_BRIDGE_PATH: %s", env_path)
        return str(Path(env_path).resolve())

    # 2. Managed build in home dir (check if up to date)
    home_bridge = get_home_dir() / "bridge" / _BRIDGE_FILENAME
    source_dir = find_bridge_source()

    if source_dir:
        # Try to build (or use cached) to home dir
        built = build_bridge(source_dir)
        if built:
            return str(Path(built).resolve())
    elif home_bridge.is_file():
        # No source but previously built — use cached
        logger.info("Using cached bridge: %s", home_bridge)
        return str(home_bridge.resolve())

    # 3. System PATH
    path_result = shutil.which(_BRIDGE_FILENAME)
    if path_result:
        logger.info("Bridge on PATH: %s", path_result)
        return str(Path(path_result).resolve())

    return None

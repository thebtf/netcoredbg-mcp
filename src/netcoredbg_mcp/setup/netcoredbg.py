"""netcoredbg binary download and detection.

Downloads netcoredbg from Samsung's GitHub releases to
~/.netcoredbg-mcp/netcoredbg/. Detects OS/arch to select the
correct release asset.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .home import get_home_dir, get_config, save_config

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com/repos/samsung/netcoredbg/releases/latest"
_EXE_NAME = "netcoredbg.exe" if os.name == "nt" else "netcoredbg"


def _detect_platform() -> str | None:
    """Detect platform string for GitHub release asset name.

    Returns asset name keyword like 'win64', 'linux-amd64', 'osx-amd64',
    or None if unsupported.
    """
    system = platform.system()
    machine = platform.machine().lower()

    is_arm = machine in ("aarch64", "arm64")

    if system == "Windows":
        return "win64"
    elif system == "Linux":
        return "linux-arm64" if is_arm else "linux-amd64"
    elif system == "Darwin":
        return "osx-amd64"  # Samsung only provides amd64 for macOS
    return None


def get_latest_release_info() -> tuple[str, str, int] | None:
    """Query GitHub API for the latest netcoredbg release.

    Returns:
        Tuple of (download_url, version_tag, file_size) or None on failure.
    """
    platform_key = _detect_platform()
    if platform_key is None:
        logger.warning("Unsupported platform for netcoredbg download")
        return None

    try:
        req = Request(_GITHUB_API, headers={"User-Agent": "netcoredbg-mcp"})
        with urlopen(req, timeout=15) as resp:
            data: dict[str, Any] = json.loads(resp.read())
    except (URLError, json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to query GitHub API: %s", e)
        return None

    version = data.get("tag_name", "unknown")
    assets = data.get("assets", [])

    for asset in assets:
        name = asset.get("name", "")
        if platform_key in name:
            url = asset.get("browser_download_url", "")
            size = asset.get("size", 0)
            logger.info(
                "Found release: %s v%s (%d bytes)",
                name, version, size,
            )
            return url, version, size

    logger.warning("No matching asset for platform '%s' in release %s", platform_key, version)
    return None


def download_netcoredbg(
    target_dir: Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path | None:
    """Download and extract latest netcoredbg to target directory.

    Args:
        target_dir: Extraction target. Defaults to ~/.netcoredbg-mcp/netcoredbg/
        progress_callback: Optional callback(bytes_downloaded, total_bytes).

    Returns:
        Path to netcoredbg executable, or None on failure.
    """
    if target_dir is None:
        target_dir = get_home_dir() / "netcoredbg"

    release = get_latest_release_info()
    if release is None:
        return None

    url, version, expected_size = release

    logger.info("Downloading netcoredbg %s from %s", version, url)

    try:
        req = Request(url, headers={"User-Agent": "netcoredbg-mcp"})
        with urlopen(req, timeout=120) as resp:
            # Download to temp file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".download") as tmp:
                tmp_path = tmp.name
                downloaded = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, expected_size)

        # Verify size
        actual_size = os.path.getsize(tmp_path)
        if expected_size > 0 and abs(actual_size - expected_size) > 1024:
            logger.warning(
                "Download size mismatch: expected %d, got %d",
                expected_size, actual_size,
            )
            os.unlink(tmp_path)
            return None

        # Extract archive
        target_dir.mkdir(parents=True, exist_ok=True)

        if url.endswith(".zip"):
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(str(target_dir))
        elif url.endswith(".tar.gz") or url.endswith(".tgz"):
            with tarfile.open(tmp_path, "r:gz") as tf:
                tf.extractall(str(target_dir))
        else:
            logger.warning("Unknown archive format: %s", url)
            os.unlink(tmp_path)
            return None

        os.unlink(tmp_path)

        # Find the executable (may be in a subdirectory like netcoredbg/)
        exe_path = _find_exe_in_dir(target_dir)
        if exe_path is None:
            logger.warning("netcoredbg executable not found after extraction in %s", target_dir)
            return None

        # Update config
        config = get_config()
        config["netcoredbg"] = {
            "version": version,
            "source": "github",
            "path": str(exe_path),
        }
        save_config(config)

        logger.info("Downloaded netcoredbg %s to %s", version, exe_path)
        return exe_path

    except (URLError, OSError) as e:
        logger.warning("Download failed: %s", e)
        # Clean up partial download
        if "tmp_path" in locals():
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None


def _find_exe_in_dir(directory: Path) -> Path | None:
    """Find netcoredbg executable in a directory (possibly nested)."""
    # Direct
    direct = directory / _EXE_NAME
    if direct.is_file():
        return direct

    # One level deep (e.g., netcoredbg/netcoredbg.exe inside archive)
    for child in directory.iterdir():
        if child.is_dir():
            nested = child / _EXE_NAME
            if nested.is_file():
                return nested

    return None


def find_netcoredbg() -> str:
    """Find netcoredbg binary with auto-download fallback.

    Search order:
    1. NETCOREDBG_PATH environment variable (explicit override)
    2. ~/.netcoredbg-mcp/netcoredbg/ (managed installation)
    3. System PATH (shutil.which)
    4. Auto-download from Samsung GitHub

    Returns:
        Absolute path to netcoredbg executable.

    Raises:
        FileNotFoundError: If not found and download fails.
    """
    # 1. Environment variable
    env_path = os.environ.get("NETCOREDBG_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Managed installation
    home_exe = get_home_dir() / "netcoredbg" / _EXE_NAME
    if home_exe.is_file():
        return str(home_exe)

    # Also check nested (archive may extract to netcoredbg/netcoredbg.exe)
    home_dir = get_home_dir() / "netcoredbg"
    if home_dir.is_dir():
        found = _find_exe_in_dir(home_dir)
        if found:
            return str(found)

    # 3. System PATH
    system_path = shutil.which("netcoredbg")
    if system_path:
        return system_path

    # 4. Auto-download
    logger.info("netcoredbg not found — attempting auto-download")
    downloaded = download_netcoredbg()
    if downloaded:
        return str(downloaded)

    raise FileNotFoundError(
        "netcoredbg not found. Run 'netcoredbg-mcp --setup' to download, "
        "or set NETCOREDBG_PATH environment variable."
    )

"""Home directory and configuration management.

Provides ~/.netcoredbg-mcp/ as centralized storage for managed binaries
and configuration. All file operations are atomic (write to .tmp, rename).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_HOME_DIR_NAME = ".netcoredbg-mcp"
_CONFIG_FILENAME = "config.json"


def get_home_dir() -> Path:
    """Return ~/.netcoredbg-mcp/, creating if needed.

    Uses Path.home() / ".netcoredbg-mcp" for cross-platform compatibility.
    Creates the directory (and parents) on first access.

    Returns:
        Absolute path to the home directory.
    """
    home = Path.home() / _HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_config() -> dict:
    """Read config.json from home directory.

    Returns:
        Configuration dict. Empty dict if file is missing,
        unreadable, or contains invalid JSON.
    """
    config_path = get_home_dir() / _CONFIG_FILENAME
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("config.json is not a dict, ignoring: %s", type(data).__name__)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read config.json: %s", e)
        return {}


def save_config(config: dict) -> None:
    """Atomically write config.json to home directory.

    Writes to a temporary file first, then uses os.replace() for an
    atomic rename. This prevents partial writes on crash or power loss.

    Args:
        config: Configuration dict to persist.

    Raises:
        TypeError: If config is not a dict.
        OSError: If write fails (permissions, disk full, etc.).
    """
    if not isinstance(config, dict):
        raise TypeError(f"config must be a dict, got {type(config).__name__}")

    config_path = get_home_dir() / _CONFIG_FILENAME
    tmp_path = config_path.with_suffix(".json.tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(str(tmp_path), str(config_path))
    logger.debug("Saved config.json to %s", config_path)

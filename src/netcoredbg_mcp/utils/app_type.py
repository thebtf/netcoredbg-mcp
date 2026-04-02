"""Application type detection from .NET runtime configuration.

Parses runtimeconfig.json and deps.json to determine whether a .NET application
is a GUI app (WPF, WinForms, Avalonia) or a console app.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GUI_FRAMEWORK_MARKERS: tuple[str, ...] = (
    "Microsoft.WindowsDesktop.App",
    "Avalonia",
)
"""Framework name substrings that indicate a GUI application."""


def _check_framework_entry(name: str) -> bool:
    """Check if a single framework name matches any GUI marker.

    Args:
        name: The framework name from runtimeconfig.json.

    Returns:
        True if the name contains a known GUI framework marker.
    """
    return any(marker in name for marker in _GUI_FRAMEWORK_MARKERS)


def _check_runtimeconfig(runtimeconfig_path: Path) -> str | None:
    """Check runtimeconfig.json for GUI framework references.

    Args:
        runtimeconfig_path: Path to the runtimeconfig.json file.

    Returns:
        "gui" if a GUI framework is detected,
        "console" if the file exists and parses but no GUI framework found,
        None if the file is missing or malformed.
    """
    try:
        data = json.loads(runtimeconfig_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("runtimeconfig.json not found: %s", runtimeconfig_path)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read runtimeconfig.json %s: %s", runtimeconfig_path, exc)
        return None

    runtime_options = data.get("runtimeOptions")
    if not isinstance(runtime_options, dict):
        logger.debug("runtimeconfig.json has no valid runtimeOptions: %s", runtimeconfig_path)
        return None

    # Check single "framework" entry
    framework = runtime_options.get("framework")
    if isinstance(framework, dict):
        name = framework.get("name", "")
        if isinstance(name, str) and _check_framework_entry(name):
            logger.debug("GUI framework detected in 'framework': %s", name)
            return "gui"

    # Check "frameworks" array
    frameworks = runtime_options.get("frameworks")
    if isinstance(frameworks, list):
        for entry in frameworks:
            if isinstance(entry, dict):
                name = entry.get("name", "")
                if isinstance(name, str) and _check_framework_entry(name):
                    logger.debug("GUI framework detected in 'frameworks': %s", name)
                    return "gui"

    logger.debug("No GUI framework found in runtimeconfig.json: %s", runtimeconfig_path)
    return "console"


def _check_deps_json(deps_path: Path) -> bool:
    """Check deps.json for Avalonia Desktop dependency as a fallback.

    Args:
        deps_path: Path to the deps.json file.

    Returns:
        True if Avalonia.Desktop is found in any target's dependencies.
    """
    try:
        data = json.loads(deps_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read deps.json %s: %s", deps_path, exc)
        return False

    targets = data.get("targets")
    if not isinstance(targets, dict):
        return False

    for _target_name, dependencies in targets.items():
        if not isinstance(dependencies, dict):
            continue
        for dep_key in dependencies:
            if isinstance(dep_key, str) and dep_key.startswith("Avalonia.Desktop/"):
                logger.debug("Avalonia.Desktop found in deps.json target: %s", dep_key)
                return True

    return False


def detect_app_type(program_path: str) -> str | None:
    """Detect if program is a GUI or console app.

    Parses the program's runtimeconfig.json to check for known GUI frameworks.
    Falls back to deps.json to detect Avalonia apps.

    Args:
        program_path: Path to .dll or .exe being debugged.

    Returns:
        "gui" if WindowsDesktop or Avalonia framework detected,
        "console" if runtimeconfig exists but no GUI framework found,
        None if runtimeconfig.json not found or unreadable.
    """
    program = Path(program_path)
    stem = program.stem
    parent = program.parent

    # Build path to runtimeconfig.json: same directory, same stem
    runtimeconfig_path = parent / f"{stem}.runtimeconfig.json"
    result = _check_runtimeconfig(runtimeconfig_path)

    if result == "gui":
        logger.debug("App type for %s: gui (from runtimeconfig.json)", program_path)
        return "gui"

    # Fallback: check deps.json for Avalonia
    deps_path = parent / f"{stem}.deps.json"
    if _check_deps_json(deps_path):
        logger.debug("App type for %s: gui (from deps.json Avalonia.Desktop)", program_path)
        return "gui"

    if result == "console":
        logger.debug("App type for %s: console", program_path)
        return "console"

    # Neither runtimeconfig nor deps had useful info
    logger.debug("App type for %s: unknown (no runtimeconfig found)", program_path)
    return None

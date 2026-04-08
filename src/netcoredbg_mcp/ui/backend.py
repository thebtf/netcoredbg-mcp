"""UI automation backend abstraction layer.

Provides a protocol for UI automation backends (FlaUI, pywinauto)
and a factory function that selects the best available backend.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Search paths for FlaUIBridge.exe (in priority order)
_BRIDGE_FILENAME = "FlaUIBridge.exe"


@runtime_checkable
class UIBackend(Protocol):
    """Protocol for UI automation backends."""

    async def connect(self, pid: int) -> None:
        """Connect to a process by PID."""
        ...

    async def disconnect(self) -> None:
        """Disconnect from the current process."""
        ...

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Find a UI element by criteria.

        Args:
            automation_id: AutomationId property.
            name: Element Name property.
            control_type: Control type (e.g. "Button").
            root_id: Optional AutomationId to scope search to a subtree.
            xpath: Optional XPath expression (FlaUI backend only).
        """
        ...

    async def invoke_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Invoke element via InvokePattern (no mouse), fallback to Click."""
        ...

    async def toggle_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Toggle CheckBox/ToggleButton via TogglePattern. Returns new state."""
        ...

    async def find_by_xpath(
        self,
        xpath: str,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        """Find element by XPath expression. Returns element info + matchCount."""
        ...

    async def find_all_cascade(
        self,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Find all matching elements with ranked scoring for disambiguation."""
        ...

    async def extract_text(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Extract text using multi-strategy fallback. Returns text + source."""
        ...

    async def click_at(self, x: int, y: int) -> None:
        """Click at screen coordinates."""
        ...

    async def right_click_at(self, x: int, y: int) -> None:
        """Right-click at screen coordinates."""
        ...

    async def double_click_at(self, x: int, y: int) -> None:
        """Double-click at screen coordinates."""
        ...

    async def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Drag from one position to another."""
        ...

    async def send_keys(self, keys: str) -> None:
        """Send keyboard input."""
        ...

    async def multi_select(self, container_id: str, indices: list[int]) -> int:
        """Select multiple items in a container. Returns count selected."""
        ...

    async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> Any:
        """Get the UI element tree."""
        ...

    @property
    def element_cache(self) -> dict[str, dict]:
        """Cached element rectangles from last tree walk."""
        ...

    @property
    def process_id(self) -> int | None:
        """Currently connected process ID."""
        ...


def _get_bridge_source_dir() -> Path | None:
    """Find the bridge source directory (contains FlaUIBridge.csproj)."""
    # Walk up from this file to find the repo root with bridge/ dir
    current = Path(__file__).resolve()
    for parent in current.parents:
        csproj = parent / "bridge" / "FlaUIBridge.csproj"
        if csproj.is_file():
            return parent / "bridge"
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


def _auto_build_bridge() -> str | None:
    """Auto-build FlaUI bridge from source if needed.

    Builds to bridge/bin/publish/ inside the repo. Only rebuilds when
    source files (.cs, .csproj, app.manifest) are newer than the EXE.

    Returns:
        Path to built FlaUIBridge.exe, or None if build fails/unavailable.
    """
    source_dir = _get_bridge_source_dir()
    if source_dir is None:
        return None

    csproj = source_dir / "FlaUIBridge.csproj"
    output_dir = source_dir / "bin" / "publish"
    exe_path = output_dir / _BRIDGE_FILENAME

    if not _needs_rebuild(csproj, exe_path):
        logger.debug("FlaUI bridge up to date: %s", exe_path)
        return str(exe_path)

    # Build — requires .NET SDK (guaranteed for netcoredbg users)
    logger.info("Building FlaUI bridge from source: %s", csproj)
    import subprocess
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
            logger.info("FlaUI bridge built: %s", exe_path)
            return str(exe_path)
        else:
            logger.warning(
                "FlaUI bridge build failed (exit %d): %s",
                result.returncode,
                result.stderr[:500] if result.stderr else "no output",
            )
            return None
    except subprocess.TimeoutExpired:
        logger.warning("FlaUI bridge build timed out (120s)")
        return None
    except FileNotFoundError:
        logger.info("dotnet CLI not found — cannot auto-build FlaUI bridge")
        return None


def find_flaui_bridge() -> str | None:
    """Find or build FlaUIBridge.exe.

    Search order:
    1. FLAUI_BRIDGE_PATH environment variable (explicit override)
    2. Auto-build from source (bridge/bin/publish/ — rebuilds if stale)
    3. Same directory as netcoredbg (NETCOREDBG_PATH)
    4. System PATH

    Returns:
        Absolute path to FlaUIBridge.exe, or None if not found.
    """
    # 1. Environment variable (explicit override — skip auto-build)
    env_path = os.environ.get("FLAUI_BRIDGE_PATH")
    if env_path and Path(env_path).is_file():
        logger.info("FlaUI bridge found via FLAUI_BRIDGE_PATH: %s", env_path)
        return str(Path(env_path).resolve())

    # 2. Auto-build from source (primary path)
    built = _auto_build_bridge()
    if built:
        return str(Path(built).resolve())

    # 3. Same directory as netcoredbg
    netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
    if netcoredbg_path:
        candidate = Path(netcoredbg_path).parent / _BRIDGE_FILENAME
        if candidate.is_file():
            logger.info("FlaUI bridge found next to netcoredbg: %s", candidate)
            return str(candidate.resolve())

    # 4. System PATH
    path_result = shutil.which(_BRIDGE_FILENAME)
    if path_result:
        logger.info("FlaUI bridge found on PATH: %s", path_result)
        return str(Path(path_result).resolve())

    return None


def create_backend(process_registry: Any = None) -> UIBackend:
    """Create the best available UI automation backend.

    Tries FlaUI first (if bridge binary found), falls back to pywinauto.

    Args:
        process_registry: Optional ProcessRegistry for FlaUI PID tracking.

    Returns:
        UIBackend implementation.
    """
    bridge_path = find_flaui_bridge()

    if bridge_path:
        logger.info("Using FlaUI backend (bridge: %s)", bridge_path)
        from .flaui_client import FlaUIBackend
        return FlaUIBackend(bridge_path, process_registry)

    logger.info("FlaUIBridge.exe not found, using pywinauto backend")
    from .pywinauto_backend import PywinautoBackend
    return PywinautoBackend()

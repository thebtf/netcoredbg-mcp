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
    ) -> dict[str, Any]:
        """Find a UI element by criteria."""
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


def find_flaui_bridge() -> str | None:
    """Search for FlaUIBridge.exe in standard locations.

    Search order:
    1. FLAUI_BRIDGE_PATH environment variable
    2. Same directory as netcoredbg (NETCOREDBG_PATH)
    3. D:\\Bin\\FlaUIBridge.exe
    4. System PATH

    Returns:
        Absolute path to FlaUIBridge.exe, or None if not found.
    """
    # 1. Environment variable
    env_path = os.environ.get("FLAUI_BRIDGE_PATH")
    if env_path and Path(env_path).is_file():
        logger.info("FlaUI bridge found via FLAUI_BRIDGE_PATH: %s", env_path)
        return str(Path(env_path).resolve())

    # 2. Same directory as netcoredbg
    netcoredbg_path = os.environ.get("NETCOREDBG_PATH")
    if netcoredbg_path:
        candidate = Path(netcoredbg_path).parent / _BRIDGE_FILENAME
        if candidate.is_file():
            logger.info("FlaUI bridge found next to netcoredbg: %s", candidate)
            return str(candidate.resolve())

    # 3. Well-known location
    well_known = Path(r"D:\Bin") / _BRIDGE_FILENAME
    if well_known.is_file():
        logger.info("FlaUI bridge found at well-known path: %s", well_known)
        return str(well_known.resolve())

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

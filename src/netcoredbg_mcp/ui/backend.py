"""UI automation backend abstraction layer.

Provides a protocol for UI automation backends (FlaUI, pywinauto)
and a factory function that selects the best available backend.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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


def find_flaui_bridge() -> str | None:
    """Find or build FlaUIBridge.exe.

    Delegates to setup.bridge module which manages builds in
    ~/.netcoredbg-mcp/bridge/ with mtime-based rebuild detection.

    Search order:
    1. FLAUI_BRIDGE_PATH environment variable (explicit override)
    2. ~/.netcoredbg-mcp/bridge/ (managed build — rebuild if stale)
    3. Auto-build from source if available
    4. System PATH

    Returns:
        Absolute path to FlaUIBridge.exe, or None if not found.
    """
    from ..setup.bridge import find_or_build_bridge
    return find_or_build_bridge()


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

"""PywinautoBackend — wraps existing UIAutomation class as a UIBackend.

This preserves all existing pywinauto behavior exactly, serving as
the fallback when FlaUIBridge.exe is not available.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PywinautoBackend:
    """UIBackend implementation wrapping the existing UIAutomation class."""

    def __init__(self) -> None:
        from .automation import UIAutomation
        self._ui = UIAutomation()

    @property
    def element_cache(self) -> dict[str, dict]:
        """Cached element rectangles from last tree walk."""
        return self._ui._element_cache

    @property
    def process_id(self) -> int | None:
        """Connected process ID."""
        return self._ui.process_id

    @property
    def inner(self) -> Any:
        """Access the underlying UIAutomation instance.

        Used by tools that need pywinauto-specific features
        not covered by the UIBackend protocol.
        """
        return self._ui

    async def connect(self, pid: int) -> None:
        """Connect to process via pywinauto."""
        await self._ui.connect(pid)

    async def disconnect(self) -> None:
        """Disconnect from process."""
        await self._ui.disconnect()

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict[str, Any]:
        """Find element via pywinauto."""
        element = await self._ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
        )
        info = await self._ui.get_element_info(element)
        return info.to_dict()

    async def click_at(self, x: int, y: int) -> None:
        """Click at coordinates."""
        await self._ui._click_at_coords(x, y)

    async def right_click_at(self, x: int, y: int) -> None:
        """Right-click at coordinates."""
        await self._ui._right_click_at_coords(x, y)

    async def double_click_at(self, x: int, y: int) -> None:
        """Double-click at coordinates."""
        await self._ui._double_click_at_coords(x, y)

    async def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Drag between coordinates."""
        await self._ui._drag_at_coords(from_x, from_y, to_x, to_y)

    async def send_keys(self, keys: str) -> None:
        """Send keyboard input to focused element."""
        await self._ui.send_keys_focused(keys)

    async def multi_select(self, container_id: str, indices: list[int]) -> int:
        """Multi-select items. Returns count selected.

        Note: This delegates to pywinauto's SelectionItemPattern approach.
        The actual multi-select with Ctrl+Click is handled at the tools layer.
        """
        # pywinauto backend doesn't have a direct multi_select;
        # the tools/ui.py handles the Ctrl+Click logic directly
        return 0

    async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> Any:
        """Get window tree via pywinauto."""
        tree = await self._ui.get_window_tree(max_depth, max_children)
        return tree.to_dict()

    def get_cached_rect(self, automation_id: str) -> dict | None:
        """Get cached rectangle for an element by AutomationId."""
        return self._ui.get_cached_rect(automation_id)

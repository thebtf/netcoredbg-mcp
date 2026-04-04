"""PywinautoBackend — wraps existing UIAutomation class as a UIBackend.

This preserves all existing pywinauto behavior exactly, serving as
the fallback when FlaUIBridge.exe is not available.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Toggle state enum values from UIA COM
_TOGGLE_STATE_NAMES = {0: "Off", 1: "On", 2: "Indeterminate"}


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

    async def _find_element_scoped(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
    ) -> Any:
        """Find element, optionally scoped to a root container.

        Returns a pywinauto wrapper element.
        """
        if root_id:
            # Find root container first, then search within it
            root_element = await self._ui.find_element(automation_id=root_id)
            # Search within the root element's subtree
            criteria: dict[str, str] = {}
            if automation_id:
                criteria["auto_id"] = automation_id
            if name:
                criteria["title"] = name
            if control_type:
                criteria["control_type"] = control_type

            if not criteria:
                return root_element

            loop = asyncio.get_running_loop()

            def _find_in_subtree():
                child = root_element.child_window(**criteria)
                child.wait("exists", timeout=5)
                return child

            return await loop.run_in_executor(self._ui._executor, _find_in_subtree)

        return await self._ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
        )

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Find element via pywinauto.

        Raises NotImplementedError if xpath is provided — pywinauto backend does not
        support XPath. Use FlaUI backend or remove the xpath argument.
        """
        if xpath:
            raise NotImplementedError(
                "XPath search requires FlaUI backend. "
                "Install FlaUIBridge.exe or use automationId/name/controlType search instead."
            )
        element = await self._find_element_scoped(automation_id, name, control_type, root_id)
        info = await self._ui.get_element_info(element)
        return info.to_dict()

    async def invoke_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Invoke element via pywinauto (IUIAutomationInvokePattern)."""
        if xpath:
            raise NotImplementedError(
                "XPath search requires FlaUI backend. "
                "Use automationId/name/controlType for invoke on pywinauto."
            )
        element = await self._find_element_scoped(automation_id, name, control_type, root_id)
        loop = asyncio.get_running_loop()

        def _invoke():
            method = "InvokePattern"
            try:
                iface = element.iface_invoke
                if iface is not None:
                    iface.Invoke()
                else:
                    element.click()
                    method = "Click"
            except Exception as exc:
                logger.debug("InvokePattern failed, falling back to Click: %s", exc)
                element.click()
                method = "Click"
            return method

        method = await loop.run_in_executor(self._ui._executor, _invoke)
        info = await self._ui.get_element_info(element)
        return {
            "invoked": True,
            "method": method,
            "automationId": info.automation_id,
            "name": info.name,
            "controlType": info.control_type,
        }

    async def toggle_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Toggle element via pywinauto (IUIAutomationTogglePattern)."""
        if xpath:
            raise NotImplementedError(
                "XPath search requires FlaUI backend. "
                "Use automationId/name/controlType for toggle on pywinauto."
            )
        element = await self._find_element_scoped(automation_id, name, control_type, root_id)
        loop = asyncio.get_running_loop()

        def _toggle():
            iface = element.iface_toggle
            if iface is None:
                raise RuntimeError(
                    f"Element does not support TogglePattern. "
                    f"Control type: {element.element_info.control_type}"
                )
            iface.Toggle()
            new_state_int = iface.CurrentToggleState
            return _TOGGLE_STATE_NAMES.get(new_state_int, str(new_state_int))

        new_state = await loop.run_in_executor(self._ui._executor, _toggle)
        info = await self._ui.get_element_info(element)
        return {
            "toggled": True,
            "newState": new_state,
            "automationId": info.automation_id,
            "name": info.name,
            "controlType": info.control_type,
        }

    async def find_by_xpath(
        self,
        xpath: str,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        """XPath search is not supported on pywinauto backend."""
        raise NotImplementedError(
            "XPath search requires FlaUI backend. "
            "Install FlaUIBridge.exe or use automationId/name/controlType search instead."
        )

    async def find_all_cascade(
        self,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Find all matching elements with basic ranking on pywinauto."""
        loop = asyncio.get_running_loop()

        def _find_all():
            if self._ui._app is None:
                return {"results": [], "totalMatches": 0}

            # Scope search to root_id container when specified
            if root_id:
                try:
                    search_root = self._ui._app.top_window().child_window(auto_id=root_id)
                    search_root.wait("exists", timeout=5)
                except Exception:
                    return {"results": [], "totalMatches": 0}
            else:
                search_root = self._ui._app.top_window()

            criteria: dict[str, str] = {}
            if name:
                criteria["title"] = name
            if control_type:
                criteria["control_type"] = control_type

            if not criteria:
                return {"results": [], "totalMatches": 0}

            # Find all matching descendants within the scoped root
            try:
                matches = search_root.descendants(**criteria)
            except Exception:
                return {"results": [], "totalMatches": 0}

            total = len(matches)
            # Simple ranking: prefer enabled, visible, shallower elements
            scored = []
            for el in matches[:max_results * 2]:  # Over-sample for ranking
                try:
                    info = el.element_info
                    score = 0
                    try:
                        if el.is_enabled():
                            score += 10
                    except Exception:
                        pass
                    try:
                        if el.is_visible():
                            score += 10
                    except Exception:
                        pass
                    scored.append({
                        "found": True,
                        "automationId": getattr(info, "automation_id", "") or "",
                        "name": getattr(info, "name", "") or "",
                        "controlType": getattr(info, "control_type", "") or "",
                        "score": score,
                        "depth": 0,  # pywinauto doesn't expose depth easily
                        "parentDesc": "",
                    })
                except Exception:
                    continue

            scored.sort(key=lambda x: x["score"], reverse=True)
            return {"results": scored[:max_results], "totalMatches": total}

        return await loop.run_in_executor(self._ui._executor, _find_all)

    async def extract_text(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Extract text using multi-strategy fallback on pywinauto."""
        if xpath:
            raise NotImplementedError(
                "XPath search requires FlaUI backend. "
                "Use automationId/name/controlType for text extraction on pywinauto."
            )
        element = await self._find_element_scoped(automation_id, name, control_type, root_id)
        loop = asyncio.get_running_loop()

        def _extract():
            # Strategy 1: ValuePattern
            try:
                iface = element.iface_value
                if iface is not None:
                    val = iface.CurrentValue
                    if val:
                        return {"text": val, "source": "ValuePattern"}
            except Exception:
                pass

            # Strategy 2: Name property
            try:
                el_name = element.element_info.name
                if el_name:
                    return {"text": el_name, "source": "Name"}
            except Exception:
                pass

            # Strategy 3: window_text()
            try:
                wt = element.window_text()
                if wt:
                    return {"text": wt, "source": "WindowText"}
            except Exception:
                pass

            # Strategy 4: LegacyIAccessible name / value
            try:
                iface = element.iface_legacy_accessible
                if iface is not None:
                    legacy_name = iface.CurrentName
                    if legacy_name:
                        return {"text": legacy_name, "source": "LegacyIAccessible.Name"}
                    legacy_value = iface.CurrentValue
                    if legacy_value:
                        return {"text": legacy_value, "source": "LegacyIAccessible.Value"}
            except Exception:
                pass

            # Strategy 5: Visible text children
            try:
                children = element.descendants(control_type="Text")
                texts = []
                for child in children:
                    try:
                        cn = child.element_info.name
                        if cn:
                            texts.append(cn)
                    except Exception:
                        continue
                if texts:
                    return {"text": " ".join(texts), "source": "TextDescendants"}
            except Exception:
                pass

            return {"text": "", "source": "None"}

        return await loop.run_in_executor(self._ui._executor, _extract)

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

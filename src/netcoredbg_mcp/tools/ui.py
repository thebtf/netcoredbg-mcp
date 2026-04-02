"""UI automation tools."""

import asyncio
import logging
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP

from ..session import SessionManager

from ..response import build_error_response, build_response
from ..session.state import DebugState

logger = logging.getLogger(__name__)


def register_ui_tools(
    mcp: FastMCP,
    session: SessionManager,
    check_session_access: Callable[[Any], str | None],
) -> None:
    """Register UI automation tools on the MCP server."""
    from mcp.types import ToolAnnotations

    # Lazy-loaded UI automation instance
    _ui_holder: dict[str, Any] = {"instance": None}

    # Cache for last annotated screenshot (used by ui_click_annotated)
    _last_annotation: dict[str, Any] | None = None

    def _get_ui() -> Any:
        """Get or create UI automation instance."""
        if _ui_holder["instance"] is None:
            from ..ui import UIAutomation
            _ui_holder["instance"] = UIAutomation()
        return _ui_holder["instance"]

    async def _ensure_ui_connected() -> Any:
        """Ensure UI automation is connected to the debug process.

        Raises:
            NoActiveSessionError: If no debug session is active
            NoProcessIdError: If process ID not available
        """
        from ..ui import NoActiveSessionError, NoProcessIdError

        if session.state.state == DebugState.IDLE:
            raise NoActiveSessionError("No debug session is active. Start debugging first.")

        process_id = session.state.process_id
        if not process_id:
            raise NoProcessIdError(
                "Process ID not available. Debug session may not have started the process yet."
            )

        ui = _get_ui()
        if ui.process_id != process_id:
            await ui.connect(process_id)
        return ui

    async def _find_ui_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ):
        """Helper to connect to UI and find an element."""
        ui = await _ensure_ui_connected()
        element = await ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
        )
        return ui, element

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_get_window_tree(max_depth: int = 3, max_children: int = 50) -> dict:
        """
        Get the visual tree of the debugged application's main window.

        Use this to understand the UI structure before interacting with elements.
        Call after start_debug and wait for the application window to appear.

        Args:
            max_depth: Maximum depth to traverse (default 3)
            max_children: Maximum children per element (default 50)

        Returns:
            Visual tree with automationId, controlType, name, isEnabled, etc.
        """
        try:
            ui = await _ensure_ui_connected()
            tree = await ui.get_window_tree(max_depth, max_children)
            return build_response(data=tree.to_dict(), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_find_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Find a UI element by AutomationId, name, or control type.

        At least one search criterion must be provided.
        Use ui_get_window_tree first to discover available elements.

        Args:
            automation_id: AutomationId property (most reliable for WPF)
            name: Element's Name/Title property
            control_type: Type like "Button", "TextBox", "MenuItem"

        Returns:
            Element info if found
        """
        try:
            ui = await _ensure_ui_connected()
            element = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
            )
            info = await ui.get_element_info(element)
            return build_response(data=info.to_dict(), state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
    async def ui_set_focus(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Set keyboard focus to a UI element.

        Call this before ui_send_keys to ensure keys go to the right element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.set_focus(element)
            return build_response(data={"focused": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys(
        ctx: Context,
        keys: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Send keyboard input to a UI element.

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}"
        - Tab: "{TAB}"
        - Escape: "{ESC}"
        - Ctrl+C: "^c"
        - Alt+F4: "%{F4}"
        - Shift+Tab: "+{TAB}"
        - Ctrl+Shift+S: "^+s"

        Args:
            keys: Keys to send (pywinauto syntax)
            automation_id: Target element's AutomationId
            name: Target element's Name
            control_type: Target element's control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.send_keys(element, keys)
            return build_response(data={"sent": keys}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys_focused(ctx: Context, keys: str) -> dict:
        """
        Send keyboard input to the currently focused element.

        Use this AFTER ui_set_focus to avoid re-searching for complex elements
        like DataGrid that may timeout on repeated searches.

        Workflow:
        1. ui_set_focus(automation_id="MyElement")  # Focus the element
        2. ui_send_keys_focused(keys="^{END}")      # Send keys without re-search

        Uses pywinauto keyboard syntax:
        - Regular text: "hello"
        - Enter: "{ENTER}", Tab: "{TAB}", Escape: "{ESC}"
        - Ctrl+C: "^c", Alt+F4: "%{F4}", Shift+Tab: "+{TAB}"
        - Arrow keys: "{LEFT}", "{RIGHT}", "{UP}", "{DOWN}"
        - Ctrl+End: "^{END}", Ctrl+Home: "^{HOME}"

        Args:
            keys: Keys to send (pywinauto syntax)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            await ui.send_keys_focused(keys)
            return build_response(
                data={"sent": keys, "target": "focused"}, state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """
        Click on a UI element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui, element = await _find_ui_element(automation_id, name, control_type)
            await ui.click(element)
            return build_response(data={"clicked": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Screenshot & annotation tools --

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_take_screenshot(ctx: Context) -> dict:
        """Take a screenshot of the debugged application's window.

        Returns the screenshot as a base64-encoded PNG image.
        Use this to see the actual UI state — what the user would see.

        Useful for:
        - Verifying UI rendered correctly after a debug step
        - Finding elements that don't appear in the automation tree
        - Understanding visual layout and spacing
        - Debugging rendering issues
        """
        try:
            from ..ui.screenshot import get_hwnd_for_pid, capture_window_as_base64

            pid = session.state.process_id
            if not pid:
                return build_error_response("No debug process. Start debugging first.", state=session.state.state)

            hwnd = get_hwnd_for_pid(pid)
            if not hwnd:
                return build_error_response(
                    f"No visible window found for process {pid}. The app may not have a UI yet.",
                    state=session.state.state,
                )

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: capture_window_as_base64(hwnd))

            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_take_annotated_screenshot(
        ctx: Context,
        max_depth: int = 3,
        interactive_only: bool = True,
    ) -> dict:
        """Take a screenshot with numbered UI elements overlaid (Set-of-Mark pattern).

        Returns annotated PNG + element index. Each interactive element gets a
        numbered label on the screenshot and an entry in the index.

        Use this when you need to:
        - See the UI AND know which elements are interactive
        - Find elements that are hard to locate by name/AutomationId
        - Understand spatial relationships between elements

        Use ui_click_annotated(element_id) to interact with elements by number.

        Args:
            max_depth: How deep to traverse the UI tree (default 3)
            interactive_only: Only show interactive elements like buttons/textboxes (default True)
        """
        nonlocal _last_annotation

        try:
            from ..ui.screenshot import (
                get_hwnd_for_pid, capture_window, collect_visible_elements, annotate_screenshot,
            )
            import ctypes
            from ctypes import wintypes

            pid = session.state.process_id
            if not pid:
                return build_error_response("No debug process.", state=session.state.state)

            hwnd = get_hwnd_for_pid(pid)
            if not hwnd:
                return build_error_response(
                    f"No visible window for process {pid}.",
                    state=session.state.state,
                )

            ui = _get_ui()
            if ui.process_id != pid:
                await ui.connect(pid)

            loop = asyncio.get_running_loop()

            # Capture screenshot
            png_bytes, width, height = await loop.run_in_executor(
                None, lambda: capture_window(hwnd),
            )

            # Collect elements
            elements = await loop.run_in_executor(
                None, lambda: collect_visible_elements(ui._app, max_depth, interactive_only),
            )

            # Get window screen position for coordinate conversion
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            window_rect = (rect.left, rect.top, rect.right, rect.bottom)

            # Annotate screenshot
            annotated_bytes = await loop.run_in_executor(
                None, lambda: annotate_screenshot(png_bytes, elements, window_rect),
            )

            # Cache elements for ui_click_annotated
            _last_annotation = {
                "elements": elements,
                "window_rect": window_rect,
                "hwnd": hwnd,
            }

            import base64
            return build_response(
                data={
                    "image": base64.b64encode(annotated_bytes).decode("ascii"),
                    "width": width,
                    "height": height,
                    "elements": [
                        {
                            "id": e["id"],
                            "name": e["name"],
                            "type": e["type"],
                            "automationId": e["automationId"],
                        }
                        for e in elements
                    ],
                    "element_count": len(elements),
                },
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click_annotated(ctx: Context, element_id: int) -> dict:
        """Click an element by its ID from ui_take_annotated_screenshot.

        Uses the numbered element from the last annotated screenshot.
        Call ui_take_annotated_screenshot first to get element IDs.

        Args:
            element_id: Element ID number from the annotated screenshot
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            if _last_annotation is None:
                return build_error_response(
                    "No annotation data. Call ui_take_annotated_screenshot first.",
                    state=session.state.state,
                )

            elements = _last_annotation["elements"]
            target = None
            for e in elements:
                if e["id"] == element_id:
                    target = e
                    break

            if target is None:
                return build_error_response(
                    f"Element {element_id} not found. Valid IDs: {[e['id'] for e in elements]}",
                    state=session.state.state,
                )

            # Click center of element bounds
            bounds = target["bounds"]
            center_x = bounds["x"] + bounds["width"] // 2
            center_y = bounds["y"] + bounds["height"] // 2

            import ctypes
            loop = asyncio.get_running_loop()

            def _click_at(x: int, y: int) -> None:
                ctypes.windll.user32.SetCursorPos(x, y)
                MOUSEEVENTF_LEFTDOWN = 0x0002
                MOUSEEVENTF_LEFTUP = 0x0004
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            await loop.run_in_executor(None, lambda: _click_at(center_x, center_y))

            return build_response(
                data={"clicked": True, "element": target["name"], "position": {"x": center_x, "y": center_y}},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Advanced interaction tools --

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_select_items(
        ctx: Context,
        automation_id: str,
        indices: list[int],
        mode: str = "replace",
    ) -> dict:
        """Select items by index in a list/grid control (DataGrid, ListView, ListBox).

        Uses UIA SelectionItemPattern for reliable multi-select without coordinate guessing.
        Works even for off-screen items (no scrolling needed).

        Args:
            automation_id: AutomationId of the list/grid control
            indices: List of 0-based item indices to select
            mode: "replace" (clear existing, select these) or "add" (add to existing selection)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            def _select() -> int:
                window = ui._app.top_window()
                control = window.child_window(auto_id=automation_id)
                control.wait("exists", timeout=5)

                items = control.children()
                selected_count = 0

                for i, item in enumerate(items):
                    try:
                        iface = item.iface_selection_item
                    except Exception:
                        continue

                    if i in indices:
                        if mode == "replace" and selected_count == 0:
                            iface.select()
                        else:
                            iface.add_to_selection()
                        selected_count += 1
                    elif mode == "replace":
                        try:
                            iface.remove_from_selection()
                        except Exception:
                            pass

                return selected_count

            loop = asyncio.get_running_loop()
            selected = await asyncio.wait_for(
                loop.run_in_executor(None, _select), timeout=10.0,
            )

            return build_response(
                data={"selected": selected, "indices": indices, "mode": mode},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_right_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """Right-click on a UI element to open context menu.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui, element = await _find_ui_element(automation_id, name, control_type)

            def _right_click() -> None:
                element.click_input(button="right")

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _right_click), timeout=5.0,
            )

            return build_response(data={"right_clicked": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_double_click(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> dict:
        """Double-click on a UI element.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui, element = await _find_ui_element(automation_id, name, control_type)

            def _double_click() -> None:
                element.double_click_input()

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _double_click), timeout=5.0,
            )

            return build_response(data={"double_clicked": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_scroll(
        ctx: Context,
        automation_id: str,
        direction: str = "down",
        amount: int = 3,
    ) -> dict:
        """Scroll a UI control.

        Args:
            automation_id: AutomationId of the scrollable control
            direction: "up", "down", "left", "right"
            amount: Number of scroll units (default 3)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            def _scroll() -> None:
                window = ui._app.top_window()
                control = window.child_window(auto_id=automation_id)
                control.wait("exists", timeout=5)
                control.scroll(direction, "page", amount)

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _scroll), timeout=5.0,
            )

            return build_response(
                data={"scrolled": True, "direction": direction, "amount": amount},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_drag(
        ctx: Context,
        from_automation_id: str,
        to_automation_id: str,
    ) -> dict:
        """Drag from one UI element to another.

        Args:
            from_automation_id: AutomationId of the source element
            to_automation_id: AutomationId of the target element
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            def _drag() -> None:
                window = ui._app.top_window()
                from_elem = window.child_window(auto_id=from_automation_id)
                from_elem.wait("exists", timeout=5)
                to_elem = window.child_window(auto_id=to_automation_id)
                to_elem.wait("exists", timeout=5)
                from_elem.drag_mouse_input(dst=to_elem)

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _drag), timeout=10.0,
            )

            return build_response(data={"dragged": True}, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

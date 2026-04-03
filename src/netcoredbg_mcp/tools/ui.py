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

    # Lazy-loaded UI backend instance (FlaUI or pywinauto)
    _backend_holder: dict[str, Any] = {"instance": None}

    # Cache for last annotated screenshot (used by ui_click_annotated)
    _last_annotation: dict[str, Any] | None = None
    _annotation_generation: int = 0

    def _get_backend() -> Any:
        """Get or create UI backend (FlaUI preferred, pywinauto fallback)."""
        if _backend_holder["instance"] is None:
            from ..ui.backend import create_backend
            _backend_holder["instance"] = create_backend(
                process_registry=session.process_registry,
            )
        return _backend_holder["instance"]

    async def _ensure_ui_connected() -> Any:
        """Ensure UI backend is connected to the debug process.

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

        backend = _get_backend()
        if backend.process_id != process_id:
            await backend.connect(process_id)
        return backend

    async def _find_ui_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ):
        """Helper to connect and find element (pywinauto fallback path).

        Returns (backend, element) where element is a pywinauto wrapper.
        For FlaUI backend, the element is the find_element dict result.
        """
        ui = await _ensure_ui_connected()
        from ..ui.pywinauto_backend import PywinautoBackend
        if isinstance(ui, PywinautoBackend):
            element = await ui.inner.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
            )
            return ui, element
        # FlaUI backend: find_element returns a dict
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
            result = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
            )
            # Backend returns dict directly (both FlaUI and pywinauto)
            data = result if isinstance(result, dict) else result.to_dict()
            return build_response(data=data, state=session.state.state)
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

        Tries cached coordinates first (click to focus), then falls back to
        pywinauto element search.

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

            # Try cache first — click to set focus
            if automation_id:
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.click_at(cx, cy)
                    return build_response(
                        data={"focused": True, "method": "cache"},
                        state=session.state.state,
                    )

            # Fallback: find element and click to focus
            ui, element = await _find_ui_element(automation_id, name, control_type)
            from ..ui.pywinauto_backend import PywinautoBackend
            if isinstance(ui, PywinautoBackend):
                await ui.inner.set_focus(element)
            else:
                # FlaUI: find_element returns dict with rect, click center
                rect = element.get("rect", {}) if isinstance(element, dict) else {}
                if rect:
                    cx = int(rect.get("x", 0) + rect.get("width", 0) / 2)
                    cy = int(rect.get("y", 0) + rect.get("height", 0) / 2)
                    await ui.click_at(cx, cy)
            return build_response(data={"focused": True, "method": "element_search"}, state=session.state.state)
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

        Tries cached coordinates first (click to focus, then send keys),
        then falls back to pywinauto element search.

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

            # Try cache first — click to focus, then send keys without element search
            if automation_id:
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.click_at(cx, cy)
                    await ui.send_keys(keys)
                    return build_response(
                        data={"sent": keys, "method": "cache"},
                        state=session.state.state,
                    )

            # Fallback: click element to focus, then send keys
            ui, element = await _find_ui_element(automation_id, name, control_type)
            from ..ui.pywinauto_backend import PywinautoBackend
            if isinstance(ui, PywinautoBackend):
                await ui.inner.send_keys(element, keys)
            else:
                await ui.send_keys(keys)
            return build_response(data={"sent": keys, "method": "element_search"}, state=session.state.state)
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
            await ui.send_keys(keys)
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

        Tries cached coordinates first (from last ui_get_window_tree call),
        then falls back to pywinauto element search.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Try cache first (fast, reliable)
            if automation_id:
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.click_at(cx, cy)
                    return build_response(
                        data={"clicked": True, "method": "cache", "position": {"x": cx, "y": cy}},
                        state=session.state.state,
                    )

            # Fallback to element search
            try:
                ui, element = await _find_ui_element(automation_id, name, control_type)
                from ..ui.pywinauto_backend import PywinautoBackend
                if isinstance(ui, PywinautoBackend):
                    await ui.inner.click(element)
                else:
                    # FlaUI: element is dict with rect
                    rect = element.get("rect", {}) if isinstance(element, dict) else {}
                    if rect:
                        cx = int(rect.get("x", 0) + rect.get("width", 0) / 2)
                        cy = int(rect.get("y", 0) + rect.get("height", 0) / 2)
                        await ui.click_at(cx, cy)
                return build_response(data={"clicked": True, "method": "element_search"}, state=session.state.state)
            except Exception:
                # Last resort: if element found but click fails (e.g., DataGrid has no click wrapper),
                # try coordinate click from element's bounding rectangle
                if automation_id:
                    ui = await _ensure_ui_connected()
                    rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                    if rect:
                        cx = (rect["left"] + rect["right"]) // 2
                        cy = (rect["top"] + rect["bottom"]) // 2
                        await ui.click_at(cx, cy)
                        return build_response(
                            data={"clicked": True, "method": "coord_fallback", "position": {"x": cx, "y": cy}},
                            state=session.state.state,
                        )
                raise  # re-raise if no fallback possible
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click_at(ctx: Context, x: int, y: int) -> dict:
        """Click at absolute screen coordinates.

        Use with ui_get_window_tree rectangle data when element search fails.
        Get coordinates from the 'rectangle' field in tree output.
        Click goes to the center: x = (left + right) / 2, y = (top + bottom) / 2

        Args:
            x: Screen X coordinate
            y: Screen Y coordinate
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            await ui.click_at(x, y)
            return build_response(
                data={"clicked": True, "position": {"x": x, "y": y}},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Screenshot & annotation tools --

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_take_screenshot(
        ctx: Context,
        max_width: int = 1024,
        format: str = "webp",
    ) -> Any:
        """Take a screenshot of the debugged application's window.

        Returns inline ImageContent (WebP preview ≤480px) directly to your
        vision pipeline, plus TextContent with metadata and HD file path.
        The HD file can be read with the Read tool for full resolution.

        Use this to see the actual UI state — what the user would see.

        Useful for:
        - Verifying UI rendered correctly after a debug step
        - Finding elements that don't appear in the automation tree
        - Understanding visual layout and spacing
        - Debugging rendering issues

        Args:
            max_width: Maximum image width (default 1024 — optimal for Claude vision)
            format: Image format: "webp" (smallest), "jpeg", "png"
        """
        _VALID_FORMATS = {"webp", "jpeg", "png"}

        try:
            import base64
            import json
            import time as _time
            from mcp.types import TextContent, ImageContent
            from ..ui.screenshot import get_hwnd_for_pid, capture_window, _process_screenshot, create_preview

            # Validate format against allow-list
            safe_format = format if format in _VALID_FORMATS else "webp"

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

            # Capture raw screenshot
            png_bytes, _, _ = await loop.run_in_executor(
                None, lambda: capture_window(hwnd),
            )

            # Create HD version in requested format
            hd_bytes, hd_w, hd_h, _ = await loop.run_in_executor(
                None, lambda: _process_screenshot(png_bytes, max_width=max_width, format=safe_format),
            )

            # Create inline preview (≤480px WebP)
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None, lambda: create_preview(png_bytes, max_width=480, quality=75),
            )

            # Save HD to session temp dir
            metadata: dict[str, Any] = {
                "width": hd_w,
                "height": hd_h,
                "preview_width": preview_w,
                "format": safe_format,
                "state": session.state.state.value if hasattr(session.state.state, "value") else str(session.state.state),
            }

            sid = session.session_id
            if sid:
                ts = int(_time.time() * 1000) & 0xFFFFFFFF
                hd_path = session.temp_manager.save_screenshot(
                    sid, hd_bytes, f"screenshot_{ts:08x}.{safe_format}",
                )
                if hd_path:
                    metadata["hd_path"] = str(hd_path)

            content: list = [
                ImageContent(
                    type="image",
                    data=base64.b64encode(preview_bytes).decode("ascii"),
                    mimeType="image/webp",
                ),
                TextContent(
                    type="text",
                    text=json.dumps(metadata),
                ),
            ]
            return content
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_take_annotated_screenshot(
        ctx: Context,
        max_depth: int = 3,
        interactive_only: bool = True,
        max_width: int = 1024,
        format: str = "webp",
        compact: bool = True,
    ) -> Any:
        """Take a screenshot with numbered UI elements overlaid (Set-of-Mark pattern).

        Returns annotated WebP image + compact element index.
        Each interactive element gets a numbered label on the screenshot.

        Use ui_click_annotated(element_id) to interact with elements by number.

        Args:
            max_depth: How deep to traverse the UI tree (default 3)
            interactive_only: Only interactive elements (default True)
            max_width: Max image width (default 1024)
            format: Image format: "webp" (smallest), "jpeg", "png"
            compact: Compact element index — id+name only (default True, saves ~60KB)
        """
        nonlocal _last_annotation, _annotation_generation

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

            backend = await _ensure_ui_connected()

            loop = asyncio.get_running_loop()

            # Capture screenshot
            png_bytes, _, _ = await loop.run_in_executor(
                None, lambda: capture_window(hwnd),
            )

            # Collect elements — needs pywinauto _app access
            from ..ui.pywinauto_backend import PywinautoBackend
            if isinstance(backend, PywinautoBackend):
                app = backend.inner._app
            else:
                # FlaUI backend: fall back to connecting pywinauto just for element collection
                from ..ui import UIAutomation
                _fallback_ui = UIAutomation()
                await _fallback_ui.connect(pid)
                app = _fallback_ui._app

            elements = await loop.run_in_executor(
                None, lambda: collect_visible_elements(app, max_depth, interactive_only),
            )

            # Get window screen position for coordinate conversion
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            window_rect = (rect.left, rect.top, rect.right, rect.bottom)

            # Annotate screenshot (with optional downsampling)
            annotated_bytes = await loop.run_in_executor(
                None, lambda: annotate_screenshot(png_bytes, elements, window_rect, max_width),
            )

            # Cache elements for ui_click_annotated
            _annotation_generation += 1
            _last_annotation = {
                "elements": elements,
                "window_rect": window_rect,
                "hwnd": hwnd,
                "generation": _annotation_generation,
            }

            # Convert to optimal format
            from ..ui.screenshot import _process_screenshot, create_preview
            import base64
            import json
            from mcp.types import TextContent, ImageContent

            _VALID_FORMATS = {"webp", "jpeg", "png"}
            safe_format = format if format in _VALID_FORMATS else "webp"

            hd_bytes, hd_w, hd_h, _ = await loop.run_in_executor(
                None, lambda: _process_screenshot(annotated_bytes, max_width=max_width, format=safe_format),
            )

            # Create inline preview (≤480px WebP) — in executor to avoid blocking loop
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None, lambda: create_preview(annotated_bytes, max_width=480, quality=75),
            )

            # Build element index — compact (id+name) or full (id+name+type+automationId)
            if compact:
                elem_index = [
                    f"{e['id']}: {e['name'] or e['automationId'] or e['type']}"
                    for e in elements
                ]
            else:
                elem_index = [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "type": e["type"],
                        "automationId": e["automationId"],
                    }
                    for e in elements
                ]

            # Save HD to session temp dir
            metadata: dict[str, Any] = {
                "width": hd_w,
                "height": hd_h,
                "preview_width": preview_w,
                "elements": elem_index,
                "element_count": len(elements),
                "generation": _annotation_generation,
                "format": safe_format,
                "state": session.state.state.value if hasattr(session.state.state, "value") else str(session.state.state),
            }

            sid = session.session_id
            if sid:
                hd_path = session.temp_manager.save_screenshot(
                    sid, hd_bytes, f"annotated_{_annotation_generation:04d}.{safe_format}",
                )
                if hd_path:
                    metadata["hd_path"] = str(hd_path)

            content: list = [
                ImageContent(
                    type="image",
                    data=base64.b64encode(preview_bytes).decode("ascii"),
                    mimeType="image/webp",
                ),
                TextContent(
                    type="text",
                    text=json.dumps(metadata),
                ),
            ]
            return content
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_click_annotated(ctx: Context, element_id: int, generation: int | None = None) -> dict:
        """Click an element by its ID from ui_take_annotated_screenshot.

        Uses the numbered element from the last annotated screenshot.
        Call ui_take_annotated_screenshot first to get element IDs.

        Args:
            element_id: Element ID number from the annotated screenshot
            generation: Generation counter from the screenshot response (optional, warns if stale)
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

            # Warn if annotation data may be stale
            stale_warning = None
            current_gen = _last_annotation.get("generation", 0)
            if generation is not None and generation != current_gen:
                stale_warning = (
                    f"Annotation data may be stale: requested generation {generation}, "
                    f"current is {current_gen}. Consider retaking the screenshot."
                )
                logger.warning(stale_warning)

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

            # Click center of element bounds using centralized SendInput implementation
            bounds = target["bounds"]
            center_x = bounds["x"] + bounds["width"] // 2
            center_y = bounds["y"] + bounds["height"] // 2

            ui = await _ensure_ui_connected()
            await ui.click_at(center_x, center_y)

            response_data: dict[str, Any] = {
                "clicked": True,
                "element": target["name"],
                "position": {"x": center_x, "y": center_y},
            }
            if stale_warning:
                response_data["warning"] = stale_warning

            return build_response(
                data=response_data,
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    # -- Advanced interaction tools --

    async def _select_via_clicks(ui_inst, automation_id: str, indices: list[int], mode: str) -> int:
        """Fallback: select items by Ctrl+clicking their cached coordinates.

        Does a deeper tree walk (depth=5) to find ListBoxItem/DataItem children,
        then clicks them with Ctrl held for multi-select.
        """
        import ctypes

        # Deep tree walk to find child items
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: ui_inst.get_window_tree.__wrapped__(ui_inst, max_depth=5, max_children=100)
            if hasattr(ui_inst.get_window_tree, "__wrapped__")
            else None,
        )

        # Rebuild: find items inside the control's bounds from refreshed cache
        cache = ui_inst._element_cache
        parent_data = cache.get(automation_id)
        if not parent_data or not parent_data.get("rect"):
            return 0

        pr = parent_data["rect"]

        # Collect ListItem/DataItem children within parent bounds, sorted by Y position
        child_items = []
        for aid, data in cache.items():
            if aid == automation_id:
                continue
            r = data.get("rect")
            ct = data.get("control_type", "")
            if not r or ct not in ("ListItem", "DataItem", "TreeItem", "ListBoxItem"):
                continue
            if (pr["left"] <= r["left"] and r["right"] <= pr["right"]
                    and pr["top"] <= r["top"] and r["bottom"] <= pr["bottom"]):
                child_items.append(r)

        # Sort by vertical position (top to bottom)
        child_items.sort(key=lambda r: r["top"])

        selected = 0
        VK_CONTROL = 0x11
        KEYEVENTF_KEYUP = 0x0002

        # Click first item (plain click to set initial selection)
        first_done = False
        for target_idx in indices:
            if target_idx >= len(child_items):
                continue
            rect = child_items[target_idx]
            cx = (rect["left"] + rect["right"]) // 2
            cy = (rect["top"] + rect["bottom"]) // 2
            await ui_inst._click_at_coords(cx, cy)
            selected += 1
            first_done = True
            break

        # Remaining items: hold Ctrl for entire sequence, click each
        remaining = [i for i in indices if i != indices[0] and i < len(child_items)]
        if remaining and first_done:
            # Press Ctrl ONCE before all remaining clicks
            ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
            try:
                for target_idx in remaining:
                    rect = child_items[target_idx]
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await asyncio.sleep(0.05)
                    await ui_inst._click_at_coords(cx, cy)
                    selected += 1
            finally:
                # ALWAYS release Ctrl
                ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

        return selected

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_select_items(
        ctx: Context,
        automation_id: str,
        indices: list[int],
        mode: str = "replace",
    ) -> dict:
        """Select items by index in a list/grid control (DataGrid, ListView, ListBox).

        With FlaUI backend: uses SelectionItemPattern (reliable for virtualized lists).
        With pywinauto backend: two strategies (tries both):
        1. UIA SelectionItemPattern — works for non-virtualized lists
        2. Coordinate click fallback — clicks items using cached rectangles
           (Ctrl+click for multi-select, plain click for first item)

        For WPF virtualized lists (VirtualizingStackPanel), strategy 1 may fail
        because off-screen items don't have UI containers. Strategy 2 uses visible
        item coordinates from the cache. FlaUI backend handles this natively.

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

            # Strategy 1: UIA SelectionItemPattern
            def _select_via_pattern() -> int:
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
            try:
                selected = await asyncio.wait_for(
                    loop.run_in_executor(None, _select_via_pattern), timeout=10.0,
                )
            except Exception:
                # Strategy 1 failed (e.g., add_to_selection error on Extended ListBox)
                selected = 0

            # Strategy 2: coordinate Ctrl+click fallback
            if selected < len(indices):
                selected = await _select_via_clicks(ui, automation_id, indices, mode)

            method = "pattern" if selected == len(indices) else "click_fallback"
            return build_response(
                data={"selected": selected, "indices": indices, "mode": mode, "method": method},
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

        Tries cached coordinates first, then falls back to pywinauto element search.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Try cache first
            if automation_id:
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.right_click_at(cx, cy)
                    return build_response(
                        data={"right_clicked": True, "method": "cache", "position": {"x": cx, "y": cy}},
                        state=session.state.state,
                    )

            # Fallback to element search
            ui, element = await _find_ui_element(automation_id, name, control_type)

            def _right_click() -> None:
                element.click_input(button="right")

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _right_click), timeout=5.0,
            )

            return build_response(data={"right_clicked": True, "method": "element_search"}, state=session.state.state)
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

        Tries cached coordinates first, then falls back to pywinauto element search.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            # Try cache first
            if automation_id:
                ui = await _ensure_ui_connected()
                rect = (ui.element_cache.get(automation_id) or {}).get("rect")
                if rect:
                    cx = (rect["left"] + rect["right"]) // 2
                    cy = (rect["top"] + rect["bottom"]) // 2
                    await ui.double_click_at(cx, cy)
                    return build_response(
                        data={"double_clicked": True, "method": "cache", "position": {"x": cx, "y": cy}},
                        state=session.state.state,
                    )

            # Fallback to element search
            ui, element = await _find_ui_element(automation_id, name, control_type)

            def _double_click() -> None:
                element.double_click_input()

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _double_click), timeout=5.0,
            )

            return build_response(data={"double_clicked": True, "method": "element_search"}, state=session.state.state)
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
        from_automation_id: str | None = None,
        to_automation_id: str | None = None,
        from_x: int | None = None,
        from_y: int | None = None,
        to_x: int | None = None,
        to_y: int | None = None,
    ) -> dict:
        """Drag from one position to another.

        Two modes:
        1. By AutomationId: from_automation_id + to_automation_id (uses cached rectangles)
        2. By coordinates: from_x, from_y, to_x, to_y (absolute screen coords)

        For mode 1, call ui_get_window_tree first to populate cache.

        Args:
            from_automation_id: Source element AutomationId
            to_automation_id: Target element AutomationId
            from_x: Source X coordinate (screen absolute)
            from_y: Source Y coordinate
            to_x: Target X coordinate
            to_y: Target Y coordinate
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # Resolve coordinates from automation IDs if needed
            fx, fy, tx, ty = from_x, from_y, to_x, to_y

            if from_automation_id and not (fx and fy):
                from_rect = (ui.element_cache.get(from_automation_id) or {}).get("rect")
                if from_rect:
                    fx = (from_rect["left"] + from_rect["right"]) // 2
                    fy = (from_rect["top"] + from_rect["bottom"]) // 2
                else:
                    return build_error_response(
                        f"Element '{from_automation_id}' not in cache. Call ui_get_window_tree first.",
                        state=session.state.state,
                    )

            if to_automation_id and not (tx and ty):
                to_rect = (ui.element_cache.get(to_automation_id) or {}).get("rect")
                if to_rect:
                    tx = (to_rect["left"] + to_rect["right"]) // 2
                    ty = (to_rect["top"] + to_rect["bottom"]) // 2
                else:
                    return build_error_response(
                        f"Element '{to_automation_id}' not in cache. Call ui_get_window_tree first.",
                        state=session.state.state,
                    )

            if not all([fx, fy, tx, ty]):
                return build_error_response(
                    "Provide either automation_ids or coordinates for both source and target.",
                    state=session.state.state,
                )

            await ui.drag(fx, fy, tx, ty)

            return build_response(
                data={"dragged": True, "from": {"x": fx, "y": fy}, "to": {"x": tx, "y": ty}},
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

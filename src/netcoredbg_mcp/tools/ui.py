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
        root_id: str | None = None,
        xpath: str | None = None,
    ):
        """Helper to connect and find element with ambiguity detection.

        Returns (backend, element, ambiguity_info) where:
        - element is a pywinauto wrapper or FlaUI dict
        - ambiguity_info is None (single match) or dict with candidateCount + warning

        When searching by name/controlType (not automationId/xpath), uses
        find_all_cascade to detect multiple matches and returns the best-ranked one.
        The top-ranked element from find_all_cascade is used for the actual selection,
        not just for ambiguity reporting.
        """
        ui = await _ensure_ui_connected()
        from ..ui.pywinauto_backend import PywinautoBackend
        if isinstance(ui, PywinautoBackend):
            element = await ui._find_element_scoped(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
            )
            return ui, element, None

        # FlaUI backend: use find_all_cascade when searching by name/controlType
        # so we select the best-ranked element, not simply the first match found.
        ambiguity_info = None
        if not automation_id and not xpath and (name or control_type):
            try:
                ranked = await ui.find_all_cascade(
                    name=name, control_type=control_type, root_id=root_id, max_results=5,
                )
                results = ranked.get("results", [])
                total = ranked.get("totalMatches", 0)
                if results:
                    # Use the top-ranked result as the selected element
                    top = results[0]
                    top_automation_id = top.get("automationId") or None

                    if total > 1:
                        ambiguity_info = {
                            "ambiguous": True,
                            "candidateCount": total,
                            "warning": (
                                f"Multiple matches ({total}) for search criteria. "
                                "Using best-ranked result."
                            ),
                            "alternatives": [
                                {
                                    "automationId": r.get("automationId", ""),
                                    "name": r.get("name", ""),
                                    "controlType": r.get("controlType", ""),
                                    "parentDesc": r.get("parentDesc", ""),
                                }
                                for r in results[1:4]  # Show up to 3 alternatives
                            ],
                        }

                    # Resolve the top-ranked element: prefer its automationId for
                    # a precise lookup; fall back to original criteria if no id.
                    if top_automation_id:
                        element = await ui.find_element(
                            automation_id=top_automation_id,
                            root_id=root_id,
                        )
                    else:
                        element = await ui.find_element(
                            name=top.get("name") or name,
                            control_type=top.get("controlType") or control_type,
                            root_id=root_id,
                        )

                    if ambiguity_info and isinstance(element, dict):
                        element.update(ambiguity_info)
                    return ui, element, ambiguity_info
            except Exception:
                pass  # Fall through to normal find_element

        element = await ui.find_element(
            automation_id=automation_id,
            name=name,
            control_type=control_type,
            root_id=root_id,
            xpath=xpath,
        )

        return ui, element, ambiguity_info

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
            # Backend returns dict directly (both FlaUI and pywinauto)
            data = tree if isinstance(tree, dict) else tree.to_dict()
            return build_response(data=data, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_find_element(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Find a UI element by AutomationId, name, control type, or XPath.

        At least one search criterion must be provided.
        Use ui_get_window_tree first to discover available elements.

        Args:
            automation_id: AutomationId property (most reliable for WPF)
            name: Element's Name/Title property
            control_type: Type like "Button", "TextBox", "MenuItem"
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)

        Returns:
            Element info if found
        """
        try:
            ui = await _ensure_ui_connected()
            result = await ui.find_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
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
        root_id: str | None = None,
        xpath: str | None = None,
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

            ui = await _ensure_ui_connected()

            # Use UIA-based focus via bridge (DPI/monitor-agnostic)
            from ..ui.flaui_client import FlaUIBackend
            if isinstance(ui, FlaUIBackend):
                params: dict = {}
                if automation_id:
                    params["automationId"] = automation_id
                if name:
                    params["name"] = name
                result = await ui.client.call("set_focus", params)
                return build_response(
                    data=result if isinstance(result, dict) else {"focused": True, "method": "UIA.Focus"},
                    state=session.state.state,
                )

            # Pywinauto fallback: find element and set focus
            from ..ui.pywinauto_backend import PywinautoBackend
            if isinstance(ui, PywinautoBackend):
                _, element, _ = await _find_ui_element(automation_id, name, control_type, root_id, xpath)
                await ui.inner.set_focus(element)
                return build_response(
                    data={"focused": True, "method": "pywinauto"},
                    state=session.state.state,
                )

            return build_error_response("No UI backend available", state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_send_keys(
        ctx: Context,
        keys: str,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Send keyboard input to a UI element.

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

        Tries cached coordinates first (click to focus, then send keys),
        then falls back to pywinauto element search.

        Key syntax (modifiers are PREFIX characters, special keys in braces):
        - Regular text: "hello world"
        - Modifiers: ^ = Ctrl, % = Alt, + = Shift
        - Alt+Z: "%z"    Alt+F4: "%{F4}"
        - Ctrl+C: "^c"   Ctrl+Shift+S: "^+s"
        - Shift+Tab: "+{TAB}"
        - Special keys: {ENTER} {TAB} {ESC} {DELETE} {BACKSPACE}
        - Arrow keys: {LEFT} {RIGHT} {UP} {DOWN}
        - Navigation: {HOME} {END} {PGUP} {PGDN}
        - Function keys: {F1} {F2} ... {F12}
        - Combined: Ctrl+End = "^{END}", Alt+Z = "%z"

        IMPORTANT: Modifier prefixes (^%+) apply to the NEXT character or {KEY}.
        For Alt+Z send "%z" (NOT "{ALT}z" or "Alt+Z").

        Args:
            keys: Keys to send (see syntax above)
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
            ui, element, _ = await _find_ui_element(automation_id, name, control_type, root_id, xpath)
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

        Key syntax (modifiers are PREFIX characters, special keys in braces):
        - ^ = Ctrl, % = Alt, + = Shift
        - Alt+Z: "%z"    Ctrl+C: "^c"    Shift+Tab: "+{TAB}"
        - Special: {ENTER} {TAB} {ESC} {DELETE} {BACKSPACE}
        - Arrows: {LEFT} {RIGHT} {UP} {DOWN}
        - Navigation: {HOME} {END} {PGUP} {PGDN}
        - Combined: Ctrl+End = "^{END}", Ctrl+Home = "^{HOME}"

        IMPORTANT: For Alt+Z send "%z" (NOT "{ALT}z").

        Args:
            keys: Keys to send (see syntax above)
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
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Click on a UI element.

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

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
                ui, element, _ = await _find_ui_element(automation_id, name, control_type, root_id, xpath)
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
    async def ui_invoke(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Invoke a UI element using UIA InvokePattern (no mouse movement).

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

        Preferred over ui_click for buttons, menu items, and hyperlinks because
        it works reliably even when the element is off-screen or partially obscured.
        Falls back to Click() if InvokePattern is not supported.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type (Button, MenuItem, Hyperlink, etc.)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.invoke_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_toggle(
        ctx: Context,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """
        Toggle a CheckBox or ToggleButton using UIA TogglePattern.

        Returns the new toggle state after the operation: "On", "Off", or
        "Indeterminate". Use this instead of ui_click for checkboxes to get
        reliable state feedback.

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            control_type: Control type (CheckBox, ToggleButton, etc.)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()
            result = await ui.toggle_element(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                root_id=root_id,
                xpath=xpath,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    def _escape_sendkeys_path(path: str) -> str:
        """Escape special SendKeys characters in file paths."""
        # Characters with special meaning in SendKeys: + ^ % { } ( ) ~
        result = []
        for ch in path:
            if ch in "+^%{}()~":
                result.append("{")
                result.append(ch)
                result.append("}")
            else:
                result.append(ch)
        return "".join(result)

    @mcp.tool(annotations=ToolAnnotations(openWorldHint=False))
    async def ui_file_dialog(
        ctx: Context,
        path: str,
        accept_button: str = "Open",
    ) -> dict:
        """
        Complete a standard Windows Open/Save file dialog in a single call.

        Enters the file path and clicks the accept button. Handles the standard
        Win32 dialog layout (File name ComboBox + Open/Save button) with
        multi-strategy fallback for different dialog variants.

        Args:
            path: Full file path to enter (e.g. "C:/data/test.txt")
            accept_button: Name of accept button (default "Open", use "Save" for save dialogs)
        """
        try:
            access_error = check_session_access(ctx)
            if access_error:
                return build_error_response(access_error, state=session.state.state)

            ui = await _ensure_ui_connected()

            # Strategy 1: Find file name field by standard automationId "1148"
            edit_method = ""
            try:
                combo = await ui.find_element(automation_id="1148")
                if combo.get("found"):
                    # Set value via the ComboBox (bridge handles ValuePattern)
                    from ..ui.flaui_client import FlaUIBackend
                    if isinstance(ui, FlaUIBackend):
                        await ui.client.call("set_value", {
                            "automationId": "1148",
                            "value": path,
                        })
                        edit_method = "set_value(id=1148)"
                    else:
                        # pywinauto fallback: type the path
                        await ui.send_keys(f"^a{_escape_sendkeys_path(path)}")
                        edit_method = "keyboard(Ctrl+A, type)"
            except Exception as exc:
                logger.debug("file_dialog strategy 1 (set_value) failed: %s", exc)

            # Strategy 2: Fallback — keyboard navigation
            if not edit_method:
                try:
                    # Standard dialog: Alt+N focuses the file name field
                    await ui.send_keys("%n")
                    await asyncio.sleep(0.2)
                    await ui.send_keys(f"^a{_escape_sendkeys_path(path)}")
                    edit_method = "keyboard(Alt+N, Ctrl+A, type)"
                except Exception as e:
                    return build_error_response(
                        f"Could not enter file path. This may not be a standard file dialog: {e}",
                        state=session.state.state,
                    )

            # Find and click the accept button
            button_method = ""
            try:
                # Strategy 1: Standard dialog accept button has automationId "1"
                result = await ui.invoke_element(automation_id="1")
                if result.get("invoked"):
                    button_method = "invoke(id=1)"
            except Exception:
                pass

            if not button_method:
                try:
                    # Strategy 2: Find button by name
                    result = await ui.invoke_element(name=accept_button, control_type="Button")
                    if result.get("invoked"):
                        button_method = f"invoke(name={accept_button})"
                except Exception:
                    pass

            if not button_method:
                try:
                    # Strategy 3: Press Enter as last resort
                    await ui.send_keys("{ENTER}")
                    button_method = "keyboard(Enter)"
                except Exception as e:
                    return build_error_response(
                        f"File path entered via {edit_method} but could not click accept button: {e}",
                        state=session.state.state,
                    )

            return build_response(
                data={
                    "completed": True,
                    "path": path,
                    "editMethod": edit_method,
                    "buttonMethod": button_method,
                },
                state=session.state.state,
            )
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
        max_width: int = 1568,
        format: str = "webp",
    ) -> Any:
        """Take a screenshot of the debugged application's window.

        Returns inline ImageContent (WebP at max_width resolution) directly
        to your vision pipeline, plus TextContent with metadata and HD file path.

        Use this to see the actual UI state — what the user would see.

        Useful for:
        - Verifying UI rendered correctly after a debug step
        - Finding elements that don't appear in the automation tree
        - Understanding visual layout and spacing
        - Debugging rendering issues

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

        Args:
            max_width: Maximum image width (default 1280 — optimal for Claude vision, max useful is 1568)
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

            # Create inline preview (≤1280px WebP — Claude vision optimal)
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None, lambda: create_preview(png_bytes, max_width=max_width, quality=80),
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
        max_width: int = 1568,
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

            # Create inline preview (≤max_width WebP) — in executor to avoid blocking loop
            preview_bytes, preview_w, _ = await loop.run_in_executor(
                None, lambda: create_preview(annotated_bytes, max_width=max_width, quality=80),
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
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Right-click on a UI element to open context menu.

        Tries cached coordinates first, then falls back to pywinauto element search.

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

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
            ui, element, _ = await _find_ui_element(automation_id, name, control_type, root_id, xpath)

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
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Double-click on a UI element.

        Tries cached coordinates first, then falls back to pywinauto element search.

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

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
            ui, element, _ = await _find_ui_element(automation_id, name, control_type, root_id, xpath)

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

        Note: If app is STOPPED at breakpoint, the UI is frozen. Resume with continue_execution() first.

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

    # -- Read / query tools --

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_get_selected_item(
        automation_id: str,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Get the currently selected item in a list/grid control.

        Returns the selected item's name, index, and properties.
        Useful for verifying selection state after clicks or keyboard navigation.

        Note: FlaUI backend returns selection for the first item only. For full multi-selection state, use ui_find_element to inspect individual items.

        Args:
            automation_id: AutomationId of the list/grid/combobox control
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            ui = await _ensure_ui_connected()

            def _get_selected() -> dict[str, Any]:
                from ..ui.pywinauto_backend import PywinautoBackend
                if isinstance(ui, PywinautoBackend):
                    window = ui.inner._app.top_window()
                    search_root = window
                    if root_id:
                        search_root = window.child_window(auto_id=root_id)
                        search_root.wait("exists", timeout=5)
                    control = search_root.child_window(auto_id=automation_id)
                    control.wait("exists", timeout=5)

                    # Try SelectionPattern via iface_selection
                    try:
                        selection = control.iface_selection.GetCurrentSelection()
                        if selection and selection.Length > 0:
                            selected_elem = selection.GetElement(0)
                            from pywinauto.uia_element_info import UIAElementInfo
                            elem_info = UIAElementInfo(selected_elem)
                            from pywinauto.controls.uiawrapper import UIAWrapper
                            wrapper = UIAWrapper(elem_info)
                            children = control.children()
                            idx = -1
                            for i, child in enumerate(children):
                                try:
                                    if child.element_info.runtime_id == wrapper.element_info.runtime_id:
                                        idx = i
                                        break
                                except Exception:
                                    pass
                            return {
                                "index": idx,
                                "name": wrapper.element_info.name or "",
                                "automationId": getattr(wrapper.element_info, "automation_id", "") or "",
                                "controlType": wrapper.element_info.control_type or "",
                            }
                    except Exception:
                        pass

                    # Fallback: iterate children looking for IsSelected
                    children = control.children()
                    for i, child in enumerate(children):
                        try:
                            iface = child.iface_selection_item
                            if iface.IsSelected:
                                return {
                                    "index": i,
                                    "name": child.element_info.name or "",
                                    "automationId": getattr(child.element_info, "automation_id", "") or "",
                                    "controlType": child.element_info.control_type or "",
                                }
                        except Exception:
                            continue

                    return {"index": -1, "name": "", "automationId": "", "controlType": ""}
                else:
                    return {
                        "index": -1,
                        "name": "",
                        "automationId": "",
                        "controlType": "",
                        "warning": "Selection query not yet supported via FlaUI backend",
                    }

            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _get_selected), timeout=10.0,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_read_text(
        automation_id: str | None = None,
        name: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Read text content from a UI element using multi-strategy extraction.

        Tries 5 strategies in order: ValuePattern → TextPattern → Name →
        LegacyIAccessible → visible text descendants. The response includes
        which strategy provided the text (source field).

        When the primary text looks like a CLR type name (e.g., "Namespace.Class"),
        automatically falls back to visible descendant text.

        Args:
            automation_id: AutomationId property
            name: Element's Name/Title property
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            ui = await _ensure_ui_connected()
            result = await ui.extract_text(
                automation_id=automation_id,
                name=name,
                root_id=root_id,
                xpath=xpath,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_get_focused_element() -> dict:
        """Get information about the currently focused UI element.

        Returns the focused element's automationId, name, controlType, and value.
        Useful for verifying focus state after ui_set_focus or tab navigation.

        Note: Returns the focused element within the app window. May not detect focus in OS-level dialogs.
        """
        try:
            ui = await _ensure_ui_connected()

            def _get_focused() -> dict[str, Any]:
                from ..ui.pywinauto_backend import PywinautoBackend
                if isinstance(ui, PywinautoBackend):
                    import comtypes.client  # noqa: F401
                    from pywinauto.uia_defines import IUIA

                    iuia = IUIA()
                    focused = iuia.iuia.GetFocusedElement()
                    if focused is None:
                        return {"name": "", "automationId": "", "controlType": "", "value": ""}

                    from pywinauto.uia_element_info import UIAElementInfo
                    elem_info = UIAElementInfo(focused)
                    from pywinauto.controls.uiawrapper import UIAWrapper
                    wrapper = UIAWrapper(elem_info)
                    info = wrapper.element_info

                    value = ""
                    try:
                        value = wrapper.iface_value.Value or ""
                    except Exception:
                        pass

                    return {
                        "name": info.name or "",
                        "automationId": getattr(info, "automation_id", "") or "",
                        "controlType": info.control_type or "",
                        "value": value,
                    }
                else:
                    return {
                        "name": "",
                        "automationId": "",
                        "controlType": "",
                        "value": "",
                        "warning": "Focused element query not yet supported via FlaUI backend",
                    }

            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _get_focused), timeout=5.0,
            )
            return build_response(data=result, state=session.state.state)
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    async def ui_wait_for(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        timeout: float = 5.0,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict:
        """Wait for a UI element to appear within timeout.

        Polls every 500ms until the element is found or timeout expires.
        Useful for waiting for dialogs, popups, or dynamically created elements.

        Args:
            automation_id: AutomationId to wait for
            name: Element name to wait for
            control_type: Control type to wait for
            timeout: Maximum wait time in seconds (default 5)
            root_id: Optional AutomationId to scope search to a subtree
            xpath: Optional XPath expression (FlaUI backend only)
        """
        try:
            if not any((automation_id, name, control_type, xpath)):
                return build_error_response(
                    "At least one search criterion must be provided.",
                    state=session.state.state,
                )

            # Clamp timeout to reasonable bounds
            clamped_timeout = max(0.5, min(timeout, 30.0))

            ui = await _ensure_ui_connected()

            import time as _time
            from ..ui import ElementNotFoundError

            start = _time.monotonic()
            poll_interval = 0.5
            last_error = ""

            while True:
                elapsed = _time.monotonic() - start
                if elapsed >= clamped_timeout:
                    break
                try:
                    result = await ui.find_element(
                        automation_id=automation_id,
                        name=name,
                        control_type=control_type,
                        root_id=root_id,
                        xpath=xpath,
                    )
                    data = result if isinstance(result, dict) else result.to_dict()
                    return build_response(
                        data={"found": True, "elapsed": round(elapsed, 2), "element": data},
                        state=session.state.state,
                    )
                except (ElementNotFoundError, TimeoutError, asyncio.TimeoutError):
                    remaining = clamped_timeout - elapsed
                    sleep_time = min(poll_interval, remaining)
                    if sleep_time <= 0:
                        break
                    await asyncio.sleep(sleep_time)
                except Exception as e:
                    last_error = str(e)
                    remaining = clamped_timeout - elapsed
                    sleep_time = min(poll_interval, remaining)
                    if sleep_time <= 0:
                        break
                    await asyncio.sleep(sleep_time)

            return build_error_response(
                f"Element not found within {clamped_timeout}s. Last error: {last_error}",
                state=session.state.state,
            )
        except Exception as e:
            return build_error_response(str(e), state=session.state.state)

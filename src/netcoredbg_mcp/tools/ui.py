"""UI automation tools."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
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

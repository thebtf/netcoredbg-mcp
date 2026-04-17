"""UI Automation wrapper using pywinauto."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pywinauto.application import Application
    from pywinauto.base_wrapper import BaseWrapper

from .errors import (
    ApplicationNotRespondingError,
    ElementNotFoundError,
    NoProcessIdError,
    UIOperationTimeoutError,
)
from .serialization import ElementInfo, serialize_element

# ── Win32 SendInput for coordinate-based clicks ─────────────────────

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_LWIN = 0x5B

_DRAG_MODIFIER_MAP = {
    "ctrl": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
    "win": VK_LWIN,
}


def _press(vk: int) -> None:
    """Press a virtual key using SendInput."""
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _fields_ = [
            ("type", wintypes.DWORD),
            ("_input", _INPUT),
        ]

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._input.ki.wVk = vk
    inp._input.ki.dwFlags = 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _release(vk: int) -> None:
    """Release a virtual key using SendInput."""
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _fields_ = [
            ("type", wintypes.DWORD),
            ("_input", _INPUT),
        ]

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._input.ki.wVk = vk
    inp._input.ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _tap(vk: int) -> None:
    """Press and release a virtual key."""
    import time

    _press(vk)
    time.sleep(0.01)
    _release(vk)


def _send_click(x: int, y: int, button: str = "left") -> None:
    """Click at screen coordinates using SendInput (modern, DPI-aware).

    Args:
        x: Absolute screen X
        y: Absolute screen Y
        button: "left" or "right"
    """
    import ctypes
    import ctypes.wintypes as wintypes
    import time

    # Move cursor
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)

    # Build INPUT structures for SendInput
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    INPUT_MOUSE = 0

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]
        _fields_ = [
            ("type", wintypes.DWORD),
            ("_input", _INPUT),
        ]

    if button == "right":
        down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP

    inputs = (INPUT * 2)()
    inputs[0].type = INPUT_MOUSE
    inputs[0]._input.mi.dwFlags = down_flag
    inputs[1].type = INPUT_MOUSE
    inputs[1]._input.mi.dwFlags = up_flag

    ctypes.windll.user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))


def _send_double_click(x: int, y: int) -> None:
    """Double-click at screen coordinates using SendInput."""
    import time

    _send_click(x, y, "left")
    time.sleep(0.05)
    _send_click(x, y, "left")

def _send_keys_via_input(keys: str) -> None:
    """Send keyboard input using Win32 SendInput API.

    Parses pywinauto-style key syntax:
    - Modifiers: ^ (Ctrl), % (Alt), + (Shift)
    - Grouped modifiers: ^(abc) holds Ctrl for a, b, c
    - Special keys in braces: {ENTER}, {TAB}, {F1}-{F12}, etc.
    - Regular characters: typed via VkKeyScanW

    This replaces pywinauto.keyboard.send_keys() which uses WM_KEYDOWN
    and fails for WPF InputBindings (e.g., Alt+Z).
    """
    import ctypes
    import ctypes.wintypes as wintypes
    import time

    user32 = ctypes.windll.user32

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    VK_MAP = {
        "ENTER": 0x0D, "RETURN": 0x0D,
        "TAB": 0x09,
        "ESC": 0x1B, "ESCAPE": 0x1B,
        "LEFT": 0x25, "RIGHT": 0x27, "UP": 0x26, "DOWN": 0x28,
        "HOME": 0x24, "END": 0x23,
        "PGUP": 0x21, "PGDN": 0x22, "PRIOR": 0x21, "NEXT": 0x22,
        "DELETE": 0x2E, "DEL": 0x2E,
        "BACKSPACE": 0x08, "BKSP": 0x08, "BS": 0x08,
        "SPACE": 0x20,
        "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
        "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
        "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    }

    MODIFIER_MAP = {
        "^": VK_CONTROL,
        "%": VK_MENU,
        "+": VK_SHIFT,
    }

    # ── KEYBDINPUT / INPUT structs for SendInput ──

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _fields_ = [
            ("type", wintypes.DWORD),
            ("_input", _INPUT),
        ]

    def _char_to_vk(ch: str) -> tuple[int, bool, bool, bool]:
        """Convert a character to (vk_code, needs_shift, needs_ctrl, needs_alt) via VkKeyScanW."""
        result = user32.VkKeyScanW(ord(ch))
        if result == -1 or (result & 0xFF) == 0xFF:
            return (0, False, False, False)
        vk = result & 0xFF
        shift = bool(result & 0x100)
        ctrl = bool(result & 0x200)
        alt = bool(result & 0x400)
        return (vk, shift, ctrl, alt)

    def _type_char(ch: str, held_modifiers: list[int]) -> None:
        """Type a single character, adding Shift/Ctrl/Alt if VkKeyScanW requires them."""
        vk, needs_shift, needs_ctrl, needs_alt = _char_to_vk(ch)
        if not vk:
            return
        extra_mods: list[int] = []
        if needs_shift and VK_SHIFT not in held_modifiers:
            extra_mods.append(VK_SHIFT)
        if needs_ctrl and VK_CONTROL not in held_modifiers:
            extra_mods.append(VK_CONTROL)
        if needs_alt and VK_MENU not in held_modifiers:
            extra_mods.append(VK_MENU)
        for m in extra_mods:
            _press(m)
            time.sleep(0.01)
        _tap(vk)
        for m in reversed(extra_mods):
            _release(m)
            time.sleep(0.01)

    i = 0
    length = len(keys)

    while i < length:
        # Collect modifier prefixes
        held_modifiers: list[int] = []
        while i < length and keys[i] in MODIFIER_MAP:
            held_modifiers.append(MODIFIER_MAP[keys[i]])
            i += 1

        if i >= length:
            break

        ch = keys[i]

        # Handle grouped modifier application: ^(abc) holds Ctrl for a, b, c
        if ch == "(" and held_modifiers:
            close = keys.find(")", i)
            if close == -1:
                raise ValueError(
                    f"Unclosed parenthesis in key sequence at position {i}"
                )
            group_chars = keys[i + 1:close]
            for mod_vk in held_modifiers:
                _press(mod_vk)
                time.sleep(0.01)
            try:
                for gch in group_chars:
                    _type_char(gch, held_modifiers)
                    time.sleep(0.02)
            finally:
                for mod_vk in reversed(held_modifiers):
                    _release(mod_vk)
                    time.sleep(0.01)
            i = close + 1
            time.sleep(0.02)
            continue

        # Press held modifiers
        for mod_vk in held_modifiers:
            _press(mod_vk)
            time.sleep(0.01)

        try:
            if ch == "{":
                # Special key in braces: {ENTER}, {F4}, etc.
                end = keys.find("}", i)
                if end == -1:
                    raise ValueError(
                        f"Unclosed brace in key sequence at position {i}: '{keys[i:]}'"
                    )
                key_name = keys[i + 1:end].upper()
                vk = VK_MAP.get(key_name)
                if vk is not None:
                    _tap(vk)
                else:
                    raise ValueError(f"Unknown special key: {{{key_name}}}")
                i = end + 1
            else:
                # Regular character
                _type_char(ch, held_modifiers)
                i += 1
        finally:
            # Release held modifiers in reverse order
            for mod_vk in reversed(held_modifiers):
                _release(mod_vk)
                time.sleep(0.01)

        time.sleep(0.02)


def _send_drag(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    speed_ms: int = 200,
    hold_modifiers: list[str] | None = None,
) -> None:
    """Drag from one screen coordinate to another using Win32 API.

    Performs: move to start → mouse down → move to end → mouse up.
    """
    import ctypes
    import time

    if speed_ms < 20:
        raise ValueError("speed_ms below drag-threshold safety floor (minimum 20)")

    if from_x == to_x and from_y == to_y:
        raise ValueError("from and to coordinates are identical (0 px distance)")

    # Mirror the bridge's sub-threshold guard (see bridge/Commands/ClickCommands.cs).
    # WPF's MinimumHorizontal/VerticalDragDistance is 4 px in each axis by
    # default; drags that never cross it do not trigger DoDragDrop. Reject
    # sub-threshold drags here too so ui_drag behaves identically regardless
    # of FlaUI-vs-pywinauto backend.
    _DRAG_THRESHOLD_PX = 4
    if abs(to_x - from_x) < _DRAG_THRESHOLD_PX and abs(to_y - from_y) < _DRAG_THRESHOLD_PX:
        raise ValueError(
            f"drag distance below WPF threshold (<{_DRAG_THRESHOLD_PX} px in each axis); "
            "adjust coordinates or use ui_click"
        )

    user32 = ctypes.windll.user32
    modifiers = hold_modifiers or []
    modifier_vks: list[int] = []
    pressed_modifier_vks: list[int] = []
    mouse_down_sent = False

    for modifier_name in modifiers:
        normalized = modifier_name.strip().lower()
        vk = _DRAG_MODIFIER_MAP.get(normalized)
        if vk is None:
            raise ValueError(
                f"Unknown modifier names: {modifier_name}. Accepted values: ctrl, shift, alt, win"
            )
        if vk not in modifier_vks:
            modifier_vks.append(vk)

    # Move to start position
    user32.SetCursorPos(from_x, from_y)
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    steps = max(10, speed_ms // 20)
    sleep_per_step = speed_ms / steps / 1000.0

    try:
        for modifier_vk in modifier_vks:
            _press(modifier_vk)
            pressed_modifier_vks.append(modifier_vk)
            time.sleep(0.01)

        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        mouse_down_sent = True

        try:
            for i in range(1, steps + 1):
                ix = from_x + (to_x - from_x) * i // steps
                iy = from_y + (to_y - from_y) * i // steps
                user32.SetCursorPos(ix, iy)
                time.sleep(sleep_per_step)
        finally:
            if mouse_down_sent:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    finally:
        for modifier_vk in reversed(pressed_modifier_vks):
            _release(modifier_vk)
            time.sleep(0.01)


logger = logging.getLogger(__name__)


class UIAutomation:
    """Async wrapper for pywinauto UI Automation."""

    def __init__(self):
        self._app: Application | None = None
        self._process_id: int | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ui_auto"
        )
        self._element_cache: dict[str, dict] = {}  # automationId -> {rect, name, control_type}

    @property
    def process_id(self) -> int | None:
        """Return the currently connected process ID."""
        return self._process_id

    async def connect(self, process_id: int) -> None:
        """
        Connect to process by PID.

        Args:
            process_id: Process ID to connect to

        Raises:
            NoProcessIdError: If process_id is invalid
            ApplicationNotRespondingError: If cannot connect to process
        """
        if not process_id or process_id <= 0:
            raise NoProcessIdError(f"Invalid process ID: {process_id}")

        def _connect():
            try:
                from pywinauto.application import Application

                # Use UIA backend for WPF support
                app = Application(backend="uia").connect(process=process_id)
                return app
            except Exception as e:
                logger.error(f"Failed to connect to process {process_id}: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot connect to process {process_id}: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            self._app = await loop.run_in_executor(self._executor, _connect)
            self._process_id = process_id
            logger.info(f"Connected to process {process_id}")
        except Exception:
            self._app = None
            self._process_id = None
            raise

    async def disconnect(self) -> None:
        """
        Disconnect from current process.

        This clears the internal references and releases pywinauto resources
        but does not kill the process.
        """
        if self._app is not None:
            logger.info(f"Disconnecting from process {self._process_id}")
            try:
                # Close UIA connection to release COM resources
                self._app = None
            except Exception as e:
                logger.warning(f"Error during disconnect cleanup: {e}")
            finally:
                self._process_id = None
                self._element_cache.clear()

    def _populate_cache(self, element_info: ElementInfo) -> None:
        """Walk an ElementInfo tree and cache elements that have an automationId."""
        if element_info.automation_id:
            self._element_cache[element_info.automation_id] = {
                "rect": dict(element_info.rectangle),
                "name": element_info.name,
                "control_type": element_info.control_type,
            }
        for child in element_info.children:
            self._populate_cache(child)

    def get_cached_rect(self, automation_id: str) -> dict | None:
        """Return cached rectangle for an automationId, or None if not cached."""
        entry = self._element_cache.get(automation_id)
        if entry is not None:
            return entry.get("rect")
        return None

    def clear_cache(self) -> None:
        """Clear the element cache."""
        self._element_cache.clear()

    async def _click_at_coords(self, x: int, y: int) -> None:
        """Click at absolute screen coordinates using Win32 SendInput."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, lambda: _send_click(x, y, button="left"),
        )

    async def _right_click_at_coords(self, x: int, y: int) -> None:
        """Right-click at absolute screen coordinates using Win32 SendInput."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, lambda: _send_click(x, y, button="right"),
        )

    async def _double_click_at_coords(self, x: int, y: int) -> None:
        """Double-click at absolute screen coordinates using Win32 SendInput."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, lambda: _send_double_click(x, y),
        )

    async def _drag_at_coords(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        speed_ms: int = 200,
        hold_modifiers: list[str] | None = None,
    ) -> None:
        """Drag from one screen coordinate to another."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor,
            lambda: _send_drag(
                from_x,
                from_y,
                to_x,
                to_y,
                speed_ms=speed_ms,
                hold_modifiers=hold_modifiers,
            ),
        )

    async def get_window_tree(
        self, max_depth: int = 3, max_children: int = 50
    ) -> ElementInfo:
        """
        Get the visual tree of the main window.

        Args:
            max_depth: Maximum depth to traverse in the tree
            max_children: Maximum number of children to serialize per element

        Returns:
            ElementInfo containing the window tree

        Raises:
            NoProcessIdError: If not connected to a process
            ApplicationNotRespondingError: If cannot access the window
            UIOperationTimeoutError: If operation takes too long
        """
        if self._app is None:
            raise NoProcessIdError("Not connected to any process")

        def _get_tree():
            try:
                # Get the top window
                window = self._app.top_window()
                return serialize_element(
                    window, max_depth=max_depth, max_children=max_children
                )
            except Exception as e:
                logger.error(f"Failed to get window tree: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot access window tree: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            # Set a reasonable timeout for UI operations
            tree = await asyncio.wait_for(
                loop.run_in_executor(self._executor, _get_tree), timeout=10.0
            )
            # Populate element cache from the tree walk
            self._element_cache = {}
            self._populate_cache(tree)
            return tree
        except asyncio.TimeoutError as e:
            logger.error("Window tree retrieval timed out")
            raise UIOperationTimeoutError(
                "Window tree retrieval timed out after 10 seconds"
            ) from e

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
    ) -> BaseWrapper:
        """
        Find element by criteria.

        At least one search criterion must be provided.

        Args:
            automation_id: AutomationId property to search for
            name: Name property to search for
            control_type: Control type to search for

        Returns:
            The found element wrapper

        Raises:
            NoProcessIdError: If not connected to a process
            ElementNotFoundError: If element cannot be found
            ValueError: If no search criteria provided
        """
        if self._app is None:
            raise NoProcessIdError("Not connected to any process")

        if not any((automation_id, name, control_type)):
            raise ValueError("At least one search criterion must be provided")

        def _find():
            # Build search criteria before try block to avoid NameError in except
            criteria = {}
            if automation_id is not None:
                criteria["auto_id"] = automation_id
            if name is not None:
                criteria["title"] = name
            if control_type is not None:
                criteria["control_type"] = control_type

            try:
                window = self._app.top_window()

                logger.debug(f"Searching for element with criteria: {criteria}")

                # Use child_window to search
                element = window.child_window(**criteria)

                # Verify element exists (this triggers the search)
                element.wait("exists", timeout=5)

                return element

            except Exception as e:
                logger.error(f"Failed to find element: {e}")
                raise ElementNotFoundError(
                    f"Element not found with criteria {criteria}: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            element = await asyncio.wait_for(
                loop.run_in_executor(self._executor, _find), timeout=10.0
            )
            return element
        except asyncio.TimeoutError as e:
            logger.error("Element search timed out")
            raise UIOperationTimeoutError(
                "Element search timed out after 10 seconds"
            ) from e

    async def get_element_info(self, element: BaseWrapper) -> ElementInfo:
        """
        Get element info.

        Args:
            element: The element to get info for

        Returns:
            ElementInfo containing the element's properties

        Raises:
            ApplicationNotRespondingError: If cannot access element
        """

        def _get_info():
            try:
                return serialize_element(element, max_depth=0, max_children=0)
            except Exception as e:
                logger.error(f"Failed to get element info: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot access element info: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            info = await asyncio.wait_for(
                loop.run_in_executor(self._executor, _get_info), timeout=5.0
            )
            return info
        except asyncio.TimeoutError as e:
            logger.error("Get element info timed out")
            raise UIOperationTimeoutError(
                "Get element info timed out after 5 seconds"
            ) from e

    async def set_focus(self, element: BaseWrapper) -> None:
        """
        Set focus to element.

        Args:
            element: The element to set focus to

        Raises:
            ApplicationNotRespondingError: If cannot set focus
            UIOperationTimeoutError: If operation times out
        """

        def _set_focus():
            try:
                element.set_focus()
                logger.debug(f"Set focus to element: {element.element_info.name}")
            except Exception as e:
                logger.error(f"Failed to set focus: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot set focus to element: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, _set_focus), timeout=5.0
            )
        except asyncio.TimeoutError as e:
            logger.error("Set focus timed out")
            raise UIOperationTimeoutError(
                "Set focus timed out after 5 seconds"
            ) from e

    async def send_keys(self, element: BaseWrapper, keys: str) -> None:
        """
        Send keys to element. Uses pywinauto keyboard syntax.

        pywinauto special keys:
        - {ENTER}, {SPACE}, {TAB}, {BACKSPACE}, {DELETE}
        - {LEFT}, {RIGHT}, {UP}, {DOWN}
        - {HOME}, {END}, {PGUP}, {PGDN}
        - {F1} through {F12}
        - Modifiers: +{KEY} (Shift), ^{KEY} (Ctrl), %{KEY} (Alt)

        Args:
            element: The element to send keys to
            keys: The keys to send (using pywinauto syntax)

        Raises:
            ApplicationNotRespondingError: If cannot send keys
            UIOperationTimeoutError: If operation times out
        """

        def _send_keys():
            try:
                element.type_keys(keys, with_spaces=True)
                logger.debug(f"Sent keys to element: {keys}")
            except Exception as e:
                logger.error(f"Failed to send keys: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot send keys to element: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, _send_keys), timeout=5.0
            )
        except asyncio.TimeoutError as e:
            logger.error("Send keys timed out")
            raise UIOperationTimeoutError(
                "Send keys timed out after 5 seconds"
            ) from e

    async def click(self, element: BaseWrapper) -> None:
        """
        Click on element center.

        Args:
            element: The element to click

        Raises:
            ApplicationNotRespondingError: If cannot click element
            UIOperationTimeoutError: If operation times out
        """

        def _click():
            try:
                element.click()
                logger.debug(f"Clicked element: {element.element_info.name}")
            except Exception as e:
                logger.error(f"Failed to click element: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot click element: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, _click), timeout=5.0
            )
        except asyncio.TimeoutError as e:
            logger.error("Click timed out")
            raise UIOperationTimeoutError("Click timed out after 5 seconds") from e

    async def send_keys_focused(self, keys: str) -> None:
        """
        Send keys to currently focused element without element search.

        This is useful after ui_set_focus when re-searching the element
        would timeout (e.g., for complex controls like DataGrid).

        Uses Win32 SendInput via _send_keys_via_input(), which works
        for WPF InputBindings (Alt+Z, etc.) unlike WM_KEYDOWN.

        Args:
            keys: The keys to send (using pywinauto syntax)

        Raises:
            NoProcessIdError: If not connected to a process
            ApplicationNotRespondingError: If cannot send keys
            UIOperationTimeoutError: If operation times out
        """
        if self._app is None:
            raise NoProcessIdError("Not connected to any process")

        def _send_keys_focused():
            try:
                _send_keys_via_input(keys)
                logger.debug(f"Sent keys to focused element via SendInput: {keys}")
            except Exception as e:
                logger.error(f"Failed to send keys to focused: {e}")
                raise ApplicationNotRespondingError(
                    f"Cannot send keys to focused element: {e}"
                ) from e

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, _send_keys_focused), timeout=5.0
            )
        except asyncio.TimeoutError as e:
            logger.error("Send keys to focused timed out")
            raise UIOperationTimeoutError(
                "Send keys to focused timed out after 5 seconds"
            ) from e

    def shutdown(self):
        """Shutdown the thread pool executor. Call this during server shutdown."""
        try:
            self._executor.shutdown(wait=True)
        except Exception as e:
            logger.warning(f"Error during executor shutdown: {e}")

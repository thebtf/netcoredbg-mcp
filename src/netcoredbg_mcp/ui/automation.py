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

logger = logging.getLogger(__name__)


class UIAutomation:
    """Async wrapper for pywinauto UI Automation."""

    def __init__(self):
        self._app: Application | None = None
        self._process_id: int | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="ui_auto"
        )

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

        Uses pywinauto keyboard.send_keys directly, which sends to the
        currently focused control.

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
                from pywinauto import keyboard

                keyboard.send_keys(keys)
                logger.debug(f"Sent keys to focused element: {keys}")
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

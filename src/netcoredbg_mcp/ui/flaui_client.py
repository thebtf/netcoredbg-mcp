"""FlaUI bridge subprocess client and UIBackend implementation.

Manages a FlaUIBridge.exe subprocess communicating via JSON-RPC over
stdin/stdout. Includes restart logic and ProcessRegistry integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Restart limits
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 60.0
CONNECT_RETRY_TIMEOUT_SECONDS = 30.0
CONNECT_CALL_TIMEOUT_SECONDS = 30.0
CONNECT_RETRY_INTERVAL_SECONDS = 0.2
WINDOW_NOT_READY_ERROR = "No window found for process"
BRIDGE_DEFAULT_CALL_TIMEOUT_SECONDS = 10.0
DRAG_PATH_POINTER_DOWN_SETTLE_MS = 100
DRAG_PATH_FINAL_DROP_SETTLE_MS = 180
DRAG_PATH_TIMEOUT_MARGIN_SECONDS = 3.0
DRAG_PATH_TIMEOUT_MULTIPLIER = 1.5
DRAG_PATH_MAX_TIMEOUT_SECONDS = 60.0
GRID_ENSURE_VISIBLE_DEFAULT_MAX_SCROLLS = 40
GRID_ENSURE_VISIBLE_MAX_SCROLLS = 250
GRID_ENSURE_VISIBLE_DEFAULT_SETTLE_MS = 80
GRID_ENSURE_VISIBLE_MAX_SETTLE_MS = 2_000
GRID_ENSURE_VISIBLE_SCAN_PASSES = 4
GRID_ENSURE_VISIBLE_SCROLL_UIA_OVERHEAD_SECONDS = 0.2
GRID_ENSURE_VISIBLE_TIMEOUT_MARGIN_SECONDS = 5.0


def _is_window_not_ready(exc: RuntimeError) -> bool:
    return WINDOW_NOT_READY_ERROR in str(exc)


def _drag_path_timeout_seconds(points: list[dict[str, Any]], speed_ms: int) -> float:
    segment_count = max(1, len(points) - 1)
    delay_ms = max(1, round(speed_ms / segment_count))
    hold_ms = sum(max(0, _int_or_zero(point.get("hold_ms"))) for point in points)
    estimated_ms = (
        DRAG_PATH_POINTER_DOWN_SETTLE_MS
        + (delay_ms * segment_count)
        + hold_ms
        + max(DRAG_PATH_FINAL_DROP_SETTLE_MS, delay_ms)
    )
    timeout = (estimated_ms / 1000.0) * DRAG_PATH_TIMEOUT_MULTIPLIER
    timeout += DRAG_PATH_TIMEOUT_MARGIN_SECONDS
    return min(
        DRAG_PATH_MAX_TIMEOUT_SECONDS,
        max(BRIDGE_DEFAULT_CALL_TIMEOUT_SECONDS, timeout),
    )


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bounded_int_or_default(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, candidate))


def _grid_ensure_visible_timeout_seconds(
    max_scrolls: int | None,
    scroll_settle_ms: int | None,
) -> float:
    bounded_scrolls = _bounded_int_or_default(
        max_scrolls,
        default=GRID_ENSURE_VISIBLE_DEFAULT_MAX_SCROLLS,
        minimum=0,
        maximum=GRID_ENSURE_VISIBLE_MAX_SCROLLS,
    )
    bounded_settle_ms = _bounded_int_or_default(
        scroll_settle_ms,
        default=GRID_ENSURE_VISIBLE_DEFAULT_SETTLE_MS,
        minimum=0,
        maximum=GRID_ENSURE_VISIBLE_MAX_SETTLE_MS,
    )
    per_scroll_seconds = (
        bounded_settle_ms / 1000.0 + GRID_ENSURE_VISIBLE_SCROLL_UIA_OVERHEAD_SECONDS
    )
    estimated_seconds = (
        GRID_ENSURE_VISIBLE_SCAN_PASSES * bounded_scrolls * per_scroll_seconds
    )
    return max(
        BRIDGE_DEFAULT_CALL_TIMEOUT_SECONDS,
        estimated_seconds + GRID_ENSURE_VISIBLE_TIMEOUT_MARGIN_SECONDS,
    )


class FlaUIBridgeClient:
    """Manages FlaUIBridge.exe subprocess lifecycle."""

    def __init__(
        self,
        bridge_path: str,
        process_registry: Any = None,
        invalidate_connection: Callable[[], None] | None = None,
    ) -> None:
        self._bridge_path = bridge_path
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._restart_times: list[float] = []
        self._process_registry = process_registry
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._invalidate_connection = invalidate_connection
        self._stop_tasks: dict[asyncio.subprocess.Process, asyncio.Task[None]] = {}

    def _mark_connection_invalid(self) -> None:
        if self._invalidate_connection is not None:
            self._invalidate_connection()

    async def start(self) -> None:
        """Start the bridge subprocess."""
        if self.is_running:
            return  # Already running

        self._process = await asyncio.create_subprocess_exec(
            self._bridge_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info("FlaUI bridge started (PID %d)", self._process.pid)

        # Register PID for reaper
        if self._process_registry and self._process.pid:
            self._process_registry.register(
                pid=self._process.pid,
                role="flaui_bridge",
            )

    async def stop(self) -> None:
        """Stop the bridge subprocess."""
        process = self._process
        if process is None:
            return

        self._mark_connection_invalid()
        existing_task = self._stop_tasks.get(process)
        if existing_task is not None and not existing_task.done():
            await asyncio.shield(existing_task)
            return

        cleanup_task = asyncio.create_task(self._stop_process(process))
        self._stop_tasks[process] = cleanup_task
        self._cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._cleanup_tasks.discard)
        cleanup_task.add_done_callback(
            lambda _task, stopped=process: self._stop_tasks.pop(stopped, None)
        )
        await asyncio.shield(cleanup_task)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        """Stop one captured bridge process and clear state after cleanup."""
        pid = process.pid

        try:
            try:
                if process.stdin and not process.stdin.is_closing():
                    # Send shutdown as notification (no id, no response expected)
                    shutdown_msg = json.dumps({"jsonrpc": "2.0", "method": "shutdown"}) + "\n"
                    process.stdin.write(shutdown_msg.encode("utf-8"))
                    await process.stdin.drain()
                    process.stdin.close()
            except Exception:
                pass

            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

            # Unregister from reaper
            if self._process_registry and pid:
                self._process_registry.unregister(pid)

            logger.info("FlaUI bridge stopped")
        finally:
            if self._process is process:
                self._process = None

    async def _stop_after_interrupted_call(self) -> None:
        """Stop the bridge without letting caller cancellation cancel cleanup."""
        self._mark_connection_invalid()
        cleanup_task = asyncio.create_task(self.stop())
        self._cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(cleanup_task)

    @property
    def is_running(self) -> bool:
        """Check if bridge subprocess is alive."""
        return (
            self._process is not None
            and self._process not in self._stop_tasks
            and self._process.returncode is None
        )

    @property
    def pid(self) -> int | None:
        """Get bridge subprocess PID."""
        if self._process is not None and self._process.returncode is None:
            return self._process.pid
        return None

    async def ensure_alive(self) -> bool:
        """Check if bridge is alive, restart if crashed.

        Returns True if bridge is running after this call.
        Returns False if restart limit exceeded.
        """
        if self.is_running:
            return True

        # Check restart budget
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if (now - t) < RESTART_WINDOW_SECONDS]
        if len(self._restart_times) >= MAX_RESTARTS:
            logger.warning(
                "FlaUI bridge restart limit reached (%d in %.0fs)",
                MAX_RESTARTS,
                RESTART_WINDOW_SECONDS,
            )
            return False

        logger.info("FlaUI bridge not running, restarting...")
        self._restart_times.append(now)
        try:
            await self.start()
            return True
        except Exception as e:
            logger.error("Failed to restart FlaUI bridge: %s", e)
            return False

    async def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """Send a JSON-RPC request and wait for response.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.
            timeout: Response timeout in seconds.

        Returns:
            Result dict from the response.

        Raises:
            RuntimeError: If bridge is not running or request fails.
            TimeoutError: If response not received within timeout.
        """
        if not await self.ensure_alive():
            raise RuntimeError("FlaUI bridge not available (restart limit exceeded)")

        async with self._lock:
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            }

            expected_id = request["id"]
            try:
                response_data = await asyncio.wait_for(
                    self._send_and_receive(request),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                logger.warning(
                    "FlaUI bridge call '%s' was cancelled; restarting bridge",
                    method,
                )
                await self._stop_after_interrupted_call()
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "FlaUI bridge call '%s' timed out after %.1fs; restarting bridge",
                    method,
                    timeout,
                )
                await self.stop()
                raise

            if not isinstance(response_data, dict):
                logger.warning(
                    "FlaUI bridge returned non-object response for '%s': %s; restarting bridge",
                    method,
                    type(response_data).__name__,
                )
                await self.stop()
                raise RuntimeError(
                    "FlaUI bridge protocol error: expected dict response, "
                    f"got {type(response_data).__name__}"
                )

            actual_id = response_data.get("id")
            if actual_id != expected_id:
                logger.warning(
                    "FlaUI bridge response id mismatch for '%s': expected %r, got %r; "
                    "restarting bridge",
                    method,
                    expected_id,
                    actual_id,
                )
                await self.stop()
                raise RuntimeError(
                    f"FlaUI bridge protocol error: response id {actual_id!r} "
                    f"did not match request id {expected_id!r}"
                )

        if "error" in response_data:
            error = response_data["error"]
            raise RuntimeError(f"FlaUI bridge error: {error.get('message', 'unknown')}")

        return response_data.get("result", {})

    async def _write_request(self, method: str, params: dict) -> None:
        """Write a JSON-RPC request to stdin."""
        assert self._process is not None and self._process.stdin is not None
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(request) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def _send_and_receive(self, request: dict) -> dict:
        """Send request and read response line."""
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._process.stdout is not None

        line = json.dumps(request) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        response_line = await self._process.stdout.readline()
        if not response_line:
            raise RuntimeError("FlaUI bridge: no response (process may have crashed)")

        return json.loads(response_line.decode("utf-8"))


class FlaUIBackend:
    """UIBackend implementation using FlaUI bridge subprocess."""

    def __init__(self, bridge_path: str, process_registry: Any = None) -> None:
        self._element_cache: dict[str, dict] = {}
        self._process_id: int | None = None
        self._client = FlaUIBridgeClient(
            bridge_path,
            process_registry,
            invalidate_connection=self._invalidate_connection,
        )

    @property
    def client(self) -> FlaUIBridgeClient:
        """Access the underlying bridge client."""
        return self._client

    @property
    def element_cache(self) -> dict[str, dict]:
        """Cached element rectangles."""
        return self._element_cache

    @property
    def process_id(self) -> int | None:
        """Connected process ID."""
        if self._process_id is not None and not self._client.is_running:
            self._invalidate_connection()
        return self._process_id

    def _invalidate_connection(self) -> None:
        self._process_id = None
        self._element_cache.clear()

    async def connect(self, pid: int, stealth: bool = False) -> None:
        """Connect to process via FlaUI bridge."""
        await self._client.ensure_alive()
        deadline = time.monotonic() + CONNECT_RETRY_TIMEOUT_SECONDS
        connect_params: dict[str, Any] = {"pid": pid}
        if stealth:
            connect_params["stealth"] = True

        while True:
            try:
                result = await self._client.call(
                    "connect",
                    connect_params,
                    timeout=CONNECT_CALL_TIMEOUT_SECONDS,
                )
            except RuntimeError as exc:
                if _is_window_not_ready(exc) and time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    logger.debug(
                        "FlaUI connect: window not ready for PID %d; retrying in %.2fs "
                        "(%.2fs remaining): %s",
                        pid,
                        CONNECT_RETRY_INTERVAL_SECONDS,
                        remaining,
                        exc,
                    )
                    await asyncio.sleep(CONNECT_RETRY_INTERVAL_SECONDS)
                    continue
                raise

            if result.get("connected"):
                self._process_id = pid
                logger.info("FlaUI backend connected to PID %d", pid)
                return

            if time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                logger.debug(
                    "FlaUI connect: bridge returned not connected for PID %d; "
                    "retrying in %.2fs (%.2fs remaining): %s",
                    pid,
                    CONNECT_RETRY_INTERVAL_SECONDS,
                    remaining,
                    result,
                )
                await asyncio.sleep(CONNECT_RETRY_INTERVAL_SECONDS)
                continue
            raise RuntimeError(f"FlaUI bridge failed to connect to PID {pid}")

    async def disconnect(self) -> None:
        """Stop the FlaUI bridge."""
        await self._client.stop()
        self._process_id = None
        self._element_cache.clear()

    async def bring_to_front(self) -> dict[str, Any]:
        """Bring the connected debuggee window to the foreground."""
        if self._process_id is None:
            raise RuntimeError("FlaUI backend is not connected to a process")

        import ctypes

        from .screenshot import get_hwnd_for_pid

        hwnd = get_hwnd_for_pid(self._process_id)
        if not hwnd:
            raise RuntimeError(f"No visible window for process {self._process_id}")

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        foreground_hwnd = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached_threads: list[tuple[int, int]] = []
        for thread_id in (foreground_thread, target_thread):
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append((current_thread, thread_id))

        try:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            activated = user32.GetForegroundWindow() == hwnd
        finally:
            for source_thread, attached_thread in reversed(attached_threads):
                user32.AttachThreadInput(source_thread, attached_thread, False)

        if activated:
            await self.connect(self._process_id, stealth=False)

        return {
            "activated": activated,
            "hwnd": hwnd,
        }

    @staticmethod
    def _build_search_params(
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, str]:
        """Build JSON-RPC params dict from search criteria."""
        params: dict[str, str] = {}
        if automation_id:
            params["automationId"] = automation_id
        if name:
            params["name"] = name
        if control_type:
            params["controlType"] = control_type
        if root_id:
            params["rootAutomationId"] = root_id
        if xpath:
            params["xpath"] = xpath
        return params

    @staticmethod
    def _build_selector_params(selector: dict[str, Any]) -> dict[str, str]:
        """Build bridge selector params from Python snake_case selector keys."""
        return FlaUIBackend._build_search_params(
            automation_id=selector.get("automation_id") or selector.get("automationId"),
            name=selector.get("name"),
            control_type=selector.get("control_type") or selector.get("controlType"),
            root_id=selector.get("root_id") or selector.get("rootAutomationId"),
            xpath=selector.get("xpath"),
        )

    async def find_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Find element via FlaUI bridge."""
        params = self._build_search_params(automation_id, name, control_type, root_id, xpath)
        return await self._client.call("find_element", params)

    async def invoke_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Invoke element via FlaUI bridge (InvokePattern, fallback to Click)."""
        params = self._build_search_params(automation_id, name, control_type, root_id, xpath)
        return await self._client.call("invoke_element", params)

    async def toggle_element(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Toggle element via FlaUI bridge (TogglePattern)."""
        params = self._build_search_params(automation_id, name, control_type, root_id, xpath)
        return await self._client.call("toggle_element", params)

    async def find_by_xpath(
        self,
        xpath: str,
        root_id: str | None = None,
    ) -> dict[str, Any]:
        """Find element by XPath via FlaUI bridge."""
        params: dict[str, str] = {"xpath": xpath}
        if root_id:
            params["rootAutomationId"] = root_id
        return await self._client.call("find_by_xpath", params)

    async def find_all_cascade(
        self,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Find all matching elements with ranked scoring via FlaUI bridge."""
        params: dict[str, Any] = {"maxResults": max_results}
        if name:
            params["name"] = name
        if control_type:
            params["controlType"] = control_type
        if root_id:
            params["rootAutomationId"] = root_id
        return await self._client.call("find_all_cascade", params)

    async def extract_text(
        self,
        automation_id: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        root_id: str | None = None,
        xpath: str | None = None,
    ) -> dict[str, Any]:
        """Extract text using multi-strategy fallback via FlaUI bridge."""
        params = self._build_search_params(automation_id, name, control_type, root_id, xpath)
        return await self._client.call("extract_text", params)

    async def click_at(self, x: int, y: int) -> None:
        """Click at coordinates via FlaUI bridge."""
        await self._client.call("click", {"x": x, "y": y})

    async def right_click_at(self, x: int, y: int) -> None:
        """Right-click via FlaUI bridge."""
        await self._client.call("right_click", {"x": x, "y": y})

    async def double_click_at(self, x: int, y: int) -> None:
        """Double-click via FlaUI bridge."""
        await self._client.call("double_click", {"x": x, "y": y})

    async def drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        speed_ms: int = 200,
        hold_modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Drag via FlaUI bridge."""
        result = await self._client.call(
            "drag",
            {
                "x1": from_x,
                "y1": from_y,
                "x2": to_x,
                "y2": to_y,
                "speed_ms": speed_ms,
                "hold_modifiers": hold_modifiers or [],
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"drag: bridge returned a non-dict response ({type(result).__name__}): {result!r}"
            )
        return result

    async def drag_path(
        self,
        points: list[dict[str, Any]],
        speed_ms: int = 200,
        hold_modifiers: list[str] | None = None,
        cancel_key: str | None = None,
    ) -> dict[str, Any]:
        """Drag through a path of screen points via the FlaUI bridge."""
        payload: dict[str, Any] = {
            "points": points,
            "speed_ms": speed_ms,
            "hold_modifiers": hold_modifiers or [],
        }
        if cancel_key is not None:
            payload["cancel_key"] = cancel_key
        result = await self._client.call(
            "drag_path",
            payload,
            timeout=_drag_path_timeout_seconds(points, speed_ms),
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"drag_path: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def send_keys(self, keys: str) -> None:
        """Send keys via FlaUI bridge."""
        await self._client.call("send_keys", {"keys": keys})

    async def send_system_event(self, event: str, mode: str = "toggle") -> dict[str, Any]:
        """Send a supported system event via FlaUI bridge."""
        result = await self._client.call(
            "send_system_event",
            {"event": event, "mode": mode},
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"send_system_event: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def hold_modifiers(self, modifiers: list[str]) -> dict[str, Any]:
        """Hold modifiers via FlaUI bridge."""
        result = await self._client.call("hold_modifiers", {"modifiers": modifiers})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"hold_modifiers: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def release_modifiers(self, modifiers: list[str] | str) -> dict[str, Any]:
        """Release modifiers via FlaUI bridge."""
        result = await self._client.call("release_modifiers", {"modifiers": modifiers})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"release_modifiers: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def get_held_modifiers(self) -> dict[str, Any]:
        """Inspect held modifiers via FlaUI bridge."""
        result = await self._client.call("get_held_modifiers", {})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"get_held_modifiers: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def scoped_key_sequence(
        self,
        selector: dict[str, Any],
        modifiers: list[str],
        keys: list[str],
    ) -> dict[str, Any]:
        """Run a scoped held-modifier key sequence via FlaUI bridge."""
        result = await self._client.call(
            "scoped_key_sequence",
            {
                "selector": self._build_selector_params(selector),
                "modifiers": modifiers,
                "keys": keys,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"scoped_key_sequence: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def grid_visible_rows(self, selector: dict[str, Any]) -> dict[str, Any]:
        """Read visible DataGrid rows via FlaUI bridge."""
        return await self._call_grid("grid_visible_rows", selector)

    async def grid_selected_rows(
        self,
        selector: dict[str, Any],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read selected DataGrid rows via FlaUI bridge."""
        return await self._call_grid("grid_selected_rows", selector, columns=columns or [])

    async def grid_snapshot(
        self,
        selector: dict[str, Any],
        rows: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read visible DataGrid rows with cell evidence via FlaUI bridge."""
        return await self._call_grid(
            "grid_snapshot",
            selector,
            rows=rows or {},
            columns=columns or [],
        )

    async def grid_assert_rows(
        self,
        selector: dict[str, Any],
        rows: list[dict[str, Any]],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Assert DataGrid rows with cell evidence via FlaUI bridge."""
        return await self._call_grid(
            "grid_assert_rows",
            selector,
            rows=rows,
            columns=columns or [],
        )

    async def grid_select_range(
        self,
        selector: dict[str, Any],
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        """Select a DataGrid row range via FlaUI bridge."""
        return await self._call_grid(
            "grid_select_range",
            selector,
            start_index=start_index,
            end_index=end_index,
        )

    async def grid_click_row(
        self,
        selector: dict[str, Any],
        row_index: int,
        column: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Click a visible DataGrid row via FlaUI bridge."""
        return await self._call_grid(
            "grid_click_row",
            selector,
            row_index=row_index,
            column=column,
            columns=columns or [],
        )

    async def grid_ensure_visible(
        self,
        selector: dict[str, Any],
        *,
        row_key: str | None = None,
        row_index: int | None = None,
        identity: dict[str, Any] | None = None,
        rows: dict[str, Any] | None = None,
        columns: list[str] | None = None,
        max_scrolls: int | None = None,
        scroll_settle_ms: int | None = None,
    ) -> dict[str, Any]:
        """Make a DataGrid row visible via FlaUI bridge-owned support."""
        payload: dict[str, Any] = {
            "identity": dict(identity or {}),
            "rows": dict(rows or {}),
            "columns": list(columns or []),
        }
        if row_key is not None:
            payload["row_key"] = row_key
        if row_index is not None:
            payload["row_index"] = row_index
        if max_scrolls is not None:
            payload["max_scrolls"] = max_scrolls
        if scroll_settle_ms is not None:
            payload["scroll_settle_ms"] = scroll_settle_ms
        return await self._call_grid(
            "grid_ensure_visible",
            selector,
            call_timeout=_grid_ensure_visible_timeout_seconds(
                max_scrolls,
                scroll_settle_ms,
            ),
            **payload,
        )

    async def grid_assert_range(
        self,
        selector: dict[str, Any],
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        """Assert a DataGrid row range via FlaUI bridge."""
        return await self._call_grid(
            "grid_assert_range",
            selector,
            start_index=start_index,
            end_index=end_index,
        )

    async def query_ui(
        self,
        selector: dict[str, Any],
        fields: list[str],
        max_results: int = 20,
    ) -> dict[str, Any]:
        """Read field-limited UI evidence via FlaUI bridge."""
        result = await self._client.call(
            "ui_query",
            {
                "selector": self._build_selector_params(selector),
                "fields": list(fields),
                "maxResults": max_results,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"ui_query: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def list_invoke_item(
        self,
        selector: dict[str, Any],
        item: dict[str, Any],
        invoke: str = "default",
    ) -> dict[str, Any]:
        """Invoke a ListBox/ListView item via FlaUI bridge."""
        result = await self._client.call(
            "list_invoke_item",
            {
                "selector": self._build_selector_params(selector),
                "item": self._build_selector_params(item),
                "itemIndex": item.get("index"),
                "invoke": invoke,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"list_invoke_item: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def list_toggle_item_child(
        self,
        selector: dict[str, Any],
        item: dict[str, Any],
        child: dict[str, Any],
        target_state: str | None = None,
    ) -> dict[str, Any]:
        """Toggle a child control scoped to a resolved list item."""
        result = await self._client.call(
            "list_toggle_item_child",
            {
                "selector": self._build_selector_params(selector),
                "item": self._build_selector_params(item),
                "itemIndex": item.get("index"),
                "child": self._build_selector_params(child),
                "targetState": target_state,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"list_toggle_item_child: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def assert_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        """Assert focus is on or inside a selector via FlaUI bridge."""
        result = await self._client.call(
            "assert_focus",
            {"selector": self._build_selector_params(selector)},
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"assert_focus: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def _call_grid(
        self,
        method: str,
        selector: dict[str, Any],
        *,
        call_timeout: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        call_kwargs: dict[str, Any] = {}
        if call_timeout is not None:
            call_kwargs["timeout"] = call_timeout
        result = await self._client.call(
            method,
            {"selector": self._build_selector_params(selector), **extra},
            **call_kwargs,
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"{method}: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def multi_select(self, container_id: str, indices: list[int]) -> int:
        """Multi-select via FlaUI bridge."""
        result = await self._client.call(
            "multi_select",
            {
                "automationId": container_id,
                "indices": indices,
            },
        )
        return len(result.get("indices", []))

    async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> Any:
        """Get tree via FlaUI bridge and update cache.

        Bridge returns the multi-window shape `{windows: [...], count, primary}`
        covering every top-level window of the target process, which is how
        modal dialogs (siblings of the app's main window) become reachable.
        Caching walks every window so ui_click / ui_send_keys can resolve
        elements inside any of them.
        """
        result = await self._client.call(
            "get_tree",
            {
                "maxDepth": max_depth,
                "maxChildren": max_children,
            },
        )
        self._element_cache.clear()
        for window_tree in self._iter_windows(result):
            self._cache_from_tree(window_tree)
        return result

    @staticmethod
    def _iter_windows(tree_result: Any) -> list[dict]:
        """Yield window subtrees regardless of whether the bridge returned the
        multi-window envelope or a legacy single-tree response."""
        if not isinstance(tree_result, dict):
            return []
        windows = tree_result.get("windows")
        if isinstance(windows, list):
            return [w for w in windows if isinstance(w, dict)]
        # Legacy single-tree shape — treat the root node as the only window.
        if "found" in tree_result or "automationId" in tree_result:
            return [tree_result]
        return []

    def _cache_from_tree(self, node: dict) -> None:
        """Recursively cache element rects from tree response."""
        aid = node.get("automationId")
        rect = node.get("rect")
        if aid and rect:
            self._element_cache[aid] = {
                "rect": {
                    "left": rect.get("x", 0),
                    "top": rect.get("y", 0),
                    "right": rect.get("x", 0) + rect.get("width", 0),
                    "bottom": rect.get("y", 0) + rect.get("height", 0),
                },
                "name": node.get("name"),
                "control_type": node.get("controlType"),
            }
        for child in node.get("children", []):
            if isinstance(child, dict) and not child.get("truncated"):
                self._cache_from_tree(child)

    async def switch_window(
        self,
        name: str | None = None,
        automation_id: str | None = None,
    ) -> dict:
        """Retarget the bridge at a different top-level window of the same process.

        Matching priority inside the bridge: automationId → name. This is how
        callers enter a modal dialog (sibling top-level window) after
        ``get_window_tree`` surfaces it.
        """
        if not name and not automation_id:
            raise ValueError("switch_window requires at least one of: name, automation_id")
        params: dict[str, str] = {}
        if automation_id:
            params["automationId"] = automation_id
        if name:
            params["name"] = name
        result = await self._client.call("set_active_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"set_active_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    # -- v0.11.1: Pattern expansion methods --

    async def close_window(self, window_title: str | None = None) -> dict[str, Any]:
        """Close a top-level window via WindowPattern."""
        params: dict[str, Any] = {}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("close_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"close_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def maximize_window(self, window_title: str | None = None) -> dict[str, Any]:
        """Maximize a top-level window via WindowPattern."""
        params: dict[str, Any] = {}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("maximize_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"maximize_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def minimize_window(self, window_title: str | None = None) -> dict[str, Any]:
        """Minimize a top-level window via WindowPattern."""
        params: dict[str, Any] = {}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("minimize_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"minimize_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def restore_window(self, window_title: str | None = None) -> dict[str, Any]:
        """Restore a top-level window to normal state via WindowPattern."""
        params: dict[str, Any] = {}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("restore_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"restore_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def move_window(self, x: int, y: int, window_title: str | None = None) -> dict[str, Any]:
        """Move a window to (x, y) via TransformPattern."""
        params: dict[str, Any] = {"x": x, "y": y}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("move_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"move_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def resize_window(
        self,
        width: int,
        height: int,
        window_title: str | None = None,
    ) -> dict[str, Any]:
        """Resize a window to width x height via TransformPattern."""
        params: dict[str, Any] = {"width": width, "height": height}
        if window_title:
            params["window_title"] = window_title
        result = await self._client.call("resize_window", params)
        if not isinstance(result, dict):
            raise RuntimeError(
                f"resize_window: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def expand(self, automation_id: str) -> dict[str, Any]:
        """Expand a TreeView node or ComboBox dropdown via ExpandCollapsePattern."""
        result = await self._client.call("expand", {"automationId": automation_id})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"expand: bridge returned a non-dict response ({type(result).__name__}): {result!r}"
            )
        return result

    async def collapse(self, automation_id: str) -> dict[str, Any]:
        """Collapse a TreeView node or ComboBox dropdown via ExpandCollapsePattern."""
        result = await self._client.call("collapse", {"automationId": automation_id})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"collapse: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def set_value(self, automation_id: str, value: float) -> dict[str, Any]:
        """Set a slider/spinner value via RangeValuePattern with range validation."""
        result = await self._client.call(
            "range_set_value",
            {"automationId": automation_id, "value": value},
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"set_value: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def clipboard_read(self) -> dict[str, Any]:
        """Read text from the clipboard (executed on STA thread in bridge)."""
        result = await self._client.call("clipboard_read", {})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"clipboard_read: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def clipboard_write(self, text: str) -> dict[str, Any]:
        """Write text to the clipboard (executed on STA thread in bridge)."""
        result = await self._client.call("clipboard_write", {"text": text})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"clipboard_write: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

    async def realize_virtualized_item(
        self,
        container_automation_id: str,
        prop_name: str,
        value: str,
    ) -> dict[str, Any]:
        """Realize a virtualized list/grid item so it enters the visual tree.

        Uses ItemContainerPattern.FindItemByProperty + VirtualizedItemPattern.Realize.
        Re-realizing an already-realized item is safe (idempotent).
        """
        result = await self._client.call(
            "realize_virtualized_item",
            {
                "container_automation_id": container_automation_id,
                "property": prop_name,
                "value": value,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                f"realize_virtualized_item: bridge returned a non-dict response "
                f"({type(result).__name__}): {result!r}"
            )
        return result

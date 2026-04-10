"""FlaUI bridge subprocess client and UIBackend implementation.

Manages a FlaUIBridge.exe subprocess communicating via JSON-RPC over
stdin/stdout. Includes restart logic and ProcessRegistry integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Restart limits
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 60.0


class FlaUIBridgeClient:
    """Manages FlaUIBridge.exe subprocess lifecycle."""

    def __init__(self, bridge_path: str, process_registry: Any = None) -> None:
        self._bridge_path = bridge_path
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._restart_times: list[float] = []
        self._process_registry = process_registry
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the bridge subprocess."""
        if self._process is not None and self._process.returncode is None:
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
        if self._process is None:
            return

        pid = self._process.pid

        try:
            if self._process.stdin and not self._process.stdin.is_closing():
                # Send shutdown as notification (no id, no response expected)
                shutdown_msg = json.dumps({"jsonrpc": "2.0", "method": "shutdown"}) + "\n"
                self._process.stdin.write(shutdown_msg.encode("utf-8"))
                await self._process.stdin.drain()
                self._process.stdin.close()
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

        # Unregister from reaper
        if self._process_registry and pid:
            self._process_registry.unregister(pid)

        logger.info("FlaUI bridge stopped")
        self._process = None

    @property
    def is_running(self) -> bool:
        """Check if bridge subprocess is alive."""
        return self._process is not None and self._process.returncode is None

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
                MAX_RESTARTS, RESTART_WINDOW_SECONDS,
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

            response_data = await asyncio.wait_for(
                self._send_and_receive(request),
                timeout=timeout,
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
        self._client = FlaUIBridgeClient(bridge_path, process_registry)
        self._element_cache: dict[str, dict] = {}
        self._process_id: int | None = None

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
        return self._process_id

    async def connect(self, pid: int) -> None:
        """Connect to process via FlaUI bridge."""
        await self._client.ensure_alive()
        result = await self._client.call("connect", {"pid": pid})
        if result.get("connected"):
            self._process_id = pid
            logger.info("FlaUI backend connected to PID %d", pid)
        else:
            raise RuntimeError(f"FlaUI bridge failed to connect to PID {pid}")

    async def disconnect(self) -> None:
        """Stop the FlaUI bridge."""
        await self._client.stop()
        self._process_id = None
        self._element_cache.clear()

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

    async def drag(self, from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Drag via FlaUI bridge."""
        await self._client.call("drag", {
            "fromX": from_x, "fromY": from_y,
            "toX": to_x, "toY": to_y,
        })

    async def send_keys(self, keys: str) -> None:
        """Send keys via FlaUI bridge."""
        await self._client.call("send_keys", {"keys": keys})

    async def multi_select(self, container_id: str, indices: list[int]) -> int:
        """Multi-select via FlaUI bridge."""
        result = await self._client.call("multi_select", {
            "automationId": container_id,
            "indices": indices,
        })
        return len(result.get("indices", []))

    async def get_window_tree(self, max_depth: int = 3, max_children: int = 50) -> Any:
        """Get tree via FlaUI bridge and update cache.

        Bridge returns the multi-window shape `{windows: [...], count, primary}`
        covering every top-level window of the target process, which is how
        modal dialogs (siblings of the app's main window) become reachable.
        Caching walks every window so ui_click / ui_send_keys can resolve
        elements inside any of them.
        """
        result = await self._client.call("get_tree", {
            "maxDepth": max_depth,
            "maxChildren": max_children,
        })
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
        return result if isinstance(result, dict) else {"switched": False}

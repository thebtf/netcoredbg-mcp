"""Tests for UI backend abstraction layer."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.ui.backend import create_backend, find_flaui_bridge

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestFindFlauiBridge:
    """Tests for FlaUI bridge binary discovery (delegates to setup.bridge)."""

    def test_delegates_to_setup_bridge(self, tmp_path):
        """find_flaui_bridge delegates to setup.bridge.find_or_build_bridge."""
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")
        with patch(
            "netcoredbg_mcp.setup.bridge.find_or_build_bridge",
            return_value=str(bridge),
        ):
            result = find_flaui_bridge()
            assert result == str(bridge)

    def test_returns_none_when_not_found(self):
        """Returns None when setup.bridge returns None."""
        with patch(
            "netcoredbg_mcp.setup.bridge.find_or_build_bridge",
            return_value=None,
        ):
            result = find_flaui_bridge()
            assert result is None


class TestCreateBackend:
    """Tests for backend factory."""

    def test_creates_pywinauto_when_no_flaui(self):
        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=None):
            backend = create_backend()
            from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

            assert isinstance(backend, PywinautoBackend)

    def test_creates_flaui_when_found(self, tmp_path):
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")

        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=str(bridge)):
            backend = create_backend()
            from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

            assert isinstance(backend, FlaUIBackend)

    def test_passes_process_registry(self, tmp_path):
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")
        mock_registry = MagicMock()

        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=str(bridge)):
            backend = create_backend(process_registry=mock_registry)
            from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

            assert isinstance(backend, FlaUIBackend)
            assert backend.client._process_registry is mock_registry


class TestPathAwareDragBackendContract:
    def test_backend_protocol_declares_path_aware_drag(self):
        backend_source = (PROJECT_ROOT / "src" / "netcoredbg_mcp" / "ui" / "backend.py").read_text(
            encoding="utf-8"
        )

        assert "async def drag_path(" in backend_source
        assert "points: list[dict[str, Any]]" in backend_source
        assert "hold_modifiers: list[str] | None = None" in backend_source

    def test_flaui_backend_routes_path_aware_drag_to_bridge(self):
        flaui_source = (
            PROJECT_ROOT / "src" / "netcoredbg_mcp" / "ui" / "flaui_client.py"
        ).read_text(encoding="utf-8")

        assert "async def drag_path(" in flaui_source
        assert '"drag_path"' in flaui_source
        assert '"points": points' in flaui_source
        assert '"hold_modifiers": hold_modifiers or []' in flaui_source

    def test_pywinauto_backend_blocks_path_aware_release_critical_drag(self):
        pywinauto_source = (
            PROJECT_ROOT / "src" / "netcoredbg_mcp" / "ui" / "pywinauto_backend.py"
        ).read_text(encoding="utf-8")

        assert "async def drag_path(" in pywinauto_source
        assert '"status": "BLOCKED"' in pywinauto_source
        assert '"requested"' in pywinauto_source
        assert '"accepted"' in pywinauto_source
        assert '"next_step"' in pywinauto_source


class TestFlaUIBackendConnect:
    @pytest.mark.asyncio
    async def test_retries_until_gui_window_is_ready(self):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = MagicMock()
        backend._client.ensure_alive = AsyncMock(return_value=True)
        backend._client.call = AsyncMock(
            side_effect=[
                RuntimeError(
                    "FlaUI bridge error: Internal error: "
                    "No window found for process 42: no usable top-level window yet"
                ),
                {"connected": True, "title": "WPF Smoke"},
            ]
        )
        backend._element_cache = {}
        backend._process_id = None

        with patch("netcoredbg_mcp.ui.flaui_client.CONNECT_RETRY_INTERVAL_SECONDS", 0):
            await backend.connect(42)

        assert backend.process_id == 42
        assert backend._client.call.await_count == 2

    @pytest.mark.asyncio
    async def test_connect_uses_bounded_cold_uia_timeout(self):
        from netcoredbg_mcp.ui.flaui_client import (
            CONNECT_CALL_TIMEOUT_SECONDS,
            FlaUIBackend,
        )

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = MagicMock()
        backend._client.ensure_alive = AsyncMock(return_value=True)
        backend._client.call = AsyncMock(return_value={"connected": True, "title": "WPF Smoke"})
        backend._element_cache = {}
        backend._process_id = None

        await backend.connect(42)

        backend._client.call.assert_awaited_once_with(
            "connect",
            {"pid": 42},
            timeout=CONNECT_CALL_TIMEOUT_SECONDS,
        )

    def test_bridge_connect_selects_usable_primary_window(self):
        command = (PROJECT_ROOT / "bridge" / "Commands" / "ElementCommands.cs").read_text(
            encoding="utf-8"
        )

        assert "SelectPrimaryWindow(windows)" in command
        assert "PrimaryWindowScore(window)" in command
        assert "window.BoundingRectangle" in command
        assert "rect.Width <= 0 || rect.Height <= 0" in command
        assert "SafeIsOffscreen(window)" in command
        assert "catch { return true; }" in command
        assert "OrderByDescending(candidate => candidate.Score)" in command
        assert "no usable top-level window yet" in command

    @pytest.mark.asyncio
    async def test_logs_connect_retries(self, caplog):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = MagicMock()
        backend._client.ensure_alive = AsyncMock(return_value=True)
        backend._client.call = AsyncMock(
            side_effect=[
                {"connected": False, "title": ""},
                {"connected": True, "title": "WPF Smoke"},
            ]
        )
        backend._element_cache = {}
        backend._process_id = None

        with (
            patch("netcoredbg_mcp.ui.flaui_client.CONNECT_RETRY_INTERVAL_SECONDS", 0),
            caplog.at_level("DEBUG", logger="netcoredbg_mcp.ui.flaui_client"),
        ):
            await backend.connect(42)

        assert backend.process_id == 42
        assert "bridge returned not connected for PID 42" in caplog.text

    @pytest.mark.asyncio
    async def test_connect_does_not_retry_non_readiness_errors(self):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = MagicMock()
        backend._client.ensure_alive = AsyncMock(return_value=True)
        backend._client.call = AsyncMock(
            side_effect=RuntimeError("FlaUI bridge error: access denied")
        )
        backend._element_cache = {}
        backend._process_id = None

        with pytest.raises(RuntimeError, match="access denied"):
            await backend.connect(42)

        assert backend._client.call.await_count == 1


class TestFlaUIBridgeClient:
    @pytest.mark.asyncio
    async def test_stop_preserves_process_handle_until_cleanup_finishes(self):
        import asyncio

        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        class FakeStdin:
            def __init__(self) -> None:
                self.closed = False

            def is_closing(self) -> bool:
                return self.closed

            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class FakeProcess:
            pid = 123

            def __init__(self) -> None:
                self.stdin = FakeStdin()
                self.returncode: int | None = None
                self.wait_started = asyncio.Event()
                self.wait_release = asyncio.Event()

            async def wait(self) -> None:
                self.wait_started.set()
                await self.wait_release.wait()
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9
                self.wait_release.set()

        process = FakeProcess()
        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        client._process = process  # type: ignore[assignment]

        caller = asyncio.create_task(client.stop())
        await process.wait_started.wait()

        assert client._process is process
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert client._process is process
        assert client._stop_task is not None

        process.wait_release.set()
        await client._stop_task

        assert client._process is None

    @pytest.mark.asyncio
    async def test_call_restarts_bridge_after_cancelled_request(self, monkeypatch):
        import asyncio

        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        client.ensure_alive = AsyncMock(return_value=True)
        client.stop = AsyncMock()
        shielded_cleanup = []

        def shield_probe(awaitable):
            shielded_cleanup.append(awaitable)
            return awaitable

        monkeypatch.setattr(asyncio, "shield", shield_probe)

        async def cancelled_response(_request):
            raise asyncio.CancelledError()

        client._send_and_receive = cancelled_response

        with pytest.raises(asyncio.CancelledError):
            await client.call("get_tree", {"maxDepth": 1})

        client.stop.assert_awaited_once()
        assert len(shielded_cleanup) == 1

    def test_process_id_invalidates_when_bridge_is_stopped(self):
        from types import SimpleNamespace

        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = SimpleNamespace(is_running=False)
        backend._process_id = 42
        backend._element_cache = {"stale": {"runtimeId": "old-bridge"}}

        assert backend.process_id is None
        assert backend._process_id is None
        assert backend._element_cache == {}

    @pytest.mark.asyncio
    async def test_call_restarts_bridge_after_timeout(self):
        import asyncio

        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        client.ensure_alive = AsyncMock(return_value=True)
        client.stop = AsyncMock()

        async def slow_response(_request):
            await asyncio.sleep(1.0)
            return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

        client._send_and_receive = slow_response

        with pytest.raises(asyncio.TimeoutError):
            await client.call("connect", {"pid": 42}, timeout=0.01)

        client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_restarts_bridge_after_response_id_mismatch(self):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        client.ensure_alive = AsyncMock(return_value=True)
        client.stop = AsyncMock()
        client._send_and_receive = AsyncMock(
            return_value={"jsonrpc": "2.0", "id": 99, "result": {"ok": True}}
        )

        with pytest.raises(RuntimeError, match="response id 99 did not match request id 1"):
            await client.call("grid_snapshot", {"selector": {"automationId": "dataGrid"}})

        client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_restarts_bridge_after_non_object_response(self):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        client.ensure_alive = AsyncMock(return_value=True)
        client.stop = AsyncMock()
        client._send_and_receive = AsyncMock(return_value=["not", "a", "response"])

        with pytest.raises(RuntimeError, match="expected dict response, got list"):
            await client.call("grid_snapshot", {"selector": {"automationId": "dataGrid"}})

        client.stop.assert_awaited_once()

    def test_bridge_errors_preserve_json_rpc_request_id(self):
        program = (PROJECT_ROOT / "bridge" / "Program.cs").read_text(encoding="utf-8")

        assert "JsonNode? id = null;" in program
        assert "return CreateErrorResponse(id, -32603" in program


class TestPywinautoBackend:
    """Tests for PywinautoBackend wrapper."""

    def test_element_cache_property(self):
        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=None):
            backend = create_backend()
            assert isinstance(backend.element_cache, dict)
            assert backend.process_id is None

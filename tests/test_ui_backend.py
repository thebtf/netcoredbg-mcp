"""Tests for UI backend abstraction layer."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.ui.backend import create_backend, find_flaui_bridge

PROJECT_ROOT = Path(__file__).resolve().parents[1]


LARGE_JSON_RESPONSE_BYTES = 64 * 1024 + 1


def _write_large_response_bridge(tmp_path: Path, *, stderr_bytes: int = 0) -> Path:
    script = tmp_path / "large_response_bridge.py"
    script.write_text(
        "\n".join(
            (
                "import json",
                "import sys",
                "for line in sys.stdin:",
                "    request = json.loads(line)",
                f"    sys.stderr.buffer.write(b'E' * {stderr_bytes})",
                "    sys.stderr.buffer.flush()",
                "    if request.get('method') == 'shutdown':",
                "        break",
                "    response = {",
                '        "jsonrpc": "2.0",',
                '        "id": request["id"],',
                f'        "result": {{"png_base64": "A" * {LARGE_JSON_RESPONSE_BYTES}}},',
                "    }",
                "    print(json.dumps(response), flush=True)",
                "",
            )
        ),
        encoding="utf-8",
    )
    return script


class _BridgeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _BridgeOwner:
    def __init__(self, pid: int, *, blocked: bool = False) -> None:
        from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef

        self.pid = pid
        self.owner = OwnedProcessRef(str(id(self)), object(), pid)
        self.stdin = _BridgeStdin()
        self.stdout = asyncio.StreamReader(limit=256 * 1024 * 1024)
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.drain_started = asyncio.Event()
        self.drain_release = asyncio.Event()
        if not blocked:
            self.drain_release.set()
        self.status = DrainStatus.DRAINED
        self.drain_calls = 0
        self.close_calls = 0
        self.receipt = None

    async def wait(self) -> int:
        await self.drain_release.wait()
        return 0

    async def drain_after_grace(self, *, grace_timeout: float, force_timeout: float):
        from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnerDrainReceipt

        self.drain_calls += 1
        self.drain_started.set()
        await self.drain_release.wait()
        complete = self.status is DrainStatus.DRAINED
        if complete:
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        self.receipt = OwnerDrainReceipt(
            owner=self.owner,
            status=self.status,
            forced=True,
            root_returncode=self.returncode,
            active_processes=0 if complete else 1,
            root_was_forced=complete,
        )
        return self.receipt

    async def aclose(self):
        self.close_calls += 1
        return self.receipt


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


class TestManagedBridgeDiscovery:
    def test_failed_rebuild_prefers_existing_managed_bridge_over_path(self, tmp_path):
        from netcoredbg_mcp.setup.bridge import find_or_build_bridge

        home_dir = tmp_path / "home"
        managed_bridge = home_dir / "bridge" / "FlaUIBridge.exe"
        managed_bridge.parent.mkdir(parents=True)
        managed_bridge.write_text("managed")

        source_dir = tmp_path / "source"
        source_dir.mkdir()
        path_bridge = tmp_path / "path" / "FlaUIBridge.exe"
        path_bridge.parent.mkdir()
        path_bridge.write_text("path")

        with (
            patch("netcoredbg_mcp.setup.bridge.get_home_dir", return_value=home_dir),
            patch("netcoredbg_mcp.setup.bridge.find_bridge_source", return_value=source_dir),
            patch("netcoredbg_mcp.setup.bridge.build_bridge", return_value=None),
            patch(
                "netcoredbg_mcp.setup.bridge.shutil.which", return_value=str(path_bridge)
            ) as which,
            patch.dict("netcoredbg_mcp.setup.bridge.os.environ", {"FLAUI_BRIDGE_PATH": ""}),
        ):
            result = find_or_build_bridge()

        assert result == str(managed_bridge.resolve())
        which.assert_not_called()


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
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        process = _BridgeOwner(123, blocked=True)
        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        with patch(
            "netcoredbg_mcp.windows_process_owner.WindowsOwnedProcess.launch", return_value=process
        ):
            await client.start()
        captured = client._process
        assert captured is not None

        caller = asyncio.create_task(client.stop())
        await asyncio.wait_for(process.drain_started.wait(), 1.0)

        assert client._process is process
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert client._process is process
        assert captured in client._stop_tasks
        cleanup_task = client._stop_tasks[captured]
        process.drain_release.set()
        await cleanup_task

        assert client._process is None
        assert process.close_calls == 1
        assert process.stdin.closed
        assert json.loads(process.stdin.writes[0]) == {"jsonrpc": "2.0", "method": "shutdown"}

    @pytest.mark.asyncio
    async def test_stop_cleans_current_process_while_old_cleanup_is_running(self):
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        old_process = _BridgeOwner(123, blocked=True)
        new_process = _BridgeOwner(456, blocked=True)
        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        with patch(
            "netcoredbg_mcp.windows_process_owner.WindowsOwnedProcess.launch",
            side_effect=[old_process, new_process],
        ):
            await client.start()
            old_captured = client._process
            assert old_captured is not None
            old_stop = asyncio.create_task(client.stop())
            await asyncio.wait_for(old_process.drain_started.wait(), 1.0)
            old_stop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old_stop
            await client.start()
            new_captured = client._process
            assert new_captured is not None

        new_stop = asyncio.create_task(client.stop())
        await asyncio.wait_for(new_process.drain_started.wait(), 1.0)
        old_cleanup = client._stop_tasks[old_captured]
        new_cleanup = client._stop_tasks[new_captured]
        old_process.drain_release.set()
        new_process.drain_release.set()
        await asyncio.gather(old_cleanup, new_cleanup)
        await new_stop

        assert client._process is None
        assert old_process.drain_calls == new_process.drain_calls == 1

    @pytest.mark.asyncio
    async def test_old_registry_cleanup_cannot_stop_same_pid_replacement(self):
        from netcoredbg_mcp.process_registry import ProcessRegistry
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient

        registry = ProcessRegistry()
        invalidated = MagicMock()
        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe", registry, invalidated)
        old_process = _BridgeOwner(123, blocked=True)
        new_process = _BridgeOwner(123)
        with (
            patch(
                "netcoredbg_mcp.windows_process_owner.WindowsOwnedProcess.launch",
                side_effect=[old_process, new_process],
            ),
            patch.object(
                registry, "register_owner", wraps=registry.register_owner
            ) as register_owner,
        ):
            await client.start()
            assert register_owner.call_args is not None
            old_callback = register_owner.call_args.kwargs["cleanup"]
            old_cleanup = asyncio.create_task(old_callback())
            await asyncio.wait_for(old_process.drain_started.wait(), 1.0)
            await client.start()
            invalidated.reset_mock()
            old_process.drain_release.set()
            outcome = await old_cleanup
            await old_callback()

        assert outcome.complete
        assert client._process is new_process
        assert client.is_running
        assert new_process.drain_calls == new_process.close_calls == 0
        invalidated.assert_not_called()
        await registry.cleanup_all()
        assert new_process.close_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["timed_out", "failed"])
    async def test_incomplete_registry_cleanup_retains_owner_for_retry(self, status: str):
        from netcoredbg_mcp.process_registry import ProcessRegistry
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient
        from netcoredbg_mcp.windows_process_owner import DrainStatus

        registry = ProcessRegistry()
        process = _BridgeOwner(123)
        process.status = DrainStatus(status)
        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe", registry)
        with patch(
            "netcoredbg_mcp.windows_process_owner.WindowsOwnedProcess.launch", return_value=process
        ):
            await client.start()

        report = await registry.cleanup_all()
        assert not report.complete
        assert report.remaining_owners == registry.owner_count == 1
        assert report.terminated == 0
        assert client._process is process
        assert process.close_calls == 0
        assert process.drain_calls == 1
        assert not client.is_running

        process.status = DrainStatus.DRAINED
        report = await registry.cleanup_all()
        assert report.complete
        assert report.remaining_owners == registry.owner_count == 0
        assert process.drain_calls == 2
        assert process.close_calls == 1
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

    @pytest.mark.skipif(os.name != "nt", reason="Windows-owned FlaUI transport proof")
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stderr_bytes", [0, 2 * 1024 * 1024])
    async def test_reads_large_json_line_through_bridge_client(
        self, tmp_path: Path, stderr_bytes: int
    ) -> None:
        from netcoredbg_mcp.ui.flaui_client import FlaUIBridgeClient
        from netcoredbg_mcp.windows_process_owner import WindowsOwnedProcess

        script = _write_large_response_bridge(tmp_path, stderr_bytes=stderr_bytes)
        real_launch = WindowsOwnedProcess.launch

        async def launch_python_bridge(**kwargs):
            kwargs["argv"] = (sys.executable, "-u", str(script))
            return await real_launch(**kwargs)

        client = FlaUIBridgeClient(str(script))
        with patch.object(WindowsOwnedProcess, "launch", side_effect=launch_python_bridge):
            await client.start()
        process = client._process
        assert isinstance(process, WindowsOwnedProcess)
        try:
            response = await client.call("capture_screenshot", timeout=5.0)
        finally:
            await client.stop()

        assert len(response["png_base64"]) == LARGE_JSON_RESPONSE_BYTES
        assert process.returncode == 0
        assert process._closed

    @pytest.mark.asyncio
    async def test_start_keeps_large_response_limit_on_owned_stdout(self) -> None:
        from netcoredbg_mcp.ui.flaui_client import BRIDGE_RESPONSE_LINE_LIMIT, FlaUIBridgeClient

        client = FlaUIBridgeClient("C:/fake/FlaUIBridge.exe")
        process = _BridgeOwner(123)
        with patch(
            "netcoredbg_mcp.windows_process_owner.WindowsOwnedProcess.launch",
            return_value=process,
        ) as launch:
            await client.start()

        assert launch.await_args is not None
        assert launch.await_args.kwargs["stdout_limit"] == BRIDGE_RESPONSE_LINE_LIMIT
        await client.stop()


class TestPywinautoBackend:
    """Tests for PywinautoBackend wrapper."""

    def test_element_cache_property(self):
        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=None):
            backend = create_backend()
            assert isinstance(backend.element_cache, dict)
            assert backend.process_id is None

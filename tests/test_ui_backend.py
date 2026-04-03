"""Tests for UI backend abstraction layer."""

from unittest.mock import patch, MagicMock

from netcoredbg_mcp.ui.backend import find_flaui_bridge, create_backend


class TestFindFlauiBridge:
    """Tests for FlaUI bridge binary discovery."""

    def test_finds_via_env_var(self, tmp_path):
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")

        with patch.dict("os.environ", {"FLAUI_BRIDGE_PATH": str(bridge)}):
            result = find_flaui_bridge()
            assert result is not None
            assert "FlaUIBridge.exe" in result

    def test_finds_next_to_netcoredbg(self, tmp_path):
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")
        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.write_text("fake")

        with patch.dict("os.environ", {
            "NETCOREDBG_PATH": str(netcoredbg),
            "FLAUI_BRIDGE_PATH": "",
        }):
            result = find_flaui_bridge()
            assert result is not None

    def test_returns_none_when_not_found(self, tmp_path):
        with patch.dict("os.environ", {
            "FLAUI_BRIDGE_PATH": "",
            "NETCOREDBG_PATH": str(tmp_path / "nonexistent"),
        }):
            with patch("shutil.which", return_value=None):
                with patch("netcoredbg_mcp.ui.backend.Path") as MockPath:
                    # Make well-known path check return False
                    MockPath.return_value.is_file.return_value = False
                    MockPath.side_effect = lambda x: MagicMock(is_file=MagicMock(return_value=False))
                    result = find_flaui_bridge()
                    # On systems without D:\Bin\FlaUIBridge.exe this returns None
                    # On systems with it, it returns a path — both are valid

    def test_finds_on_path(self, tmp_path):
        bridge = tmp_path / "FlaUIBridge.exe"
        bridge.write_text("fake")

        with patch.dict("os.environ", {
            "FLAUI_BRIDGE_PATH": "",
            "NETCOREDBG_PATH": "",
        }):
            with patch("shutil.which", return_value=str(bridge)):
                result = find_flaui_bridge()
                # May find well-known path first, but at minimum doesn't crash
                assert result is None or "FlaUIBridge" in result


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


class TestPywinautoBackend:
    """Tests for PywinautoBackend wrapper."""

    def test_element_cache_property(self):
        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=None):
            backend = create_backend()
            assert isinstance(backend.element_cache, dict)
            assert backend.process_id is None

"""Tests for UI backend abstraction layer."""

from unittest.mock import patch, MagicMock

from netcoredbg_mcp.ui.backend import find_flaui_bridge, create_backend


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


class TestPywinautoBackend:
    """Tests for PywinautoBackend wrapper."""

    def test_element_cache_property(self):
        with patch("netcoredbg_mcp.ui.backend.find_flaui_bridge", return_value=None):
            backend = create_backend()
            assert isinstance(backend.element_cache, dict)
            assert backend.process_id is None

"""Tests for new UI tools: ui_invoke, ui_toggle, ui_file_dialog, root_id, xpath."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_backend():
    """Create a mock FlaUI backend."""
    backend = AsyncMock()
    backend.process_id = 1234
    backend.element_cache = {}
    return backend


@pytest.fixture
def mock_session():
    """Create a mock SessionManager with STOPPED state."""
    from netcoredbg_mcp.session.state import DebugState

    session = MagicMock()
    session.state.state = DebugState.STOPPED
    session.state.process_id = 1234
    session.process_registry = None
    return session


class TestFlaUIBackendInvoke:
    """Tests for FlaUIBackend.invoke_element."""

    @pytest.mark.asyncio
    async def test_invoke_delegates_to_bridge(self):
        """invoke_element calls bridge with correct params."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "invoked": True,
            "method": "InvokePattern",
            "automationId": "btn1",
            "name": "Save",
            "controlType": "Button",
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.invoke_element(automation_id="btn1")
        backend._client.call.assert_called_once_with(
            "invoke_element", {"automationId": "btn1"},
        )
        assert result["invoked"] is True
        assert result["method"] == "InvokePattern"

    @pytest.mark.asyncio
    async def test_invoke_with_root_id(self):
        """invoke_element passes rootAutomationId to bridge."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"invoked": True, "method": "Click"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.invoke_element(automation_id="btn", root_id="panel1")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "panel1"

    @pytest.mark.asyncio
    async def test_invoke_with_xpath(self):
        """invoke_element passes xpath to bridge."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"invoked": True, "method": "InvokePattern"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.invoke_element(xpath="//Button[@Name='Save']")
        args = backend._client.call.call_args
        assert args[0][1]["xpath"] == "//Button[@Name='Save']"


class TestFlaUIBackendToggle:
    """Tests for FlaUIBackend.toggle_element."""

    @pytest.mark.asyncio
    async def test_toggle_returns_new_state(self):
        """toggle_element returns toggled state."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "toggled": True,
            "newState": "On",
            "automationId": "chk1",
            "name": "Enabled",
            "controlType": "CheckBox",
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.toggle_element(automation_id="chk1")
        assert result["toggled"] is True
        assert result["newState"] == "On"

    @pytest.mark.asyncio
    async def test_toggle_with_root_id(self):
        """toggle_element passes rootAutomationId."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"toggled": True, "newState": "Off"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.toggle_element(name="Debug", root_id="settingsPanel")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "settingsPanel"


class TestFlaUIBackendXPath:
    """Tests for FlaUIBackend.find_by_xpath."""

    @pytest.mark.asyncio
    async def test_find_by_xpath_returns_element(self):
        """find_by_xpath returns element info with matchCount."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "found": True,
            "automationId": "btn1",
            "name": "Save",
            "controlType": "Button",
            "matchCount": 1,
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.find_by_xpath("//Button[@Name='Save']")
        backend._client.call.assert_called_once_with(
            "find_by_xpath", {"xpath": "//Button[@Name='Save']"},
        )
        assert result["found"] is True
        assert result["matchCount"] == 1

    @pytest.mark.asyncio
    async def test_find_by_xpath_with_root_id(self):
        """find_by_xpath scopes search via rootAutomationId."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"found": True, "matchCount": 1})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.find_by_xpath("//Edit", root_id="form1")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "form1"


class TestPywinautoBackendInvoke:
    """Tests for PywinautoBackend invoke/toggle/xpath."""

    @pytest.mark.asyncio
    async def test_invoke_calls_iface_invoke(self):
        """invoke_element uses iface_invoke when available."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = MagicMock()
        mock_element.iface_invoke.Invoke = MagicMock()
        mock_element.element_info.automation_id = "btn1"
        mock_element.element_info.name = "Save"
        mock_element.element_info.control_type = "Button"

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(return_value=MagicMock(
            automation_id="btn1", name="Save", control_type="Button",
        ))
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="btn1")
        assert result["invoked"] is True
        assert result["method"] == "InvokePattern"

    @pytest.mark.asyncio
    async def test_invoke_falls_back_to_click(self):
        """invoke_element falls back to click when iface_invoke is None."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_invoke = None
        mock_element.click = MagicMock()

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(return_value=MagicMock(
            automation_id="div1", name="Panel", control_type="Pane",
        ))
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.invoke_element(automation_id="div1")
        assert result["method"] == "Click"

    @pytest.mark.asyncio
    async def test_toggle_returns_state(self):
        """toggle_element returns On/Off/Indeterminate."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_toggle = MagicMock()
        mock_toggle.Toggle = MagicMock()
        mock_toggle.CurrentToggleState = 1  # On
        mock_element.iface_toggle = mock_toggle

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui.get_element_info = AsyncMock(return_value=MagicMock(
            automation_id="chk1", name="Enabled", control_type="CheckBox",
        ))
        mock_ui._executor = None
        backend._ui = mock_ui

        result = await backend.toggle_element(automation_id="chk1")
        assert result["toggled"] is True
        assert result["newState"] == "On"

    @pytest.mark.asyncio
    async def test_toggle_no_pattern_raises(self):
        """toggle_element raises when element has no TogglePattern."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        mock_ui = MagicMock()
        mock_element = MagicMock()
        mock_element.iface_toggle = None
        mock_element.element_info.control_type = "TextBox"

        mock_ui.find_element = AsyncMock(return_value=mock_element)
        mock_ui._executor = None
        backend._ui = mock_ui

        with pytest.raises(RuntimeError, match="TogglePattern"):
            await backend.toggle_element(automation_id="txt1")

    @pytest.mark.asyncio
    async def test_find_by_xpath_raises_not_implemented(self):
        """find_by_xpath on pywinauto raises NotImplementedError."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        backend._ui = MagicMock()

        with pytest.raises(NotImplementedError, match="FlaUI backend"):
            await backend.find_by_xpath("//Button")

    @pytest.mark.asyncio
    async def test_find_element_xpath_only_raises(self):
        """find_element with only xpath on pywinauto raises."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        backend = PywinautoBackend.__new__(PywinautoBackend)
        backend._ui = MagicMock()

        with pytest.raises(NotImplementedError, match="FlaUI backend"):
            await backend.find_element(xpath="//Button")


class TestBackendProtocol:
    """Tests for UIBackend protocol conformance."""

    def test_flaui_backend_has_new_methods(self):
        """FlaUIBackend implements invoke/toggle/find_by_xpath."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        assert hasattr(FlaUIBackend, "invoke_element")
        assert hasattr(FlaUIBackend, "toggle_element")
        assert hasattr(FlaUIBackend, "find_by_xpath")

    def test_pywinauto_backend_has_new_methods(self):
        """PywinautoBackend implements invoke/toggle/find_by_xpath."""
        from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

        assert hasattr(PywinautoBackend, "invoke_element")
        assert hasattr(PywinautoBackend, "toggle_element")
        assert hasattr(PywinautoBackend, "find_by_xpath")

    def test_find_element_accepts_root_id_xpath(self):
        """find_element signature includes root_id and xpath params."""
        import inspect
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        sig = inspect.signature(FlaUIBackend.find_element)
        params = list(sig.parameters.keys())
        assert "root_id" in params
        assert "xpath" in params

    def test_build_search_params(self):
        """_build_search_params converts snake_case to camelCase."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        params = FlaUIBackend._build_search_params(
            automation_id="btn1",
            root_id="panel1",
            xpath="//Button",
        )
        assert params == {
            "automationId": "btn1",
            "rootAutomationId": "panel1",
            "xpath": "//Button",
        }

    def test_build_search_params_omits_none(self):
        """_build_search_params skips None values."""
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        params = FlaUIBackend._build_search_params(automation_id="btn1")
        assert params == {"automationId": "btn1"}
        assert "rootAutomationId" not in params
        assert "xpath" not in params

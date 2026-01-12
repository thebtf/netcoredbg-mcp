"""Tests for UI automation module."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from netcoredbg_mcp.ui.errors import (
    ElementNotFoundError,
    NoProcessIdError,
    UIAutomationError,
)
from netcoredbg_mcp.ui.serialization import ElementInfo, serialize_element


class TestElementInfo:
    """Tests for ElementInfo dataclass."""

    def test_to_dict_basic(self):
        """Test basic ElementInfo serialization."""
        info = ElementInfo(
            automation_id="btn1",
            control_type="Button",
            name="OK",
            class_name="Button",
            rectangle={"left": 0, "top": 0, "right": 100, "bottom": 50},
            is_enabled=True,
            is_visible=True,
            has_keyboard_focus=False,
            child_count=0,
            children=[],
        )
        result = info.to_dict()

        assert result["automationId"] == "btn1"
        assert result["controlType"] == "Button"
        assert result["name"] == "OK"
        assert result["isEnabled"] is True
        assert result["childCount"] == 0

    def test_to_dict_with_children(self):
        """Test ElementInfo with nested children."""
        child = ElementInfo(
            automation_id="child1",
            control_type="Text",
            name="Label",
            class_name="TextBlock",
            rectangle={},
            is_enabled=True,
            is_visible=True,
            has_keyboard_focus=False,
            child_count=0,
            children=[],
        )
        parent = ElementInfo(
            automation_id="parent",
            control_type="Panel",
            name="Container",
            class_name="StackPanel",
            rectangle={},
            is_enabled=True,
            is_visible=True,
            has_keyboard_focus=False,
            child_count=1,
            children=[child],
        )
        result = parent.to_dict()

        assert result["childCount"] == 1
        assert len(result["children"]) == 1
        assert result["children"][0]["automationId"] == "child1"


class TestUIAutomationErrors:
    """Tests for UI automation error classes."""

    def test_no_process_id_error(self):
        """Test NoProcessIdError."""
        error = NoProcessIdError("No process")
        assert str(error) == "No process"
        assert isinstance(error, UIAutomationError)

    def test_element_not_found_error(self):
        """Test ElementNotFoundError."""
        error = ElementNotFoundError("Element not found: btn1")
        assert "btn1" in str(error)
        assert isinstance(error, UIAutomationError)


class TestUIAutomation:
    """Tests for UIAutomation class."""

    @pytest.fixture
    def ui_automation(self):
        """Create UIAutomation instance."""
        from netcoredbg_mcp.ui.automation import UIAutomation
        return UIAutomation()

    def test_init(self, ui_automation):
        """Test UIAutomation initialization."""
        assert ui_automation._app is None
        assert ui_automation._process_id is None
        assert ui_automation._executor is not None

    @pytest.mark.asyncio
    async def test_connect_invalid_pid(self, ui_automation):
        """Test connect with invalid PID."""
        with pytest.raises(NoProcessIdError):
            await ui_automation.connect(0)

        with pytest.raises(NoProcessIdError):
            await ui_automation.connect(-1)

    @pytest.mark.asyncio
    async def test_get_window_tree_not_connected(self, ui_automation):
        """Test get_window_tree when not connected."""
        with pytest.raises(NoProcessIdError, match="Not connected"):
            await ui_automation.get_window_tree()

    @pytest.mark.asyncio
    async def test_find_element_not_connected(self, ui_automation):
        """Test find_element when not connected."""
        with pytest.raises(NoProcessIdError, match="Not connected"):
            await ui_automation.find_element(automation_id="test")

    @pytest.mark.asyncio
    async def test_find_element_no_criteria(self, ui_automation):
        """Test find_element with no search criteria."""
        # Mock connection
        ui_automation._app = MagicMock()
        ui_automation._process_id = 1234

        with pytest.raises(ValueError, match="At least one search criterion"):
            await ui_automation.find_element()

    @pytest.mark.asyncio
    async def test_send_keys_focused_not_connected(self, ui_automation):
        """Test send_keys_focused when not connected."""
        with pytest.raises(NoProcessIdError, match="Not connected"):
            await ui_automation.send_keys_focused("{ENTER}")

    @pytest.mark.asyncio
    async def test_disconnect(self, ui_automation):
        """Test disconnect clears state."""
        ui_automation._app = MagicMock()
        ui_automation._process_id = 1234

        await ui_automation.disconnect()

        assert ui_automation._app is None
        assert ui_automation._process_id is None

    def test_shutdown(self, ui_automation):
        """Test shutdown doesn't raise."""
        ui_automation.shutdown()


class TestSerializeElement:
    """Tests for serialize_element function."""

    def test_serialize_with_max_depth_zero(self):
        """Test serialization with max_depth=0 excludes children."""
        mock_element = MagicMock()
        mock_element.element_info.automation_id = "test"
        mock_element.element_info.control_type = "Button"
        mock_element.element_info.name = "Test"
        mock_element.element_info.class_name = "Button"
        mock_element.element_info.rectangle = MagicMock(
            left=0, top=0, right=100, bottom=50
        )
        mock_element.is_enabled.return_value = True
        mock_element.is_visible.return_value = True
        mock_element.has_keyboard_focus.return_value = False
        mock_element.children.return_value = [MagicMock()]  # Has children

        result = serialize_element(mock_element, max_depth=0, max_children=10)

        assert result.automation_id == "test"
        assert result.children == []  # No children at depth 0

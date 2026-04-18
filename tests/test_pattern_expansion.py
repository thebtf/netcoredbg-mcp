"""Tests for v0.11.1 pattern expansion: window, transform, expand/collapse,
set_value (RangeValue), clipboard, and virtualized item tools.

Covers:
- FlaUIBackend method forwarding to bridge (method names + param shapes)
- PywinautoBackend returns {unsupported: True} for all 12 new methods
- RuntimeError on non-dict bridge responses
- Input validation (missing params, out-of-range, wrong types)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


# ─────────────────────────────────────────────────────────────
# TestWindowControl
# ─────────────────────────────────────────────────────────────

class TestWindowControl:
    @pytest.mark.asyncio
    async def test_close_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"closed": True, "window_title": "My App"}
        result = await backend.close_window()
        assert result["closed"] is True
        backend._client.call.assert_awaited_once_with("close_window", {})

    @pytest.mark.asyncio
    async def test_close_window_with_title(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"closed": True, "window_title": "Dialog"}
        await backend.close_window(window_title="Dialog")
        backend._client.call.assert_awaited_once_with(
            "close_window", {"window_title": "Dialog"}
        )

    @pytest.mark.asyncio
    async def test_maximize_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"maximized": True, "window_title": "App"}
        result = await backend.maximize_window()
        assert result["maximized"] is True
        backend._client.call.assert_awaited_once_with("maximize_window", {})

    @pytest.mark.asyncio
    async def test_minimize_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"minimized": True, "window_title": "App"}
        result = await backend.minimize_window()
        assert result["minimized"] is True

    @pytest.mark.asyncio
    async def test_restore_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"restored": True, "window_title": "App"}
        result = await backend.restore_window()
        assert result["restored"] is True

    @pytest.mark.asyncio
    async def test_close_window_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.close_window()

    @pytest.mark.asyncio
    async def test_unsupported_close_window(self):
        backend = PywinautoBackend()
        result = await backend.close_window()
        assert result["unsupported"] is True
        assert "WindowPattern" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_maximize_window(self):
        backend = PywinautoBackend()
        result = await backend.maximize_window()
        assert result["unsupported"] is True

    @pytest.mark.asyncio
    async def test_unsupported_minimize_window(self):
        backend = PywinautoBackend()
        result = await backend.minimize_window()
        assert result["unsupported"] is True

    @pytest.mark.asyncio
    async def test_unsupported_restore_window(self):
        backend = PywinautoBackend()
        result = await backend.restore_window()
        assert result["unsupported"] is True


# ─────────────────────────────────────────────────────────────
# TestTransform
# ─────────────────────────────────────────────────────────────

class TestTransform:
    @pytest.mark.asyncio
    async def test_move_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"moved": True, "x": 100, "y": 200, "window_title": "App"}
        result = await backend.move_window(100, 200)
        assert result["moved"] is True
        backend._client.call.assert_awaited_once_with("move_window", {"x": 100, "y": 200})

    @pytest.mark.asyncio
    async def test_move_window_not_movable(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "moved": False,
            "reason": "window is not movable",
            "window_title": "FixedDialog",
        }
        result = await backend.move_window(0, 0)
        assert result["moved"] is False
        assert "not movable" in result["reason"]

    @pytest.mark.asyncio
    async def test_resize_window_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"resized": True, "width": 800, "height": 600}
        result = await backend.resize_window(800, 600)
        assert result["resized"] is True
        backend._client.call.assert_awaited_once_with("resize_window", {"width": 800, "height": 600})

    @pytest.mark.asyncio
    async def test_resize_window_not_resizable(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "resized": False,
            "reason": "window is not resizable",
        }
        result = await backend.resize_window(200, 200)
        assert result["resized"] is False
        assert "not resizable" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_move_window(self):
        backend = PywinautoBackend()
        result = await backend.move_window(0, 0)
        assert result["unsupported"] is True
        assert "TransformPattern" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_resize_window(self):
        backend = PywinautoBackend()
        result = await backend.resize_window(800, 600)
        assert result["unsupported"] is True
        assert "TransformPattern" in result["reason"]


# ─────────────────────────────────────────────────────────────
# TestExpandCollapse
# ─────────────────────────────────────────────────────────────

class TestExpandCollapse:
    @pytest.mark.asyncio
    async def test_expand_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "expanded": True,
            "automation_id": "TreeRoot",
            "was_already": False,
        }
        result = await backend.expand("TreeRoot")
        assert result["expanded"] is True
        assert result["was_already"] is False
        backend._client.call.assert_awaited_once_with("expand", {"automationId": "TreeRoot"})

    @pytest.mark.asyncio
    async def test_expand_already_expanded(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "expanded": True,
            "automation_id": "TreeRoot",
            "was_already": True,
        }
        result = await backend.expand("TreeRoot")
        assert result["was_already"] is True

    @pytest.mark.asyncio
    async def test_collapse_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "collapsed": True,
            "automation_id": "TreeRoot",
            "was_already": False,
        }
        result = await backend.collapse("TreeRoot")
        assert result["collapsed"] is True
        backend._client.call.assert_awaited_once_with("collapse", {"automationId": "TreeRoot"})

    @pytest.mark.asyncio
    async def test_expand_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = "error string"
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.expand("TreeRoot")

    @pytest.mark.asyncio
    async def test_unsupported_expand(self):
        backend = PywinautoBackend()
        result = await backend.expand("anything")
        assert result["unsupported"] is True
        assert "ExpandCollapsePattern" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_collapse(self):
        backend = PywinautoBackend()
        result = await backend.collapse("anything")
        assert result["unsupported"] is True
        assert "ExpandCollapsePattern" in result["reason"]


# ─────────────────────────────────────────────────────────────
# TestSetValue (RangeValue)
# ─────────────────────────────────────────────────────────────

class TestSetValue:
    @pytest.mark.asyncio
    async def test_set_value_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "set": True,
            "automation_id": "DurationSlider",
            "value": 75.0,
            "minimum": 0.0,
            "maximum": 100.0,
        }
        result = await backend.set_value("DurationSlider", 75.0)
        assert result["set"] is True
        assert result["value"] == 75.0
        backend._client.call.assert_awaited_once_with(
            "range_set_value",
            {"automationId": "DurationSlider", "value": 75.0},
        )

    @pytest.mark.asyncio
    async def test_set_value_out_of_range(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "set": False,
            "reason": "value 200.0 out of range [0.0..100.0]",
            "automation_id": "DurationSlider",
            "minimum": 0.0,
            "maximum": 100.0,
        }
        result = await backend.set_value("DurationSlider", 200.0)
        assert result["set"] is False
        assert "out of range" in result["reason"]

    @pytest.mark.asyncio
    async def test_set_value_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = 42
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.set_value("Slider", 50.0)

    @pytest.mark.asyncio
    async def test_unsupported_set_value(self):
        backend = PywinautoBackend()
        result = await backend.set_value("Slider", 50.0)
        assert result["unsupported"] is True
        assert "RangeValuePattern" in result["reason"]


# ─────────────────────────────────────────────────────────────
# TestClipboard
# ─────────────────────────────────────────────────────────────

class TestClipboard:
    @pytest.mark.asyncio
    async def test_clipboard_read_with_text(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"text": "hello world", "has_text": True}
        result = await backend.clipboard_read()
        assert result["has_text"] is True
        assert result["text"] == "hello world"
        backend._client.call.assert_awaited_once_with("clipboard_read", {})

    @pytest.mark.asyncio
    async def test_clipboard_read_empty(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"text": "", "has_text": False}
        result = await backend.clipboard_read()
        assert result["has_text"] is False
        assert result["text"] == ""

    @pytest.mark.asyncio
    async def test_clipboard_write_returns_length(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"written": True, "length": 5}
        result = await backend.clipboard_write("hello")
        assert result["written"] is True
        assert result["length"] == 5
        backend._client.call.assert_awaited_once_with("clipboard_write", {"text": "hello"})

    @pytest.mark.asyncio
    async def test_clipboard_write_unicode(self):
        backend = _make_flaui()
        text = "emoji \U0001F389 \u00fcnic\u00f6de"
        backend._client.call.return_value = {"written": True, "length": len(text)}
        result = await backend.clipboard_write(text)
        assert result["length"] == len(text)
        backend._client.call.assert_awaited_once_with("clipboard_write", {"text": text})

    @pytest.mark.asyncio
    async def test_clipboard_read_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.clipboard_read()

    @pytest.mark.asyncio
    async def test_clipboard_write_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.clipboard_write("text")

    @pytest.mark.asyncio
    async def test_unsupported_clipboard_read(self):
        backend = PywinautoBackend()
        result = await backend.clipboard_read()
        assert result["unsupported"] is True
        assert "Clipboard" in result["reason"]

    @pytest.mark.asyncio
    async def test_unsupported_clipboard_write(self):
        backend = PywinautoBackend()
        result = await backend.clipboard_write("any")
        assert result["unsupported"] is True
        assert "Clipboard" in result["reason"]


# ─────────────────────────────────────────────────────────────
# TestVirtualizedItem
# ─────────────────────────────────────────────────────────────

class TestVirtualizedItem:
    @pytest.mark.asyncio
    async def test_realize_happy(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "realized": True,
            "element_id": "VirtList_Row_150",
            "bounding_rect": {"x": 0, "y": 1500, "width": 300, "height": 20},
        }
        result = await backend.realize_virtualized_item(
            container_automation_id="VirtList",
            property="AutomationId",
            value="VirtList_Row_150",
        )
        assert result["realized"] is True
        assert result["element_id"] == "VirtList_Row_150"
        assert "bounding_rect" in result
        backend._client.call.assert_awaited_once_with(
            "realize_virtualized_item",
            {
                "container_automation_id": "VirtList",
                "property": "AutomationId",
                "value": "VirtList_Row_150",
            },
        )

    @pytest.mark.asyncio
    async def test_realize_item_not_found(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "realized": False,
            "reason": "item not found",
        }
        result = await backend.realize_virtualized_item(
            container_automation_id="VirtList",
            property="AutomationId",
            value="NonExistent_Row",
        )
        assert result["realized"] is False
        assert result["reason"] == "item not found"

    @pytest.mark.asyncio
    async def test_realize_unsupported_container(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "realized": False,
            "reason": "container does not support ItemContainerPattern",
        }
        result = await backend.realize_virtualized_item(
            container_automation_id="PlainButton",
            property="AutomationId",
            value="anything",
        )
        assert result["realized"] is False
        assert "ItemContainerPattern" in result["reason"]

    @pytest.mark.asyncio
    async def test_realize_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.realize_virtualized_item("List", "AutomationId", "Row_0")

    @pytest.mark.asyncio
    async def test_unsupported_realize_virtualized_item(self):
        backend = PywinautoBackend()
        result = await backend.realize_virtualized_item("List", "AutomationId", "Row_0")
        assert result["unsupported"] is True
        assert "VirtualizedItemPattern" in result["reason"]


# ─────────────────────────────────────────────────────────────
# TestPywinautoUnsupported — all 12 new methods
# ─────────────────────────────────────────────────────────────

class TestPywinautoUnsupported:
    """All 12 v0.11.1 methods must return {unsupported: True} on PywinautoBackend."""

    @pytest.fixture
    def backend(self):
        return PywinautoBackend()

    @pytest.mark.asyncio
    async def test_close_window(self, backend):
        r = await backend.close_window()
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_maximize_window(self, backend):
        r = await backend.maximize_window()
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_minimize_window(self, backend):
        r = await backend.minimize_window()
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_restore_window(self, backend):
        r = await backend.restore_window()
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_move_window(self, backend):
        r = await backend.move_window(0, 0)
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_resize_window(self, backend):
        r = await backend.resize_window(800, 600)
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_expand(self, backend):
        r = await backend.expand("node")
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_collapse(self, backend):
        r = await backend.collapse("node")
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_set_value(self, backend):
        r = await backend.set_value("slider", 50.0)
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_clipboard_read(self, backend):
        r = await backend.clipboard_read()
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_clipboard_write(self, backend):
        r = await backend.clipboard_write("text")
        assert r["unsupported"] is True

    @pytest.mark.asyncio
    async def test_realize_virtualized_item(self, backend):
        r = await backend.realize_virtualized_item("list", "AutomationId", "row")
        assert r["unsupported"] is True

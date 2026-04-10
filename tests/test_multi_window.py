"""Tests for multi-window bridge support (engram issue #7).

Covers the Python-side contract for:
- FlaUIBackend.get_window_tree parsing the new {windows:[...]} envelope and
  caching every window's elements, while staying backward-compatible with the
  legacy single-tree shape.
- FlaUIBackend.switch_window forwarding to the bridge's set_active_window
  command with correct parameters.
- PywinautoBackend.switch_window raising NotImplementedError so tools layer
  can return a structured error.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend


def _make_backend() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


def _window_node(automation_id: str, name: str, children: list | None = None) -> dict:
    return {
        "found": True,
        "automationId": automation_id,
        "name": name,
        "controlType": "Window",
        "className": "",
        "rect": {"x": 0, "y": 0, "width": 800, "height": 600},
        "children": children or [],
    }


def _edit_node(automation_id: str, name: str = "") -> dict:
    return {
        "found": True,
        "automationId": automation_id,
        "name": name,
        "controlType": "Edit",
        "className": "",
        "rect": {"x": 10, "y": 20, "width": 200, "height": 30},
        "children": [],
    }


class TestMultiWindowGetTree:
    """FlaUIBackend.get_window_tree must handle the {windows:[...]} envelope."""

    @pytest.mark.asyncio
    async def test_parses_multi_window_envelope(self):
        backend = _make_backend()
        backend._client.call.return_value = {
            "windows": [
                _window_node("MainAppRoot", "Main App", [_edit_node("mainTextBox")]),
                _window_node("", "Create collection", [_edit_node("dialogTextBox")]),
            ],
            "count": 2,
            "primary": "Main App",
        }

        result = await backend.get_window_tree()

        assert isinstance(result, dict)
        assert result["count"] == 2
        assert result["primary"] == "Main App"
        assert len(result["windows"]) == 2
        backend._client.call.assert_awaited_once_with(
            "get_tree",
            {"maxDepth": 3, "maxChildren": 50},
        )

    @pytest.mark.asyncio
    async def test_caches_elements_from_every_window(self):
        """Elements in dialog subtree must be cached alongside main window ones."""
        backend = _make_backend()
        backend._client.call.return_value = {
            "windows": [
                _window_node("MainAppRoot", "Main App", [_edit_node("mainTextBox")]),
                _window_node("", "Create collection", [_edit_node("dialogTextBox")]),
            ],
            "count": 2,
            "primary": "Main App",
        }

        await backend.get_window_tree()

        assert "mainTextBox" in backend.element_cache
        assert "dialogTextBox" in backend.element_cache
        dialog_rect = backend.element_cache["dialogTextBox"]["rect"]
        assert dialog_rect["left"] == 10
        assert dialog_rect["top"] == 20
        assert dialog_rect["right"] == 210
        assert dialog_rect["bottom"] == 50

    @pytest.mark.asyncio
    async def test_backward_compatible_with_legacy_single_tree(self):
        """If the bridge returns the legacy flat shape, cache still builds."""
        backend = _make_backend()
        backend._client.call.return_value = _window_node(
            "MainAppRoot", "Main App", [_edit_node("legacyTextBox")]
        )

        result = await backend.get_window_tree()

        assert result["automationId"] == "MainAppRoot"
        assert "legacyTextBox" in backend.element_cache

    @pytest.mark.asyncio
    async def test_cache_rebuilt_on_every_call(self):
        """Previous cache entries must be cleared before re-walking."""
        backend = _make_backend()
        backend._element_cache["stale"] = {"rect": {}, "name": "old", "control_type": "Button"}

        backend._client.call.return_value = {
            "windows": [_window_node("Root", "App", [_edit_node("fresh")])],
            "count": 1,
            "primary": "App",
        }

        await backend.get_window_tree()

        assert "stale" not in backend.element_cache
        assert "fresh" in backend.element_cache

    @pytest.mark.asyncio
    async def test_empty_windows_list_produces_empty_cache(self):
        backend = _make_backend()
        backend._element_cache["old"] = {"rect": {}, "name": "x", "control_type": "y"}
        backend._client.call.return_value = {"windows": [], "count": 0, "primary": ""}

        await backend.get_window_tree()

        assert backend.element_cache == {}

    @pytest.mark.asyncio
    async def test_none_response_produces_empty_cache(self):
        """Defensive guard — the bridge must not return None, but if it does,
        _iter_windows should short-circuit and the cache should stay empty."""
        backend = _make_backend()
        backend._element_cache["stale"] = {"rect": {}, "name": "x", "control_type": "y"}
        backend._client.call.return_value = None

        result = await backend.get_window_tree()

        assert result is None
        assert backend.element_cache == {}

    @pytest.mark.asyncio
    async def test_error_shape_with_found_false_is_not_cached(self):
        """If the bridge ever returns an error-flavored envelope {'found': False,
        'error': '...'}, _iter_windows treats it as a single legacy node but
        there is nothing to cache (no automationId or rect)."""
        backend = _make_backend()
        backend._client.call.return_value = {"found": False, "error": "boom"}

        result = await backend.get_window_tree()

        assert result == {"found": False, "error": "boom"}
        assert backend.element_cache == {}


class TestSwitchWindow:
    """FlaUIBackend.switch_window contract with the bridge's set_active_window."""

    @pytest.mark.asyncio
    async def test_switch_by_name(self):
        backend = _make_backend()
        backend._client.call.return_value = {
            "switched": True,
            "title": "Create collection",
            "automationId": "",
        }

        result = await backend.switch_window(name="Create collection")

        assert result["switched"] is True
        assert result["title"] == "Create collection"
        backend._client.call.assert_awaited_once_with(
            "set_active_window",
            {"name": "Create collection"},
        )

    @pytest.mark.asyncio
    async def test_switch_by_automation_id(self):
        backend = _make_backend()
        backend._client.call.return_value = {
            "switched": True,
            "title": "Dialog",
            "automationId": "dlgRoot",
        }

        result = await backend.switch_window(automation_id="dlgRoot")

        assert result["switched"] is True
        backend._client.call.assert_awaited_once_with(
            "set_active_window",
            {"automationId": "dlgRoot"},
        )

    @pytest.mark.asyncio
    async def test_switch_by_both_forwards_both_params(self):
        backend = _make_backend()
        backend._client.call.return_value = {"switched": True, "title": "T", "automationId": "A"}

        await backend.switch_window(name="T", automation_id="A")

        backend._client.call.assert_awaited_once_with(
            "set_active_window",
            {"automationId": "A", "name": "T"},
        )

    @pytest.mark.asyncio
    async def test_switch_without_criteria_raises(self):
        backend = _make_backend()
        with pytest.raises(ValueError, match="at least one of"):
            await backend.switch_window()
        backend._client.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_raises_on_non_dict_bridge_response(self):
        """A non-dict response indicates a bridge contract violation and must
        surface as an explicit error rather than being masked into a silent
        ``switched=False``."""
        backend = _make_backend()
        backend._client.call.return_value = None

        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.switch_window(name="Missing")


class TestPywinautoSwitchWindow:
    """Pywinauto fallback must report capability unsupported via a structured
    response rather than raising, so tool-layer callers can surface a clean
    error without catching NotImplementedError on the UIBackend protocol."""

    @pytest.mark.asyncio
    async def test_returns_structured_unsupported_response(self):
        backend = PywinautoBackend()
        result = await backend.switch_window(name="Create collection")
        assert isinstance(result, dict)
        assert result.get("switched") is False
        assert result.get("unsupported") is True
        assert "FlaUI bridge" in result.get("reason", "")

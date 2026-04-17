"""Tests for UI input primitives (engram #79, #80, #81).

Covers the Python-side contract for:
- FlaUIBackend.drag — forwards {x1, y1, x2, y2, speed_ms, hold_modifiers} to
  the bridge's `drag` command.
- FlaUIBackend.send_system_event — forwards {event, mode} to the bridge's
  `send_system_event` command.
- FlaUIBackend.hold_modifiers / release_modifiers / get_held_modifiers —
  forward to the bridge's persistent-modifier commands.
- PywinautoBackend — returns structured {unsupported:True} responses for
  FlaUI-only primitives (send_system_event, hold_modifiers, release_modifiers)
  and a compatible drag implementation with speed_ms/hold_modifiers.
- Input validation — unknown modifiers, speed_ms < 20, identical drag coords.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


class TestFlaUIDragForwarding:
    @pytest.mark.asyncio
    async def test_drag_forwards_default_speed(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "dragged": True,
            "x1": 10, "y1": 20, "x2": 100, "y2": 200,
            "steps": 10, "duration_ms": 200,
        }
        result = await backend.drag(10, 20, 100, 200)
        assert result["dragged"] is True
        backend._client.call.assert_awaited_once_with(
            "drag",
            {"x1": 10, "y1": 20, "x2": 100, "y2": 200, "speed_ms": 200, "hold_modifiers": []},
        )

    @pytest.mark.asyncio
    async def test_drag_forwards_custom_speed_and_modifiers(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "dragged": True,
            "x1": 0, "y1": 0, "x2": 50, "y2": 50,
            "steps": 25, "duration_ms": 500,
        }
        await backend.drag(0, 0, 50, 50, speed_ms=500, hold_modifiers=["ctrl"])
        backend._client.call.assert_awaited_once_with(
            "drag",
            {"x1": 0, "y1": 0, "x2": 50, "y2": 50, "speed_ms": 500, "hold_modifiers": ["ctrl"]},
        )

    @pytest.mark.asyncio
    async def test_drag_raises_on_non_dict_response(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.drag(0, 0, 100, 100)


class TestFlaUISystemEvent:
    @pytest.mark.asyncio
    async def test_send_system_event_toggle(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "event": "theme_change", "from": "dark", "to": "light",
        }
        result = await backend.send_system_event("theme_change", mode="toggle")
        assert result["to"] == "light"
        backend._client.call.assert_awaited_once_with(
            "send_system_event",
            {"event": "theme_change", "mode": "toggle"},
        )

    @pytest.mark.asyncio
    async def test_send_system_event_explicit_mode(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "event": "theme_change", "from": "dark", "to": "light",
        }
        await backend.send_system_event("theme_change", mode="light")
        backend._client.call.assert_awaited_once_with(
            "send_system_event",
            {"event": "theme_change", "mode": "light"},
        )

    @pytest.mark.asyncio
    async def test_send_system_event_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = "not a dict"
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.send_system_event("theme_change")


class TestFlaUIModifierHold:
    @pytest.mark.asyncio
    async def test_hold_modifiers_forwards_list(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"modifiers": ["ctrl"]}
        await backend.hold_modifiers(["ctrl"])
        backend._client.call.assert_awaited_once_with(
            "hold_modifiers",
            {"modifiers": ["ctrl"]},
        )

    @pytest.mark.asyncio
    async def test_release_modifiers_forwards_list(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"modifiers": []}
        await backend.release_modifiers(["ctrl"])
        backend._client.call.assert_awaited_once_with(
            "release_modifiers",
            {"modifiers": ["ctrl"]},
        )

    @pytest.mark.asyncio
    async def test_release_modifiers_forwards_all_sentinel(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"modifiers": []}
        await backend.release_modifiers("all")
        backend._client.call.assert_awaited_once_with(
            "release_modifiers",
            {"modifiers": "all"},
        )

    @pytest.mark.asyncio
    async def test_get_held_modifiers_forwards_empty_params(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"modifiers": ["ctrl", "shift"]}
        result = await backend.get_held_modifiers()
        assert result["modifiers"] == ["ctrl", "shift"]
        backend._client.call.assert_awaited_once_with("get_held_modifiers", {})

    @pytest.mark.asyncio
    async def test_hold_modifiers_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.hold_modifiers(["ctrl"])

    @pytest.mark.asyncio
    async def test_release_modifiers_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.release_modifiers(["ctrl"])

    @pytest.mark.asyncio
    async def test_get_held_modifiers_raises_on_non_dict(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.get_held_modifiers()


class TestPywinautoUnsupported:
    """Pywinauto backend must surface FlaUI-only primitives as structured
    unsupported responses — tool layer relies on this shape (see
    PR #47's switch_window pattern)."""

    @pytest.mark.asyncio
    async def test_send_system_event_returns_unsupported(self):
        backend = PywinautoBackend()
        result = await backend.send_system_event("theme_change", mode="toggle")
        assert result["unsupported"] is True
        assert result["switched"] is False
        assert "FlaUI bridge" in result["reason"]

    @pytest.mark.asyncio
    async def test_hold_modifiers_returns_unsupported(self):
        backend = PywinautoBackend()
        result = await backend.hold_modifiers(["ctrl"])
        assert result["unsupported"] is True
        assert "FlaUI bridge" in result["reason"]

    @pytest.mark.asyncio
    async def test_release_modifiers_returns_unsupported(self):
        backend = PywinautoBackend()
        result = await backend.release_modifiers(["ctrl"])
        assert result["unsupported"] is True
        assert "FlaUI bridge" in result["reason"]

    @pytest.mark.asyncio
    async def test_get_held_modifiers_returns_empty(self):
        """Pywinauto has no persistent modifier state — an empty list is the
        honest answer, not an unsupported marker (inspection should never
        fail)."""
        backend = PywinautoBackend()
        result = await backend.get_held_modifiers()
        assert result == {"modifiers": []}


class TestPywinautoDrag:
    """Pywinauto backend drag delegates to _send_drag via UIAutomation, with
    speed_ms/hold_modifiers passed through. The _drag_at_coords wrapper
    forwards params to the module-level _send_drag helper."""

    @pytest.mark.asyncio
    async def test_drag_forwards_speed_and_modifiers(self):
        backend = PywinautoBackend()
        send_drag_mock = MagicMock()
        with patch(
            "netcoredbg_mcp.ui.automation._send_drag",
            send_drag_mock,
        ):
            result = await backend.drag(10, 20, 100, 200, speed_ms=300, hold_modifiers=["ctrl"])
        send_drag_mock.assert_called_once()
        kwargs = send_drag_mock.call_args.kwargs
        args = send_drag_mock.call_args.args
        all_args = {**kwargs}
        if args:
            # Positional form: (from_x, from_y, to_x, to_y, speed_ms, hold_modifiers)
            positions = ["from_x", "from_y", "to_x", "to_y", "speed_ms", "hold_modifiers"]
            for idx, value in enumerate(args):
                if idx < len(positions):
                    all_args.setdefault(positions[idx], value)
        assert all_args.get("from_x") == 10
        assert all_args.get("to_x") == 100
        assert all_args.get("speed_ms") == 300
        assert all_args.get("hold_modifiers") == ["ctrl"]
        assert result["dragged"] is True
        assert result["duration_ms"] == 300


class TestDragInputValidation:
    """_send_drag (module-level helper) enforces drag-threshold safety floor
    and rejects zero-distance gestures. These guards must fire before any
    Win32 calls to avoid no-op drags that silently miss the WPF threshold."""

    def test_send_drag_rejects_speed_below_floor(self):
        from netcoredbg_mcp.ui.automation import _send_drag
        with pytest.raises(ValueError, match="speed_ms below drag-threshold"):
            _send_drag(0, 0, 100, 100, speed_ms=10)

    def test_send_drag_rejects_zero_distance(self):
        from netcoredbg_mcp.ui.automation import _send_drag
        with pytest.raises(ValueError, match="identical"):
            _send_drag(50, 50, 50, 50, speed_ms=200)

    def test_send_drag_rejects_unknown_modifier(self):
        from netcoredbg_mcp.ui.automation import _send_drag
        with pytest.raises(ValueError, match="Unknown modifier"):
            _send_drag(0, 0, 100, 100, speed_ms=200, hold_modifiers=["super"])

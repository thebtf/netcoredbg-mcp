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

import ctypes
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_flaui() -> FlaUIBackend:
    backend = FlaUIBackend("C:/fake/FlaUIBridge.exe")
    backend._client = MagicMock()
    backend._client.call = AsyncMock()
    return backend


def _csharp_method_body(content: str, signature: str) -> str:
    method_start = content.index(signature)
    body_start = content.index("{", method_start)
    depth = 0
    for index in range(body_start, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[method_start : index + 1]
    raise AssertionError(f"Could not find method body for {signature}")


class TestFlaUIDragForwarding:
    @pytest.mark.asyncio
    async def test_drag_forwards_default_speed(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "dragged": True,
            "x1": 10,
            "y1": 20,
            "x2": 100,
            "y2": 200,
            "steps": 10,
            "duration_ms": 200,
        }
        result = await backend.drag(10, 20, 100, 200)
        assert result["dragged"] is True
        backend._client.call.assert_awaited_once_with(
            "drag",
            {
                "x1": 10,
                "y1": 20,
                "x2": 100,
                "y2": 200,
                "speed_ms": 200,
                "hold_modifiers": [],
            },
        )

    @pytest.mark.asyncio
    async def test_drag_forwards_custom_speed_and_modifiers(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "dragged": True,
            "x1": 0,
            "y1": 0,
            "x2": 50,
            "y2": 50,
            "steps": 25,
            "duration_ms": 500,
        }
        await backend.drag(0, 0, 50, 50, speed_ms=500, hold_modifiers=["ctrl"])
        backend._client.call.assert_awaited_once_with(
            "drag",
            {
                "x1": 0,
                "y1": 0,
                "x2": 50,
                "y2": 50,
                "speed_ms": 500,
                "hold_modifiers": ["ctrl"],
            },
        )

    @pytest.mark.asyncio
    async def test_drag_raises_on_non_dict_response(self):
        backend = _make_flaui()
        backend._client.call.return_value = None
        with pytest.raises(RuntimeError, match="non-dict response"):
            await backend.drag(0, 0, 100, 100)

    @pytest.mark.asyncio
    async def test_drag_path_forwards_points_holds_and_modifiers(self):
        backend = _make_flaui()
        points = [
            {"x": 10, "y": 20},
            {"x": 10, "y": 180, "hold_ms": 650},
            {"x": 40, "y": 220},
        ]
        backend._client.call.return_value = {
            "dragged": True,
            "path_points": points,
            "hold_points": [points[1]],
            "modifier_cleanup": {"released": ["shift"]},
        }

        result = await backend.drag_path(points, speed_ms=700, hold_modifiers=["shift"])

        assert result["hold_points"] == [points[1]]
        assert result["modifier_cleanup"] == {"released": ["shift"]}
        backend._client.call.assert_awaited_once_with(
            "drag_path",
            {
                "points": points,
                "speed_ms": 700,
                "hold_modifiers": ["shift"],
            },
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_drag_path_forwards_cancel_key_when_requested(self):
        backend = _make_flaui()
        points = [{"x": 10, "y": 20}, {"x": 40, "y": 220}]
        backend._client.call.return_value = {
            "dragged": True,
            "cancel": {"key": "escape", "sent": True},
        }

        result = await backend.drag_path(points, cancel_key="escape")

        assert result["cancel"] == {"key": "escape", "sent": True}
        backend._client.call.assert_awaited_once_with(
            "drag_path",
            {
                "points": points,
                "speed_ms": 200,
                "hold_modifiers": [],
                "cancel_key": "escape",
            },
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_drag_path_distinguishes_held_edge_from_direct_route(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"dragged": True}
        direct_points = [{"x": 10, "y": 20}, {"x": 40, "y": 220}]
        held_points = [
            {"x": 10, "y": 20},
            {"x": 10, "y": 180, "hold_ms": 650},
            {"x": 40, "y": 220},
        ]

        await backend.drag_path(direct_points)
        await backend.drag_path(held_points)

        direct_payload = backend._client.call.await_args_list[0].args[1]
        held_payload = backend._client.call.await_args_list[1].args[1]
        assert direct_payload["points"] != held_payload["points"]
        assert "hold_ms" not in direct_payload["points"][1]
        assert held_payload["points"][1]["hold_ms"] == 650

    @pytest.mark.asyncio
    async def test_drag_path_expands_timeout_for_long_edge_holds(self):
        backend = _make_flaui()
        backend._client.call.return_value = {"dragged": True}
        points = [
            {"x": 70, "y": 75},
            {"x": 70, "y": 212, "hold_ms": 7000},
            {"x": 70, "y": 200},
        ]

        await backend.drag_path(points, speed_ms=900)

        timeout = backend._client.call.await_args.kwargs["timeout"]
        assert timeout > 10.0
        assert timeout <= 60.0


class TestFlaUISystemEvent:
    @pytest.mark.asyncio
    async def test_send_system_event_toggle(self):
        backend = _make_flaui()
        backend._client.call.return_value = {
            "event": "theme_change",
            "from": "dark",
            "to": "light",
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
            "event": "theme_change",
            "from": "dark",
            "to": "light",
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
            result = await backend.drag(
                10, 20, 100, 200, speed_ms=300, hold_modifiers=["ctrl"]
            )
        send_drag_mock.assert_called_once()
        kwargs = send_drag_mock.call_args.kwargs
        args = send_drag_mock.call_args.args
        all_args = {**kwargs}
        if args:
            # Positional form: (from_x, from_y, to_x, to_y, speed_ms, hold_modifiers)
            positions = [
                "from_x",
                "from_y",
                "to_x",
                "to_y",
                "speed_ms",
                "hold_modifiers",
            ]
            for idx, value in enumerate(args):
                if idx < len(positions):
                    all_args.setdefault(positions[idx], value)
        assert all_args.get("from_x") == 10
        assert all_args.get("to_x") == 100
        assert all_args.get("speed_ms") == 300
        assert all_args.get("hold_modifiers") == ["ctrl"]
        assert result["dragged"] is True
        assert result["duration_ms"] == 300

    @pytest.mark.asyncio
    async def test_drag_path_returns_blocked_for_release_critical_routes(self):
        backend = PywinautoBackend()
        result = await backend.drag_path(
            [
                {"x": 10, "y": 20},
                {"x": 10, "y": 180, "hold_ms": 650},
                {"x": 40, "y": 220},
            ],
            speed_ms=700,
            hold_modifiers=["shift"],
        )

        assert result["status"] == "BLOCKED"
        assert result["requested"]["capability"] == "path-aware drag"
        assert result["accepted"]["backend"] == "FlaUI drag_path"
        assert "FlaUI bridge" in result["next_step"]


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

    def test_bridge_registers_drag_path_command(self):
        handler = (PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs").read_text(
            encoding="utf-8"
        )

        assert '["drag_path"] = ClickCommands.DragPath' in handler

    def test_bridge_drag_path_validates_route_speed_and_holds(self):
        command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
            encoding="utf-8"
        )

        assert "public static JsonNode DragPath(" in command
        assert "DragPathBlocked(" in command
        assert "drag_path requires at least two points" in command
        assert "drag_path route requires pointer movement" in command
        assert "speed_ms below drag-path safety floor" in command
        assert "hold_ms must be non-negative" in command
        assert "private const int DragPathHoldPulseMs" in command
        assert "PulseHeldDragPoint(point, point.HoldMs);" in command
        assert (
            'TryReadInt(paramObject, "speed_ms", out var requestedSpeedMs)' in command
        )
        assert 'TryReadDragPathCancelKey(@params?["cancel_key"]' in command
        assert "VirtualKeyShort.ESCAPE" in command
        drag_path_body = _csharp_method_body(
            command, "public static JsonNode DragPath("
        )
        assert "Thread.Sleep(PointerMoveSettleMs);" in drag_path_body
        assert "Thread.Sleep(Math.Max(FinalDropSettleMs, delayMs));" in drag_path_body
        assert "SendDragPathCancel(cancelKey.Value);" in drag_path_body
        assert (
            'output["cancel"] = new JsonObject { ["key"] = "escape", ["sent"] = true };'
            in drag_path_body
        )
        assert 'output["no_op"] = new JsonObject' in drag_path_body
        assert '["reason"] = "cancelled"' in drag_path_body
        assert '["route_attempted"] = true' in drag_path_body

    def test_bridge_simple_drag_returns_route_evidence(self):
        command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
            encoding="utf-8"
        )
        drag_body = _csharp_method_body(command, "public static JsonNode Drag(")

        assert "BuildDragWaypoints(x1, y1, x2, y2, steps)" in drag_body
        assert "Thread.Sleep(PointerMoveSettleMs);" in drag_body
        assert '["path_points"] = DragPointsJson' in drag_body
        assert '["final_pointer"] = DragPointJson(new Point(x2, y2))' in drag_body

    def test_bridge_drag_path_releases_pointer_and_modifiers_on_exceptions(self):
        command = (PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs").read_text(
            encoding="utf-8"
        )
        drag_path_body = _csharp_method_body(
            command, "public static JsonNode DragPath("
        )

        assert "mouseButtonDown" in drag_path_body
        assert "mouse_event(MOUSEEVENTF_LEFTDOWN" in drag_path_body
        assert "mouse_event(MOUSEEVENTF_LEFTUP" in drag_path_body
        assert (
            "KeySequenceCommands.SendSignedKeyUp(pressedTemporaryModifiers[i])"
            in drag_path_body
        )
        assert "finally" in drag_path_body


class TestInputProvenanceSignature:
    def test_python_input_producers_stamp_runner_signature(self):
        from netcoredbg_mcp.ui.input_signature import RUNNER_INPUT_SIGNATURE

        assert RUNNER_INPUT_SIGNATURE == 0x4E434442
        automation = (
            PROJECT_ROOT / "src" / "netcoredbg_mcp" / "ui" / "automation.py"
        ).read_text(encoding="utf-8")

        assert "RUNNER_INPUT_SIGNATURE" in automation
        assert "_runner_input_extra_info()" in automation
        assert "inp._input.ki.dwExtraInfo = _runner_input_extra_info()" in automation
        assert (
            "inputs[0]._input.mi.dwExtraInfo = _runner_input_extra_info()" in automation
        )
        assert (
            "inputs[1]._input.mi.dwExtraInfo = _runner_input_extra_info()" in automation
        )
        assert (
            "mouse_event(mouseeventf_leftdown, 0, 0, 0, RUNNER_INPUT_SIGNATURE)"
            in automation
        )
        assert (
            "mouse_event(mouseeventf_leftup, 0, 0, 0, RUNNER_INPUT_SIGNATURE)"
            in automation
        )

    def test_python_sendinput_producers_emit_signature_value(self, monkeypatch):
        from ctypes import wintypes

        from netcoredbg_mcp.ui import automation
        from netcoredbg_mcp.ui.input_signature import RUNNER_INPUT_SIGNATURE

        captured: list[tuple[str, int]] = []

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class KeybdInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeybdInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("_input", InputUnion)]

        class FakeUser32:
            def SetCursorPos(self, _x: int, _y: int) -> bool:  # noqa: N802
                return True

            def SendInput(self, n_inputs: int, p_inputs: object, _cb_size: int) -> int:  # noqa: N802
                inputs = ctypes.cast(p_inputs, ctypes.POINTER(Input))
                for index in range(n_inputs):
                    item = inputs[index]
                    if item.type == 1:
                        captured.append(("keyboard", int(item._input.ki.dwExtraInfo)))
                    else:
                        captured.append(("mouse", int(item._input.mi.dwExtraInfo)))
                return n_inputs

            def mouse_event(
                self, _flags: int, _dx: int, _dy: int, _data: int, extra_info: int
            ) -> None:
                captured.append(("mouse_event", int(extra_info)))

        class FakeWindll:
            user32 = FakeUser32()

        monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)

        automation._press(automation.VK_SHIFT)
        automation._release(automation.VK_SHIFT)
        automation._send_click(10, 20)
        automation._send_drag(0, 0, 20, 20, speed_ms=20)

        assert captured
        assert {value for _kind, value in captured} == {RUNNER_INPUT_SIGNATURE}

    def test_bridge_input_producers_stamp_runner_signature(self):
        signature = (
            PROJECT_ROOT / "bridge" / "Commands" / "InputSignature.cs"
        ).read_text(encoding="utf-8")
        click_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "ClickCommands.cs"
        ).read_text(encoding="utf-8")
        grid_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "GridCommands.DragRowToRow.cs"
        ).read_text(encoding="utf-8")
        selection_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "SelectionCommands.cs"
        ).read_text(encoding="utf-8")
        key_sequence_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "KeySequenceCommands.cs"
        ).read_text(encoding="utf-8")

        assert "RunnerInputSignature" in signature
        for body in (
            _csharp_method_body(click_commands, "public static JsonNode Drag("),
            _csharp_method_body(click_commands, "public static JsonNode DragPath("),
            _csharp_method_body(click_commands, "internal static void MoveCursor("),
            _csharp_method_body(
                click_commands, "private static void SignedMouseClick("
            ),
            _csharp_method_body(grid_commands, "public static JsonNode DragRowToRow("),
        ):
            assert "InputSignature.RunnerInputSignature" in body
            assert "UIntPtr.Zero" not in body
        key_body = _csharp_method_body(
            key_sequence_commands, "private static void SendKey("
        )
        assert "InputSignature.RunnerInputSignatureIntPtr" in key_body
        assert "IntPtr.Zero" not in key_body
        assert "internal static void SendSignedKeyDown" in key_sequence_commands
        assert "internal static void SendSignedKeyUp" in key_sequence_commands
        for body in (
            _csharp_method_body(click_commands, "public static JsonNode Drag("),
            _csharp_method_body(click_commands, "public static JsonNode DragPath("),
            _csharp_method_body(
                click_commands, "private static void SendDragPathCancel("
            ),
            _csharp_method_body(grid_commands, "public static JsonNode DragRowToRow("),
        ):
            assert "KeySequenceCommands.SendSignedKeyDown" in body
            assert "KeySequenceCommands.SendSignedKeyUp" in body
            assert "Keyboard.Press" not in body
            assert "Keyboard.Release" not in body
        assert (
            "KeySequenceCommands.SendSignedKeyDown(VirtualKeyShort.CONTROL);"
            in selection_commands
        )
        assert (
            "KeySequenceCommands.SendSignedKeyUp(VirtualKeyShort.CONTROL);"
            in selection_commands
        )
        for command_file in (PROJECT_ROOT / "bridge" / "Commands").glob("*.cs"):
            command_text = command_file.read_text(encoding="utf-8")
            assert "Keyboard.Press" not in command_text
            assert "Keyboard.Release" not in command_text
        assert "SignedLeftClick(new Point(x.Value, y.Value));" in click_commands
        assert "SignedRightClick(new Point(x, y));" in click_commands
        assert "SignedDoubleClick(new Point(x, y));" in click_commands
        assert "SignedLeftClick(center);" in click_commands
        assert "ClickCommands.SignedLeftClick(center);" in selection_commands

    def test_bridge_input_commands_sign_literal_text_paths(self):
        input_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "InputCommands.cs"
        ).read_text(encoding="utf-8")

        assert "Keyboard.Type(" not in input_commands
        assert "KeySequenceCommands.SendSignedText(" in input_commands

    def test_bridge_input_commands_preserve_modifier_shortcuts_with_signed_keys(self):
        input_commands = (
            PROJECT_ROOT / "bridge" / "Commands" / "InputCommands.cs"
        ).read_text(encoding="utf-8")

        assert "TypeToken(ctrlTarget, preserveModifierShortcut: true)" in input_commands
        assert "TypeToken(altTarget, preserveModifierShortcut: true)" in input_commands
        assert (
            "TypeToken(shiftTarget, preserveModifierShortcut: true)" in input_commands
        )
        assert "TryParseLiteralVirtualKey" in input_commands

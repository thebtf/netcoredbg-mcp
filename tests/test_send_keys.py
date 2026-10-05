"""Tests for _send_keys_via_input key parser and SendInput dispatch."""

from __future__ import annotations

import ctypes
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_user32():
    """Mock ctypes.windll.user32 for testing on any platform."""
    mock = MagicMock()

    # VkKeyScanW: return vk_code in low byte, shift flag in high byte
    # Default: 'a' -> 0x41, no shift
    def vk_scan(char_code):
        ch = chr(char_code)
        if ch.isupper():
            return ord(ch) | 0x100  # needs shift
        if ch.isalpha():
            return ord(ch.upper())
        if ch == "!":
            return 0x31 | 0x100  # '1' + shift
        if ch.isdigit():
            return ord(ch)
        return -1  # unmapped

    mock.VkKeyScanW.side_effect = vk_scan
    mock.SendInput.return_value = 1
    return mock


@pytest.fixture
def send_keys(mock_user32):
    """Import _send_keys_via_input with mocked user32."""
    # We need to patch ctypes.windll.user32 before importing
    # Since the function imports ctypes inside, we patch at module level
    windll_mock = MagicMock()
    windll_mock.user32 = mock_user32

    with patch.dict(sys.modules, {}):
        import ctypes

        original_windll = getattr(ctypes, "windll", None)
        try:
            ctypes.windll = windll_mock
            # Re-import to pick up mock
            from netcoredbg_mcp.ui.automation import _send_keys_via_input

            yield _send_keys_via_input
        finally:
            if original_windll is not None:
                ctypes.windll = original_windll


def _keyboard_events(send_keys, mock_user32, sequence):
    events = []

    def capture(_count, input_pointer, _size):
        event = input_pointer._obj
        key = event._input.ki
        events.append((event.type, key.wVk, key.wScan, key.dwFlags))
        return 1

    mock_user32.SendInput.side_effect = capture
    send_keys(sequence)
    return events


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
@pytest.mark.parametrize(
    ("name", "vk", "scan", "extended"),
    [
        *(
            (f"NUMPAD{digit}", 0x60 + digit, code, False)
            for digit, code in enumerate(
                (0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49)
            )
        ),
        ("NUMPADADD", 0x6B, 0x4E, False),
        ("NUMPADSUBTRACT", 0x6D, 0x4A, False),
        ("NUMPADMULTIPLY", 0x6A, 0x37, False),
        ("NUMPADDIVIDE", 0x6F, 0x35, True),
        ("NUMPADDECIMAL", 0x6E, 0x53, False),
        ("NUMPADENTER", 0x0D, 0x1C, True),
        ("NUMLOCK", 0x90, 0x45, None),
    ],
)
def test_keypad_tokens_emit_physical_down_and_up(send_keys, mock_user32, name, vk, scan, extended):
    events = _keyboard_events(send_keys, mock_user32, f"{{{name}}}")

    assert len(events) == 2
    for index, (input_type, actual_vk, actual_scan, flags) in enumerate(events):
        assert input_type == 1  # INPUT_KEYBOARD
        if flags & 0x0008:  # KEYEVENTF_SCANCODE: wVk is ignored
            assert actual_scan == scan
            assert actual_vk in (0, vk)
        else:
            assert actual_vk == vk
            assert name != "NUMPADENTER"  # VK_RETURN alone cannot distinguish the two Enter keys
        if extended is not None:
            assert bool(flags & 0x0001) is extended  # KEYEVENTF_EXTENDEDKEY (E0)
        assert bool(flags & 0x0002) is bool(index)  # KEYEVENTF_KEYUP
        assert not flags & 0x0004  # KEYEVENTF_UNICODE


@pytest.mark.skipif(
    sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8,
    reason="64-bit Windows SendInput layout",
)
def test_keypad_and_modifier_events_use_native_input_size(send_keys, mock_user32):
    from netcoredbg_mcp.ui.automation import _press, _release

    send_keys("{NUMPAD1}")
    _press(0x11)
    _release(0x11)

    assert mock_user32.SendInput.call_count == 4
    for call in mock_user32.SendInput.call_args_list:
        assert call.args[2] == 40  # Win64 INPUT includes the 32-byte MOUSEINPUT union arm.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_keypad_events_differ_from_text_digit_and_standard_enter(send_keys, mock_user32):
    keypad_digit = _keyboard_events(send_keys, mock_user32, "{NUMPAD1}")
    top_row_digit = _keyboard_events(send_keys, mock_user32, "1")
    keypad_enter = _keyboard_events(send_keys, mock_user32, "{NUMPADENTER}")
    ordinary_enter = _keyboard_events(send_keys, mock_user32, "{ENTER}")

    assert keypad_digit[0][1] in (0, 0x61)
    assert top_row_digit[0][1] == 0x31
    assert (top_row_digit[0][1], top_row_digit[0][2], top_row_digit[0][3] & 0x0009) != (
        keypad_digit[0][1],
        keypad_digit[0][2],
        keypad_digit[0][3] & 0x0009,
    )
    assert keypad_enter[0][2] == 0x1C
    assert keypad_enter[0][3] & 0x0009 == 0x0009
    assert not ordinary_enter[0][3] & 0x0001
    assert ordinary_enter[0][2:] != keypad_enter[0][2:]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_keypad_tokens_keep_modifier_and_concatenation_order(send_keys, mock_user32):
    events = _keyboard_events(send_keys, mock_user32, "^{NUMPAD1}{NUMPAD2}")

    assert [flags & 0x0002 for _, _, _, flags in events] == [0, 0, 2, 2, 0, 2]
    assert [vk for _, vk, _, _ in (events[0], events[3])] == [0x11, 0x11]
    assert [vk for _, vk, _, _ in (events[1], events[2])] in ([0x61, 0x61], [0, 0])
    assert [vk for _, vk, _, _ in (events[4], events[5])] in ([0x62, 0x62], [0, 0])
    assert all(scan == 0x4F or vk == 0x61 for _, vk, scan, _ in events[1:3])
    assert all(scan == 0x50 or vk == 0x62 for _, vk, scan, _ in events[4:6])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_unknown_keypad_name_fails_without_sending_an_event(send_keys, mock_user32):
    with pytest.raises(ValueError, match="Unknown special key"):
        _keyboard_events(send_keys, mock_user32, "{NUMPADNOTAKEY}")
    mock_user32.SendInput.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
@pytest.mark.parametrize(
    ("token", "vk", "scan", "flags"),
    [
        ("a", 0x41, 0, 0),
        ("{ENTER}", 0x0D, 0, 0),
        ("{NUMPAD1}", 0, 0x4F, 0x0008),
        ("{NUMPADENTER}", 0, 0x1C, 0x0009),
        ("{NUMLOCK}", 0, 0x45, 0x0008),
    ],
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_key_up_attempts_owned_up_only_cleanup(
    send_keys, mock_user32, token, vk, scan, flags, cleanup_fails
):
    events = []
    key_up = (1, vk, scan, flags | 0x0002)

    def capture(_count, input_pointer, _size):
        event = input_pointer._obj
        key = event._input.ki
        current = (event.type, key.wVk, key.wScan, key.dwFlags)
        events.append(current)
        if current == key_up and (events.count(key_up) == 1 or cleanup_fails):
            return 0
        return 1

    mock_user32.SendInput.side_effect = capture
    with (
        patch("ctypes.windll.kernel32.GetLastError", side_effect=[5, 87]) as last_error,
        patch("time.sleep"),
        pytest.raises(OSError, match="SendInput failed") as error,
    ):
        send_keys("^+" + token)

    assert error.value.errno == 5
    assert events == [
        (1, 0x11, 0, 0),
        (1, 0x10, 0, 0),
        (1, vk, scan, flags),
        key_up,
        key_up,
        (1, 0x10, 0, 0x0002),
        (1, 0x11, 0, 0x0002),
    ]
    assert last_error.call_count == (2 if cleanup_fails else 1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
@pytest.mark.parametrize("sequence", ["^+a", "^+(a)"])
def test_failed_shift_release_still_attempts_ctrl_release(send_keys, mock_user32, sequence):
    events = []

    def capture(_count, input_pointer, _size):
        event = input_pointer._obj
        key = event._input.ki
        events.append((event.type, key.wVk, key.dwFlags))
        return 0 if key.wVk == 0x10 and key.dwFlags & 0x0002 else 1

    mock_user32.SendInput.side_effect = capture
    ctypes.windll.kernel32.GetLastError.return_value = 5

    with pytest.raises(OSError, match="SendInput failed") as error:
        send_keys(sequence)

    assert error.value.errno == 5
    assert events == [
        (1, 0x11, 0),
        (1, 0x10, 0),
        (1, 0x41, 0),
        (1, 0x41, 0x0002),
        (1, 0x10, 0x0002),
        (1, 0x11, 0x0002),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_drag_failed_shift_release_still_attempts_ctrl_release(send_keys, mock_user32):
    from netcoredbg_mcp.ui.automation import _send_drag

    events = []

    def capture(_count, input_pointer, _size):
        event = input_pointer._obj
        key = event._input.ki
        events.append((event.type, key.wVk, key.dwFlags))
        return 0 if key.wVk == 0x10 and key.dwFlags & 0x0002 else 1

    mock_user32.SendInput.side_effect = capture
    ctypes.windll.kernel32.GetLastError.return_value = 5

    with pytest.raises(OSError, match="SendInput failed") as error:
        _send_drag(10, 20, 30, 40, speed_ms=20, hold_modifiers=["ctrl", "shift"])

    assert error.value.errno == 5
    assert events == [
        (1, 0x11, 0),
        (1, 0x10, 0),
        (1, 0x10, 0x0002),
        (1, 0x11, 0x0002),
    ]
    assert [call.args[0] for call in mock_user32.mouse_event.call_args_list] == [0x0002, 0x0004]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_group_keypad_escape_sequence_preserves_event_and_pause_order(send_keys, mock_user32):
    events = []
    scans = {ord("a"): 0x41, ord("b"): 0x42, ord("}"): 0xDD | 0x100, ord("c"): 0x43}
    mock_user32.VkKeyScanW.side_effect = scans.__getitem__

    def capture(_count, input_pointer, _size):
        key = input_pointer._obj._input.ki
        events.append(("key", key.wVk, key.wScan, key.dwFlags))
        return 1

    mock_user32.SendInput.side_effect = capture
    with patch("time.sleep", side_effect=lambda delay: events.append(("sleep", delay))):
        send_keys("^+(ab){NUMPAD1}^{}}c")

    assert events == [
        ("key", 0x11, 0, 0),
        ("sleep", 0.01),
        ("key", 0x10, 0, 0),
        ("sleep", 0.01),
        ("key", 0x41, 0, 0),
        ("sleep", 0.01),
        ("key", 0x41, 0, 0x0002),
        ("sleep", 0.02),
        ("key", 0x42, 0, 0),
        ("sleep", 0.01),
        ("key", 0x42, 0, 0x0002),
        ("sleep", 0.02),
        ("key", 0x10, 0, 0x0002),
        ("sleep", 0.01),
        ("key", 0x11, 0, 0x0002),
        ("sleep", 0.01),
        ("sleep", 0.02),
        ("key", 0, 0x4F, 0x0008),
        ("key", 0, 0x4F, 0x000A),
        ("sleep", 0.02),
        ("key", 0x11, 0, 0),
        ("sleep", 0.01),
        ("key", 0x10, 0, 0),
        ("sleep", 0.01),
        ("key", 0xDD, 0, 0),
        ("sleep", 0.01),
        ("key", 0xDD, 0, 0x0002),
        ("key", 0x10, 0, 0x0002),
        ("sleep", 0.01),
        ("key", 0x11, 0, 0x0002),
        ("sleep", 0.01),
        ("key", 0x43, 0, 0),
        ("sleep", 0.01),
        ("key", 0x43, 0, 0x0002),
        ("sleep", 0.02),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
@pytest.mark.parametrize(
    ("sequence", "message", "expected"),
    [
        ("^+{UNKNOWN}", "Unknown special key", [(0x11, 0), (0x10, 0), (0x10, 2), (0x11, 2)]),
        ("^+{ENTER", "Unclosed brace", [(0x11, 0), (0x10, 0), (0x10, 2), (0x11, 2)]),
        ("^+(ab", "Unclosed parenthesis", []),
    ],
)
def test_malformed_modified_keys_keep_cleanup_order(
    send_keys, mock_user32, sequence, message, expected
):
    events = []

    def capture(_count, input_pointer, _size):
        key = input_pointer._obj._input.ki
        events.append((key.wVk, key.dwFlags))
        return 1

    mock_user32.SendInput.side_effect = capture
    with patch("time.sleep"), pytest.raises(ValueError, match=message):
        send_keys(sequence)
    assert events == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
def test_cleanup_error_keeps_precedence_over_parse_error(send_keys, mock_user32):
    events = []

    def capture(_count, input_pointer, _size):
        key = input_pointer._obj._input.ki
        events.append((key.wVk, key.dwFlags))
        return 0 if key.dwFlags & 0x0002 else 1

    mock_user32.SendInput.side_effect = capture
    with (
        patch("ctypes.windll.kernel32.GetLastError", side_effect=[5, 6]),
        patch("time.sleep"),
        pytest.raises(OSError, match="SendInput failed") as error,
    ):
        send_keys("^+{UNKNOWN}")

    assert error.value.errno == 5
    assert events == [(0x11, 0), (0x10, 0), (0x10, 2), (0x11, 2)]


class TestSendKeysParser:
    """Test key sequence parsing logic."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_simple_character(self, send_keys, mock_user32):
        """Single character 'a' should call SendInput for key down + up."""
        send_keys("a")
        # Should have called VkKeyScanW for 'a'
        mock_user32.VkKeyScanW.assert_called()
        # Should have called SendInput multiple times (down, up)
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_special_key_enter(self, send_keys, mock_user32):
        """Special key {ENTER} should send VK_RETURN (0x0D)."""
        send_keys("{ENTER}")
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_modifier_alt_z(self, send_keys, mock_user32):
        """Alt+Z (%z) should press Alt, tap Z, release Alt."""
        send_keys("%z")
        # Should call SendInput for: Alt down, Z down, Z up, Alt up
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_modifier_ctrl_shift(self, send_keys, mock_user32):
        """+^a (Shift+Ctrl+A) should press both modifiers."""
        send_keys("+^a")
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_unclosed_brace_raises(self, send_keys):
        """Unclosed brace {ENTER should raise ValueError."""
        with pytest.raises(ValueError, match="Unclosed brace"):
            send_keys("{ENTER")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_unknown_special_key_raises(self, send_keys):
        """Unknown special key {FOO} should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown special key"):
            send_keys("{FOO}")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_grouped_modifiers(self, send_keys, mock_user32):
        """Grouped ^(abc) should hold Ctrl for all three characters."""
        send_keys("^(abc)")
        # Should call SendInput for: Ctrl down, a down/up, b down/up, c down/up, Ctrl up
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_unclosed_paren_raises(self, send_keys):
        """Unclosed parenthesis ^(abc should raise ValueError."""
        with pytest.raises(ValueError, match="Unclosed parenthesis"):
            send_keys("^(abc")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_empty_string(self, send_keys, mock_user32):
        """Empty string should not call SendInput."""
        send_keys("")
        mock_user32.SendInput.assert_not_called()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_multiple_special_keys(self, send_keys, mock_user32):
        """{TAB}{ENTER} should send both keys."""
        send_keys("{TAB}{ENTER}")
        assert mock_user32.SendInput.call_count >= 2

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_literal_escaped_sendkeys_tokens(self, send_keys, mock_user32):
        """Escaped modifier/special tokens should type literal text."""
        send_keys("A{+}{^}{%}{{}{}}{(}{)}{~}")
        assert mock_user32.SendInput.call_count >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput")
    def test_ctrl_end(self, send_keys, mock_user32):
        """^{END} should press Ctrl, tap End, release Ctrl."""
        send_keys("^{END}")
        assert mock_user32.SendInput.call_count >= 1

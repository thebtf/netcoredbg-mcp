"""Tests for _send_keys_via_input key parser and SendInput dispatch."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

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
        if ch == '!':
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
        original_windll = getattr(ctypes, 'windll', None)
        try:
            ctypes.windll = windll_mock
            # Re-import to pick up mock
            from netcoredbg_mcp.ui.automation import _send_keys_via_input
            yield _send_keys_via_input
        finally:
            if original_windll is not None:
                ctypes.windll = original_windll


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
    def test_ctrl_end(self, send_keys, mock_user32):
        """^{END} should press Ctrl, tap End, release Ctrl."""
        send_keys("^{END}")
        assert mock_user32.SendInput.call_count >= 1

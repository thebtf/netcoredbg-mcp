"""Tests for SessionTempManager."""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from netcoredbg_mcp.ui.temp_manager import SessionTempManager, TEMP_PREFIX


class TestSessionTempManager:
    """Unit tests for session temp file management."""

    def test_get_session_dir_creates_dir(self, tmp_path):
        mgr = SessionTempManager()
        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_dir = tmp_path / "mcp-netcoredbg-test123-abc"
            mock_dir.mkdir()
            mock_tf.mkdtemp.return_value = str(mock_dir)

            result = mgr.get_session_dir("test123")

            assert result is not None
            assert result == mock_dir
            mock_tf.mkdtemp.assert_called_once()

    def test_get_session_dir_generates_uuid_when_none(self):
        mgr = SessionTempManager()
        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            with patch("netcoredbg_mcp.ui.temp_manager.uuid") as mock_uuid:
                mock_uuid.uuid4.return_value.hex = "abcdef123456abcdef"
                mock_tf.mkdtemp.return_value = "/tmp/mcp-netcoredbg-abcdef123456-xyz"

                mgr.get_session_dir(None)

                call_args = mock_tf.mkdtemp.call_args
                assert "abcdef123456" in call_args.kwargs.get("prefix", call_args[1].get("prefix", ""))

    def test_get_session_dir_returns_cached(self, tmp_path):
        mgr = SessionTempManager()
        mock_dir = tmp_path / "session-dir"
        mock_dir.mkdir()

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.mkdtemp.return_value = str(mock_dir)
            first = mgr.get_session_dir("sess1")
            second = mgr.get_session_dir("sess1")

        assert first == second
        # mkdtemp should only be called once
        assert mock_tf.mkdtemp.call_count == 1

    def test_get_session_dir_returns_none_on_failure(self):
        mgr = SessionTempManager()
        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.mkdtemp.side_effect = OSError("Read-only filesystem")
            result = mgr.get_session_dir("fail")
            assert result is None

    def test_save_screenshot(self, tmp_path):
        mgr = SessionTempManager()
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.mkdtemp.return_value = str(session_dir)
            path = mgr.save_screenshot("s1", b"fake-png-data", "shot.webp")

        assert path is not None
        assert path.name == "shot.webp"
        assert path.read_bytes() == b"fake-png-data"

    def test_save_screenshot_returns_none_when_no_dir(self):
        mgr = SessionTempManager()
        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.mkdtemp.side_effect = OSError("no space")
            result = mgr.save_screenshot("s1", b"data", "shot.webp")
            assert result is None

    def test_cleanup_session(self, tmp_path):
        mgr = SessionTempManager()
        session_dir = tmp_path / "to-clean"
        session_dir.mkdir()
        (session_dir / "file.txt").write_text("test")

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.mkdtemp.return_value = str(session_dir)
            mgr.get_session_dir("clean-me")

        mgr.cleanup_session("clean-me")
        assert not session_dir.exists()

    def test_cleanup_session_nonexistent_is_safe(self):
        mgr = SessionTempManager()
        # Should not raise
        mgr.cleanup_session("nonexistent")

    def test_cleanup_all(self, tmp_path):
        mgr = SessionTempManager()

        dirs = []
        for i in range(3):
            d = tmp_path / f"session-{i}"
            d.mkdir()
            dirs.append(d)

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            for i, d in enumerate(dirs):
                mock_tf.mkdtemp.return_value = str(d)
                mgr.get_session_dir(f"s{i}")

        mgr.cleanup_all()
        for d in dirs:
            assert not d.exists()

    def test_gc_stale_removes_old_dirs(self, tmp_path):
        # Create a fake stale dir
        stale_dir = tmp_path / f"{TEMP_PREFIX}old-session-abc"
        stale_dir.mkdir()
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        import os
        os.utime(stale_dir, (old_time, old_time))

        # Create a recent dir
        fresh_dir = tmp_path / f"{TEMP_PREFIX}new-session-xyz"
        fresh_dir.mkdir()

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.gettempdir.return_value = str(tmp_path)
            removed = SessionTempManager.gc_stale(max_age_hours=1.0)

        assert removed == 1
        assert not stale_dir.exists()
        assert fresh_dir.exists()

    def test_gc_stale_keeps_recent_dirs(self, tmp_path):
        recent_dir = tmp_path / f"{TEMP_PREFIX}recent-abc"
        recent_dir.mkdir()

        with patch("netcoredbg_mcp.ui.temp_manager.tempfile") as mock_tf:
            mock_tf.gettempdir.return_value = str(tmp_path)
            removed = SessionTempManager.gc_stale(max_age_hours=1.0)

        assert removed == 0
        assert recent_dir.exists()

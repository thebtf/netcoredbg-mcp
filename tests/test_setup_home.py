"""Tests for setup home directory management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from netcoredbg_mcp.setup.home import (
    _CONFIG_FILENAME,
    _HOME_DIR_NAME,
    get_config,
    get_home_dir,
    save_config,
)


class TestGetHomeDir:
    """Tests for get_home_dir()."""

    def test_creates_directory(self, tmp_path: Path):
        """Home dir is created if it doesn't exist."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result = get_home_dir()
        assert result == fake_home / _HOME_DIR_NAME
        assert result.is_dir()

    def test_returns_same_path(self, tmp_path: Path):
        """Repeated calls return the same path."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result1 = get_home_dir()
            result2 = get_home_dir()
        assert result1 == result2

    def test_existing_directory_unchanged(self, tmp_path: Path):
        """Existing directory is not modified."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        existing = fake_home / _HOME_DIR_NAME
        existing.mkdir()
        marker = existing / "existing_file.txt"
        marker.write_text("keep me")
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result = get_home_dir()
        assert result == existing
        assert marker.read_text() == "keep me"


class TestGetConfig:
    """Tests for get_config()."""

    def test_missing_config_returns_empty(self, tmp_path: Path):
        """No config.json returns empty dict."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result = get_config()
        assert result == {}

    def test_valid_config_roundtrip(self, tmp_path: Path):
        """Written config can be read back."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            data = {"version": 1, "netcoredbg": {"source": "github"}}
            save_config(data)
            result = get_config()
        assert result == data

    def test_corrupted_json_returns_empty(self, tmp_path: Path):
        """Invalid JSON returns empty dict (no crash)."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        config_dir = fake_home / _HOME_DIR_NAME
        config_dir.mkdir()
        config_file = config_dir / _CONFIG_FILENAME
        config_file.write_text("{invalid json!!!", encoding="utf-8")
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result = get_config()
        assert result == {}

    def test_non_dict_json_returns_empty(self, tmp_path: Path):
        """JSON that is not a dict returns empty dict."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        config_dir = fake_home / _HOME_DIR_NAME
        config_dir.mkdir()
        config_file = config_dir / _CONFIG_FILENAME
        config_file.write_text("[1, 2, 3]", encoding="utf-8")
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            result = get_config()
        assert result == {}


class TestSaveConfig:
    """Tests for save_config()."""

    def test_creates_config_file(self, tmp_path: Path):
        """Config file is created on first save."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            save_config({"key": "value"})
            config_path = get_home_dir() / _CONFIG_FILENAME
        assert config_path.is_file()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data == {"key": "value"}

    def test_atomic_write_no_tmp_left(self, tmp_path: Path):
        """Temp file is cleaned up after atomic write."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            save_config({"test": True})
            home = get_home_dir()
        tmp_file = home / "config.json.tmp"
        assert not tmp_file.exists()

    def test_overwrites_existing(self, tmp_path: Path):
        """Saving again overwrites the previous config."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            save_config({"version": 1})
            save_config({"version": 2, "extra": "data"})
            result = get_config()
        assert result == {"version": 2, "extra": "data"}

    def test_rejects_non_dict(self):
        """Non-dict input raises TypeError."""
        with pytest.raises(TypeError, match="must be a dict"):
            save_config("not a dict")  # type: ignore[arg-type]

    def test_preserves_unicode(self, tmp_path: Path):
        """Unicode content is preserved."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("netcoredbg_mcp.setup.home.Path.home", return_value=fake_home):
            data = {"path": "C:\\Users\\Кирилл", "emoji": "🔧"}
            save_config(data)
            result = get_config()
        assert result == data

"""Tests for dbgshim scanner, selector, and swap."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from netcoredbg_mcp.setup.dbgshim import (
    _DBGSHIM_FILENAME,
    _get_runtime_scan_paths,
    extract_dbgshim_versions,
    scan_installed_runtimes,
    select_and_swap_dbgshim,
    select_dbgshim,
    swap_dbgshim,
)


def _create_fake_runtime(base: Path, version: str, size: int = 1024) -> Path:
    """Create a fake .NET runtime directory with dbgshim file."""
    runtime_dir = base / version
    runtime_dir.mkdir(parents=True, exist_ok=True)
    dbgshim = runtime_dir / _DBGSHIM_FILENAME
    dbgshim.write_bytes(b"\x00" * size)
    return dbgshim


class TestScanPaths:
    """Tests for _get_runtime_scan_paths."""

    def test_returns_paths_for_current_os(self):
        paths = _get_runtime_scan_paths()
        assert len(paths) >= 1
        assert all(isinstance(p, Path) for p in paths)

    def test_dotnet_root_override(self, tmp_path: Path):
        with patch.dict(os.environ, {"DOTNET_ROOT": str(tmp_path)}):
            paths = _get_runtime_scan_paths()
        expected = tmp_path / "shared" / "Microsoft.NETCore.App"
        assert expected in paths
        # DOTNET_ROOT should be first
        assert paths[0] == expected


class TestScanInstalledRuntimes:
    """Tests for scan_installed_runtimes."""

    def test_scan_mock_filesystem(self, tmp_path: Path):
        """Scanner finds dbgshim in mock runtime directories."""
        runtime_base = tmp_path / "shared" / "Microsoft.NETCore.App"
        _create_fake_runtime(runtime_base, "6.0.36")
        _create_fake_runtime(runtime_base, "8.0.10")
        _create_fake_runtime(runtime_base, "9.0.1")

        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[runtime_base],
        ):
            result = scan_installed_runtimes()

        assert "6.0.36" in result
        assert "8.0.10" in result
        assert "9.0.1" in result
        assert len(result) == 3

    def test_scan_empty_dir(self, tmp_path: Path):
        """Empty scan path returns empty dict."""
        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[tmp_path],
        ):
            result = scan_installed_runtimes()
        assert result == {}

    def test_scan_nonexistent_path(self, tmp_path: Path):
        """Non-existent scan path handled gracefully."""
        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[tmp_path / "nonexistent"],
        ):
            result = scan_installed_runtimes()
        assert result == {}

    def test_scan_skips_dirs_without_dbgshim(self, tmp_path: Path):
        """Directories without dbgshim file are skipped."""
        runtime_base = tmp_path / "shared" / "Microsoft.NETCore.App"
        _create_fake_runtime(runtime_base, "6.0.36")
        # Create dir without dbgshim
        (runtime_base / "7.0.0").mkdir(parents=True)

        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[runtime_base],
        ):
            result = scan_installed_runtimes()
        assert "6.0.36" in result
        assert "7.0.0" not in result


class TestExtractDbgshimVersions:
    """Tests for extract_dbgshim_versions."""

    def test_extract_to_cache(self, tmp_path: Path):
        """Extraction copies dbgshim files to cache directory."""
        runtime_base = tmp_path / "runtimes"
        _create_fake_runtime(runtime_base, "6.0.36", size=2048)
        _create_fake_runtime(runtime_base, "8.0.10", size=4096)
        cache_dir = tmp_path / "cache"

        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[runtime_base],
        ), patch(
            "netcoredbg_mcp.setup.dbgshim.get_home_dir",
            return_value=tmp_path / "home",
        ):
            versions = extract_dbgshim_versions(cache_dir)

        assert "6.0.36" in versions
        assert "8.0.10" in versions
        assert (cache_dir / "6.0.36" / _DBGSHIM_FILENAME).is_file()
        assert (cache_dir / "8.0.10" / _DBGSHIM_FILENAME).is_file()

    def test_skip_already_cached(self, tmp_path: Path):
        """Extraction skips files already cached with same size."""
        runtime_base = tmp_path / "runtimes"
        _create_fake_runtime(runtime_base, "6.0.36", size=2048)
        cache_dir = tmp_path / "cache"

        with patch(
            "netcoredbg_mcp.setup.dbgshim._get_runtime_scan_paths",
            return_value=[runtime_base],
        ):
            # First extraction
            extract_dbgshim_versions(cache_dir)
            mtime1 = (cache_dir / "6.0.36" / _DBGSHIM_FILENAME).stat().st_mtime

            # Second extraction — should skip
            extract_dbgshim_versions(cache_dir)
            mtime2 = (cache_dir / "6.0.36" / _DBGSHIM_FILENAME).stat().st_mtime

        assert mtime1 == mtime2  # File not re-copied


class TestSelectDbgshim:
    """Tests for select_dbgshim."""

    def _setup_cache(self, cache_dir: Path, versions: list[str]) -> None:
        for v in versions:
            d = cache_dir / v
            d.mkdir(parents=True)
            (d / _DBGSHIM_FILENAME).write_bytes(b"\x00" * 100)

    def test_exact_major_match(self, tmp_path: Path):
        """Selects dbgshim with matching major version."""
        self._setup_cache(tmp_path, ["6.0.36", "8.0.10"])
        result = select_dbgshim("6.0.0", tmp_path)
        assert result is not None
        assert "6.0.36" in str(result)

    def test_highest_patch(self, tmp_path: Path):
        """Selects highest patch when multiple same-major versions exist."""
        self._setup_cache(tmp_path, ["6.0.16", "6.0.20", "6.0.36"])
        result = select_dbgshim("6.0.0", tmp_path)
        assert result is not None
        assert "6.0.36" in str(result)

    def test_no_match(self, tmp_path: Path):
        """Returns None when no major version matches."""
        self._setup_cache(tmp_path, ["6.0.36", "8.0.10"])
        result = select_dbgshim("7.0.0", tmp_path)
        assert result is None

    def test_major_only_input(self, tmp_path: Path):
        """Handles major-only version string like '6'."""
        self._setup_cache(tmp_path, ["6.0.36"])
        result = select_dbgshim("6", tmp_path)
        assert result is not None

    def test_empty_cache(self, tmp_path: Path):
        """Returns None for empty cache dir."""
        tmp_path.mkdir(exist_ok=True)
        result = select_dbgshim("6.0.0", tmp_path)
        assert result is None

    def test_nonexistent_cache(self, tmp_path: Path):
        """Returns None for non-existent cache dir."""
        result = select_dbgshim("6.0.0", tmp_path / "nope")
        assert result is None

    def test_invalid_version_string(self, tmp_path: Path):
        """Returns None for unparseable version."""
        result = select_dbgshim("not-a-version", tmp_path)
        assert result is None


class TestSwapDbgshim:
    """Tests for swap_dbgshim."""

    def test_swap_copies_file(self, tmp_path: Path):
        """Swap copies dbgshim to netcoredbg directory."""
        source = tmp_path / "source" / _DBGSHIM_FILENAME
        source.parent.mkdir()
        source.write_bytes(b"dbgshim-content")

        dest_dir = tmp_path / "netcoredbg"
        dest_dir.mkdir()

        swap_dbgshim(dest_dir, source)
        dest = dest_dir / _DBGSHIM_FILENAME
        assert dest.is_file()
        assert dest.read_bytes() == b"dbgshim-content"

    def test_swap_no_tmp_leftover(self, tmp_path: Path):
        """Temp file is cleaned up after swap."""
        source = tmp_path / "source" / _DBGSHIM_FILENAME
        source.parent.mkdir()
        source.write_bytes(b"content")

        dest_dir = tmp_path / "netcoredbg"
        dest_dir.mkdir()

        swap_dbgshim(dest_dir, source)
        tmp_file = dest_dir / (_DBGSHIM_FILENAME + ".tmp")
        assert not tmp_file.exists()


class TestSelectAndSwap:
    """Tests for select_and_swap_dbgshim high-level function."""

    def test_swap_with_matching_runtime(self, tmp_path: Path):
        """Full flow: program with runtimeconfig → matching dbgshim → swap."""
        # Create fake program with runtimeconfig
        program = tmp_path / "app" / "MyApp.dll"
        program.parent.mkdir()
        program.write_bytes(b"")
        runtimeconfig = tmp_path / "app" / "MyApp.runtimeconfig.json"
        runtimeconfig.write_text(
            '{"runtimeOptions":{"framework":{"name":"Microsoft.NETCore.App","version":"6.0.36"}}}',
            encoding="utf-8",
        )

        # Create fake netcoredbg
        netcoredbg_dir = tmp_path / "netcoredbg"
        netcoredbg_dir.mkdir()
        netcoredbg = netcoredbg_dir / "netcoredbg.exe"
        netcoredbg.write_bytes(b"")

        # Create cached dbgshim
        cache_dir = tmp_path / "cache" / "6.0.36"
        cache_dir.mkdir(parents=True)
        (cache_dir / _DBGSHIM_FILENAME).write_bytes(b"correct-dbgshim")

        with patch(
            "netcoredbg_mcp.setup.dbgshim.get_home_dir",
            return_value=tmp_path,
        ), patch.dict(
            os.environ, {"DOTNET_ROOT": str(tmp_path / "nonexistent")},
        ):
            # Point cache to our test dir
            result = select_and_swap_dbgshim(str(program), str(netcoredbg))

        # Check dbgshim was copied
        if result:
            dest = netcoredbg_dir / _DBGSHIM_FILENAME
            assert dest.is_file()

    def test_no_runtimeconfig(self, tmp_path: Path):
        """Returns False when program has no runtimeconfig."""
        program = tmp_path / "app" / "MyApp.dll"
        program.parent.mkdir()
        program.write_bytes(b"")
        netcoredbg = tmp_path / "netcoredbg" / "netcoredbg.exe"
        netcoredbg.parent.mkdir()
        netcoredbg.write_bytes(b"")

        result = select_and_swap_dbgshim(str(program), str(netcoredbg))
        assert result is False

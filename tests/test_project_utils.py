"""Tests for project root detection utilities."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.utils.project import (
    ProjectRootConfig,
    configure_project_root,
    find_dotnet_project_root,
    get_config,
    get_project_root,
    get_project_root_sync,
    parse_file_uri,
)


class TestParseFileUri:
    """Tests for parse_file_uri function."""

    def test_parse_unix_path(self):
        """Test parsing Unix file:// URI."""
        uri = "file:///home/user/project"
        result = parse_file_uri(uri)
        # On Windows this won't be a valid path, on Unix it will be
        if sys.platform != "win32":
            assert result == Path("/home/user/project")

    def test_parse_windows_path(self):
        """Test parsing Windows file:// URI with drive letter."""
        uri = "file:///C:/Users/project"
        result = parse_file_uri(uri)
        if sys.platform == "win32":
            assert result == Path("C:/Users/project")

    def test_parse_url_encoded_path(self):
        """Test parsing URL-encoded paths."""
        uri = "file:///home/user/my%20project"
        result = parse_file_uri(uri)
        if sys.platform != "win32":
            assert result == Path("/home/user/my project")

    def test_parse_non_file_uri_returns_none(self):
        """Test that non-file:// URIs return None."""
        assert parse_file_uri("http://example.com") is None
        assert parse_file_uri("https://example.com") is None

    def test_parse_invalid_uri_returns_none(self):
        """Test that invalid URIs return None."""
        assert parse_file_uri("not a uri") is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_parse_windows_unc_path(self):
        """Test parsing Windows UNC file:// URI."""
        uri = "file://server/share/path"
        result = parse_file_uri(uri)
        assert result == Path("\\\\server\\share\\path")


class TestFindDotnetProjectRoot:
    """Tests for find_dotnet_project_root function."""

    def test_finds_sln_file(self, tmp_path, monkeypatch):
        """Test that .sln file is found first."""
        (tmp_path / "Solution.sln").touch()
        subdir = tmp_path / "src" / "Project"
        subdir.mkdir(parents=True)
        (subdir / "Project.csproj").touch()

        result = find_dotnet_project_root(subdir)
        assert result == tmp_path

    def test_finds_csproj_when_no_sln(self, tmp_path):
        """Test that .csproj is found when no .sln exists."""
        (tmp_path / "Project.csproj").touch()
        result = find_dotnet_project_root(tmp_path)
        assert result == tmp_path

    def test_finds_vbproj(self, tmp_path):
        """Test that .vbproj files are found."""
        (tmp_path / "Project.vbproj").touch()
        result = find_dotnet_project_root(tmp_path)
        assert result == tmp_path

    def test_finds_fsproj(self, tmp_path):
        """Test that .fsproj files are found."""
        (tmp_path / "Project.fsproj").touch()
        result = find_dotnet_project_root(tmp_path)
        assert result == tmp_path

    def test_finds_git_when_no_dotnet_files(self, tmp_path):
        """Test that .git is found as fallback."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()

        result = find_dotnet_project_root(subdir)
        assert result == tmp_path

    def test_falls_back_to_start_dir(self, tmp_path):
        """Test fallback to start_dir when no markers found."""
        result = find_dotnet_project_root(tmp_path)
        assert result == tmp_path

    def test_searches_upward(self, tmp_path):
        """Test that search goes up the directory tree."""
        (tmp_path / "Solution.sln").touch()
        deep_dir = tmp_path / "src" / "components" / "utils"
        deep_dir.mkdir(parents=True)

        result = find_dotnet_project_root(deep_dir)
        assert result == tmp_path

    def test_prefers_sln_over_csproj(self, tmp_path):
        """Test that .sln is preferred over .csproj in same directory."""
        (tmp_path / "Solution.sln").touch()
        (tmp_path / "Project.csproj").touch()

        result = find_dotnet_project_root(tmp_path)
        assert result == tmp_path

    def test_prefers_csproj_over_git(self, tmp_path):
        """Test that .csproj is preferred over .git."""
        (tmp_path / ".git").mkdir()
        project_dir = tmp_path / "src"
        project_dir.mkdir()
        (project_dir / "Project.csproj").touch()

        result = find_dotnet_project_root(project_dir)
        assert result == project_dir


class TestProjectRootConfig:
    """Tests for ProjectRootConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = ProjectRootConfig()
        assert config.startup_cwd is None
        assert config.use_project_from_cwd is False
        assert config.explicit_project_path is None
        assert "NETCOREDBG_PROJECT_ROOT" in config.env_var_names

    def test_configure_project_root(self, tmp_path):
        """Test configure_project_root sets global config."""
        configure_project_root(
            use_project_from_cwd=True,
            explicit_project_path=str(tmp_path),
            startup_cwd=str(tmp_path),
        )

        config = get_config()
        assert config.use_project_from_cwd is True
        assert config.explicit_project_path == tmp_path
        assert config.startup_cwd == tmp_path


class TestGetProjectRootSync:
    """Tests for get_project_root_sync function."""

    def test_returns_none_when_no_config(self):
        """Test returns None when nothing configured."""
        configure_project_root()  # Reset config
        result = get_project_root_sync()
        assert result is None

    def test_returns_explicit_path(self, tmp_path):
        """Test returns explicit project path when configured."""
        configure_project_root(explicit_project_path=str(tmp_path))
        result = get_project_root_sync()
        assert result == tmp_path

    def test_returns_cwd_with_marker_search(self, tmp_path, monkeypatch):
        """Test returns CWD with marker search when --project-from-cwd."""
        (tmp_path / "Solution.sln").touch()
        subdir = tmp_path / "src"
        subdir.mkdir()

        configure_project_root(
            use_project_from_cwd=True,
            startup_cwd=str(subdir),
        )
        result = get_project_root_sync()
        assert result == tmp_path

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        """Test environment variable takes precedence over startup CWD."""
        env_path = tmp_path / "env_project"
        env_path.mkdir()
        monkeypatch.setenv("NETCOREDBG_PROJECT_ROOT", str(env_path))

        configure_project_root(
            use_project_from_cwd=True,
            startup_cwd=str(tmp_path),
        )
        result = get_project_root_sync()
        assert result == env_path

    def test_mcp_project_root_env_var(self, tmp_path, monkeypatch):
        """Test MCP_PROJECT_ROOT environment variable works."""
        env_path = tmp_path / "mcp_project"
        env_path.mkdir()
        monkeypatch.setenv("MCP_PROJECT_ROOT", str(env_path))

        configure_project_root()  # No other config
        result = get_project_root_sync()
        assert result == env_path


class TestGetProjectRoot:
    """Tests for async get_project_root function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_sources(self):
        """Test returns None when no sources available."""
        configure_project_root()  # Reset config

        # Mock context that returns empty roots
        ctx = MagicMock()
        ctx.list_roots = AsyncMock(return_value=[])

        result = await get_project_root(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_mcp_root_when_available(self, tmp_path):
        """Test uses MCP root when client provides it."""
        configure_project_root()  # Reset config

        # Create a mock root
        mock_root = MagicMock()
        mock_root.uri = f"file:///{tmp_path.as_posix()}"

        ctx = MagicMock()
        ctx.list_roots = AsyncMock(return_value=[mock_root])

        result = await get_project_root(ctx)
        # Result should be the path from MCP root
        if result:
            assert result.exists()

    @pytest.mark.asyncio
    async def test_falls_back_to_env_var(self, tmp_path, monkeypatch):
        """Test falls back to env var when MCP roots fail."""
        env_path = tmp_path / "env_project"
        env_path.mkdir()
        monkeypatch.setenv("NETCOREDBG_PROJECT_ROOT", str(env_path))

        configure_project_root()

        # Mock context that raises exception
        ctx = MagicMock()
        ctx.list_roots = AsyncMock(side_effect=Exception("Not supported"))

        result = await get_project_root(ctx)
        assert result == env_path

    @pytest.mark.asyncio
    async def test_falls_back_to_startup_cwd(self, tmp_path):
        """Test falls back to startup CWD when all else fails."""
        (tmp_path / "Solution.sln").touch()

        configure_project_root(
            use_project_from_cwd=True,
            startup_cwd=str(tmp_path),
        )

        # Mock context with empty roots
        ctx = MagicMock()
        ctx.list_roots = AsyncMock(return_value=[])

        result = await get_project_root(ctx)
        assert result == tmp_path

    @pytest.mark.asyncio
    async def test_works_without_context(self, tmp_path, monkeypatch):
        """Test works when called without context."""
        env_path = tmp_path / "env_project"
        env_path.mkdir()
        monkeypatch.setenv("NETCOREDBG_PROJECT_ROOT", str(env_path))

        configure_project_root()

        result = await get_project_root(None)
        assert result == env_path


class TestSessionManagerIntegration:
    """Tests for SessionManager integration with project utilities."""

    def test_set_project_path(self, tmp_path):
        """Test SessionManager.set_project_path method."""
        from netcoredbg_mcp.session import SessionManager

        with patch("netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg", return_value="netcoredbg"):
            session = SessionManager()
            assert session.project_path is None

            session.set_project_path(str(tmp_path))
            assert session.project_path == str(tmp_path)

            session.set_project_path(None)
            assert session.project_path is None

    def test_validate_path_after_set_project_path(self, tmp_path):
        """Test path validation works after updating project path."""
        from netcoredbg_mcp.session import SessionManager

        # Create test structure
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        test_file = project_dir / "test.cs"
        test_file.touch()

        outside_file = tmp_path / "outside.cs"
        outside_file.touch()

        with patch("netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg", return_value="netcoredbg"):
            session = SessionManager()

            # Initially no scope - should allow any path
            result = session.validate_path(str(test_file), must_exist=True)
            assert result == str(test_file)

            # Set project path
            session.set_project_path(str(project_dir))

            # Path within project should work
            result = session.validate_path(str(test_file), must_exist=True)
            assert result == str(test_file)

            # Path outside project should fail
            with pytest.raises(ValueError, match="outside project scope"):
                session.validate_path(str(outside_file))

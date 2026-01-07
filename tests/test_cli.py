"""Tests for CLI entry point - project root detection."""

import os
import pytest
from pathlib import Path

from netcoredbg_mcp.__main__ import find_project_root


class TestFindProjectRoot:
    """Tests for find_project_root function."""

    def test_finds_sln_file(self, tmp_path, monkeypatch):
        """Test that .sln file is found first."""
        # Create .sln file
        (tmp_path / "Solution.sln").touch()
        # Create .csproj in subdirectory
        subdir = tmp_path / "src" / "Project"
        subdir.mkdir(parents=True)
        (subdir / "Project.csproj").touch()

        # Change to subdirectory
        monkeypatch.chdir(subdir)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_finds_csproj_when_no_sln(self, tmp_path, monkeypatch):
        """Test that .csproj is found when no .sln exists."""
        # Create .csproj file
        (tmp_path / "Project.csproj").touch()

        # Change to directory
        monkeypatch.chdir(tmp_path)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_finds_vbproj(self, tmp_path, monkeypatch):
        """Test that .vbproj files are found."""
        (tmp_path / "Project.vbproj").touch()
        monkeypatch.chdir(tmp_path)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_finds_fsproj(self, tmp_path, monkeypatch):
        """Test that .fsproj files are found."""
        (tmp_path / "Project.fsproj").touch()
        monkeypatch.chdir(tmp_path)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_finds_git_when_no_dotnet_files(self, tmp_path, monkeypatch):
        """Test that .git is found as fallback."""
        # Create .git directory
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src"
        subdir.mkdir()

        monkeypatch.chdir(subdir)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_falls_back_to_cwd(self, tmp_path, monkeypatch):
        """Test fallback to CWD when no markers found."""
        monkeypatch.chdir(tmp_path)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_respects_boundary(self, tmp_path, monkeypatch):
        """Test that boundary parameter constrains search."""
        # Create .sln above boundary
        (tmp_path / "Solution.sln").touch()

        # Create subdirectory structure
        boundary = tmp_path / "restricted"
        boundary.mkdir()
        subdir = boundary / "project"
        subdir.mkdir()

        monkeypatch.chdir(subdir)

        # With boundary, should not find .sln above it
        result = find_project_root(root=str(boundary))
        assert result == str(subdir)  # Falls back to CWD

    def test_prefers_sln_over_csproj(self, tmp_path, monkeypatch):
        """Test that .sln is preferred over .csproj in same directory."""
        (tmp_path / "Solution.sln").touch()
        (tmp_path / "Project.csproj").touch()

        monkeypatch.chdir(tmp_path)

        result = find_project_root()
        assert result == str(tmp_path)

    def test_prefers_csproj_over_git(self, tmp_path, monkeypatch):
        """Test that .csproj is preferred over .git."""
        (tmp_path / ".git").mkdir()
        project_dir = tmp_path / "src"
        project_dir.mkdir()
        (project_dir / "Project.csproj").touch()

        monkeypatch.chdir(project_dir)

        result = find_project_root()
        assert result == str(project_dir)

    def test_searches_upward(self, tmp_path, monkeypatch):
        """Test that search goes up the directory tree."""
        # Create .sln at root
        (tmp_path / "Solution.sln").touch()

        # Create deep nested directory
        deep_dir = tmp_path / "src" / "components" / "utils"
        deep_dir.mkdir(parents=True)

        monkeypatch.chdir(deep_dir)

        result = find_project_root()
        assert result == str(tmp_path)

"""Tests for build policy - argument validation and security."""

import os

import pytest

from netcoredbg_mcp.build.policy import (
    FRAMEWORK_PATTERN,
    RUNTIME_PATTERN,
    BuildCommand,
    BuildPolicy,
)


class TestBuildCommand:
    """Tests for BuildCommand enum."""

    def test_command_values(self):
        """Test command enum values."""
        assert BuildCommand.CLEAN.value == "clean"
        assert BuildCommand.RESTORE.value == "restore"
        assert BuildCommand.BUILD.value == "build"
        assert BuildCommand.REBUILD.value == "rebuild"

    def test_command_is_string(self):
        """Test command is string enum."""
        assert isinstance(BuildCommand.BUILD, str)
        assert BuildCommand.BUILD == "build"


class TestPatterns:
    """Tests for validation patterns."""

    def test_framework_pattern_valid(self):
        """Test valid framework monikers."""
        valid = [
            "net8.0",
            "net7.0",
            "net6.0",
            "net5.0",
            "netstandard2.1",
            "netstandard2.0",
            "net48",
            "net451",  # Digits without dot
            "netcoreapp3.1",  # netcoreapp prefix
            "netcoreapp2.1",
            "net8.0-android",  # Platform suffix
            "net8.0-ios",
            "net8.0-windows10.0.19041",  # Version suffix
            "NET8.0",  # Case insensitive
        ]
        for framework in valid:
            assert FRAMEWORK_PATTERN.match(framework), f"Should match: {framework}"

    def test_framework_pattern_invalid(self):
        """Test invalid framework monikers."""
        invalid = [
            "invalid",
            "net",
            "8.0",
            "../net8.0",
            "net8.0; rm -rf /",
        ]
        for framework in invalid:
            assert not FRAMEWORK_PATTERN.match(framework), f"Should not match: {framework}"

    def test_runtime_pattern_valid(self):
        """Test valid runtime identifiers."""
        valid = [
            "win-x64",
            "win-x86",
            "linux-x64",
            "linux-arm64",
            "osx-x64",
            "osx-arm64",
            "browser-wasm",  # WebAssembly
            "iossimulator-x64",  # iOS simulator
            "maccatalyst-arm64",  # Mac Catalyst
            "linux-loongarch64",  # LoongArch
            "linux-s390x",  # IBM Z
            "unix",  # Standalone RID
            "any",  # Any platform
            "WIN-X64",  # Case insensitive
        ]
        for rid in valid:
            assert RUNTIME_PATTERN.match(rid), f"Should match: {rid}"

    def test_runtime_pattern_invalid(self):
        """Test invalid runtime identifiers."""
        invalid = [
            "invalid",
            "windows-x64",  # Should be 'win'
            "x64",
            "../win-x64",
        ]
        for rid in invalid:
            assert not RUNTIME_PATTERN.match(rid), f"Should not match: {rid}"


class TestBuildPolicyInit:
    """Tests for BuildPolicy initialization."""

    def test_init_with_valid_workspace(self, tmp_path):
        """Test initialization with valid workspace."""
        policy = BuildPolicy(workspace_root=str(tmp_path))
        assert policy.workspace_root == str(tmp_path)

    def test_init_normalizes_path(self, tmp_path):
        """Test that workspace path is normalized."""
        # Add trailing slash
        policy = BuildPolicy(workspace_root=str(tmp_path) + os.sep)
        assert not policy.workspace_root.endswith(os.sep)

    def test_init_with_allowed_outputs(self, tmp_path):
        """Test initialization with allowed output directories."""
        output_dir = tmp_path / "bin"
        output_dir.mkdir()
        policy = BuildPolicy(
            workspace_root=str(tmp_path),
            allowed_output_dirs=[str(output_dir)],
        )
        assert str(output_dir) in policy.allowed_output_dirs


class TestPathValidation:
    """Tests for path validation security."""

    def test_validate_project_path_within_workspace(self, tmp_path):
        """Test valid project path within workspace."""
        project = tmp_path / "src" / "Project.csproj"
        project.parent.mkdir(parents=True)
        project.touch()

        policy = BuildPolicy(workspace_root=str(tmp_path))
        result = policy.validate_project_path(str(project))
        assert result == str(project)

    def test_validate_project_path_outside_workspace(self, tmp_path):
        """Test rejection of path outside workspace."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="outside workspace"):
            policy.validate_project_path("/etc/passwd")

    def test_validate_path_traversal_rejected(self, tmp_path):
        """Test rejection of path traversal."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="outside workspace"):
            policy.validate_project_path(str(tmp_path / ".." / "etc" / "passwd"))

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only test")
    def test_validate_unc_path_rejected(self, tmp_path):
        """Test rejection of UNC paths by default."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="UNC paths not allowed"):
            policy.validate_project_path("\\\\server\\share\\project.csproj")

    @pytest.mark.skipif(os.name != "nt", reason="Windows-only test")
    def test_validate_device_path_rejected(self, tmp_path):
        """Test rejection of device paths."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="Device paths not allowed"):
            policy.validate_project_path("\\\\.\\C:\\project.csproj")

    def test_validate_empty_path(self, tmp_path):
        """Test rejection of empty path."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="Empty"):
            policy.validate_project_path("")


class TestArgumentValidation:
    """Tests for argument whitelist validation."""

    def test_allowed_configuration(self, tmp_path):
        """Test allowed configuration values."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        result = policy.validate_arguments(["-c", "Debug"])
        assert result == ["-c", "Debug"]

        result = policy.validate_arguments(["--configuration", "Release"])
        assert result == ["--configuration", "Release"]

    def test_invalid_configuration_rejected(self, tmp_path):
        """Test rejection of invalid configuration."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="Invalid configuration"):
            policy.validate_arguments(["-c", "MaliciousConfig"])

    def test_allowed_framework(self, tmp_path):
        """Test allowed framework values."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        result = policy.validate_arguments(["-f", "net8.0"])
        assert "-f" in result
        assert "net8.0" in result

    def test_invalid_framework_rejected(self, tmp_path):
        """Test rejection of invalid framework."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="Invalid framework"):
            policy.validate_arguments(["-f", "../etc/passwd"])

    def test_allowed_runtime(self, tmp_path):
        """Test allowed runtime values."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        result = policy.validate_arguments(["-r", "win-x64"])
        assert "-r" in result
        assert "win-x64" in result

    def test_invalid_runtime_rejected(self, tmp_path):
        """Test rejection of invalid runtime."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="Invalid runtime"):
            policy.validate_arguments(["-r", "malicious-runtime"])

    def test_boolean_flags(self, tmp_path):
        """Test boolean flag arguments."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        result = policy.validate_arguments(["--no-restore", "--force"])
        assert "--no-restore" in result
        assert "--force" in result

    def test_unknown_argument_rejected(self, tmp_path):
        """Test rejection of unknown arguments."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError, match="not allowed"):
            policy.validate_arguments(["--malicious-flag"])

    def test_property_argument_rejected(self, tmp_path):
        """Test rejection of /p: arguments (security risk)."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        # /p: is not in allowed list
        with pytest.raises(ValueError, match="not allowed"):
            policy.validate_arguments(["/p:Foo=Bar"])

    def test_verbosity_validation(self, tmp_path):
        """Test verbosity argument validation."""
        policy = BuildPolicy(workspace_root=str(tmp_path))

        result = policy.validate_arguments(["-v", "minimal"])
        assert "-v" in result
        assert "minimal" in result

        with pytest.raises(ValueError, match="Invalid verbosity"):
            policy.validate_arguments(["-v", "super-verbose"])


class TestGetDotnetCommand:
    """Tests for building dotnet commands."""

    def test_build_command(self, tmp_path):
        """Test build command generation."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        cmd = policy.get_dotnet_command(BuildCommand.BUILD, str(project), "Release")

        assert cmd[0] == "dotnet"
        assert cmd[1] == "build"
        assert str(project) in cmd
        assert "-c" in cmd
        assert "Release" in cmd
        # Note: --no-interactive removed in favor of --interactive false for restore only

    def test_clean_command(self, tmp_path):
        """Test clean command generation."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        cmd = policy.get_dotnet_command(BuildCommand.CLEAN, str(project))

        assert cmd[0] == "dotnet"
        assert cmd[1] == "clean"

    def test_restore_command(self, tmp_path):
        """Test restore command generation."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        cmd = policy.get_dotnet_command(BuildCommand.RESTORE, str(project))

        assert cmd[0] == "dotnet"
        assert cmd[1] == "restore"
        # Restore uses --interactive false for automation
        assert "--interactive" in cmd
        assert "false" in cmd

    def test_rebuild_returns_build_command(self, tmp_path):
        """Test rebuild returns build command (caller handles clean)."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        cmd = policy.get_dotnet_command(BuildCommand.REBUILD, str(project))

        assert cmd[0] == "dotnet"
        assert cmd[1] == "build"

    def test_extra_args_validated(self, tmp_path):
        """Test extra arguments are validated."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        cmd = policy.get_dotnet_command(
            BuildCommand.BUILD, str(project), extra_args=["-v", "minimal"]
        )

        assert "-v" in cmd
        assert "minimal" in cmd

    def test_extra_args_invalid_rejected(self, tmp_path):
        """Test invalid extra arguments rejected."""
        project = tmp_path / "Test.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(tmp_path))

        with pytest.raises(ValueError):
            policy.get_dotnet_command(
                BuildCommand.BUILD, str(project), extra_args=["--evil-flag"]
            )


class TestWorktreeAndEnvPaths:
    """Tests for git worktree and NETCOREDBG_ALLOWED_PATHS support."""

    def test_path_within_workspace_accepted(self, tmp_path):
        """Path inside workspace root is always accepted."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        project = workspace / "App.csproj"
        project.touch()
        policy = BuildPolicy(workspace_root=str(workspace))
        result = policy.validate_project_path(str(project))
        assert result == str(project)

    def test_path_outside_workspace_rejected(self, tmp_path):
        """Path outside workspace is rejected without env/worktree."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        other = tmp_path / "other" / "App.csproj"
        other.parent.mkdir()
        other.touch()
        policy = BuildPolicy(workspace_root=str(workspace))
        with pytest.raises(ValueError, match="outside workspace"):
            policy.validate_project_path(str(other))

    def test_env_allowed_paths(self, tmp_path, monkeypatch):
        """NETCOREDBG_ALLOWED_PATHS env var adds allowed prefixes."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        project = worktree / "App.csproj"
        project.touch()
        monkeypatch.setenv("NETCOREDBG_ALLOWED_PATHS", str(worktree))
        policy = BuildPolicy(workspace_root=str(workspace))
        result = policy.validate_project_path(str(project))
        assert result == str(project)

    def test_env_allowed_paths_comma_separated(self, tmp_path, monkeypatch):
        """Multiple paths separated by commas."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        wt1 = tmp_path / "wt1"
        wt1.mkdir()
        wt2 = tmp_path / "wt2"
        wt2.mkdir()
        project = wt2 / "App.csproj"
        project.touch()
        monkeypatch.setenv("NETCOREDBG_ALLOWED_PATHS", f"{wt1},{wt2}")
        policy = BuildPolicy(workspace_root=str(workspace))
        result = policy.validate_project_path(str(project))
        assert result == str(project)

    def test_is_within_helper(self, tmp_path):
        """_is_within correctly checks path containment."""
        root = str(tmp_path / "root")
        child = str(tmp_path / "root" / "sub")
        sibling = str(tmp_path / "root2")
        assert BuildPolicy._is_within(child, root) is True
        assert BuildPolicy._is_within(root, root) is True
        assert BuildPolicy._is_within(sibling, root) is False

    def test_output_path_in_env_allowed(self, tmp_path, monkeypatch):
        """Output paths in NETCOREDBG_ALLOWED_PATHS are accepted."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        output = worktree / "bin" / "Debug"
        output.mkdir(parents=True)
        monkeypatch.setenv("NETCOREDBG_ALLOWED_PATHS", str(worktree))
        policy = BuildPolicy(workspace_root=str(workspace))
        result = policy.validate_output_path(str(output))
        assert result == str(output)

    def test_git_worktree_auto_detection(self, tmp_path, monkeypatch):
        """Paths in auto-detected git worktrees are accepted."""
        from unittest.mock import patch
        import subprocess

        workspace = tmp_path / "project"
        workspace.mkdir()
        worktree = tmp_path / "wt-feature"
        worktree.mkdir()
        project = worktree / "App.csproj"
        project.touch()

        # Mock git worktree list to return our worktree
        porcelain_output = f"worktree {workspace}\nHEAD abc123\nbranch refs/heads/main\n\nworktree {worktree}\nHEAD def456\nbranch refs/heads/feature\n\n"
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=porcelain_output, stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            policy = BuildPolicy(workspace_root=str(workspace))
            result = policy.validate_project_path(str(project))
            assert result == str(project)

    def test_worktree_cache(self, tmp_path):
        """Worktree paths are cached after first call."""
        from unittest.mock import patch
        import subprocess

        workspace = tmp_path / "project"
        workspace.mkdir()
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"worktree {workspace}\n\n", stderr="",
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            policy = BuildPolicy(workspace_root=str(workspace))
            policy._get_allowed_worktree_paths()
            policy._get_allowed_worktree_paths()
            # subprocess.run called only once (cached)
            assert mock_run.call_count == 1

    def test_git_not_available(self, tmp_path):
        """Graceful fallback when git is not available."""
        from unittest.mock import patch

        workspace = tmp_path / "project"
        workspace.mkdir()
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            policy = BuildPolicy(workspace_root=str(workspace))
            paths = policy._get_allowed_worktree_paths()
            assert paths == []

    def test_prunable_worktrees_excluded(self, tmp_path):
        """Worktrees marked prunable are filtered out."""
        from unittest.mock import patch
        import subprocess

        workspace = tmp_path / "project"
        workspace.mkdir()
        active_wt = tmp_path / "wt-active"
        active_wt.mkdir()
        prunable_wt = tmp_path / "wt-prunable"
        prunable_wt.mkdir()

        porcelain = (
            f"worktree {workspace}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {active_wt}\nHEAD def\nbranch refs/heads/feat\n\n"
            f"worktree {prunable_wt}\nHEAD ghi\nprunable gitdir file/path points to non-existent location\n\n"
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=porcelain, stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            policy = BuildPolicy(workspace_root=str(workspace))
            paths = policy._get_allowed_worktree_paths()
            assert str(active_wt) in [os.path.abspath(p) for p in paths]
            assert str(prunable_wt) not in [os.path.abspath(p) for p in paths]

    def test_nonexistent_worktree_dir_excluded(self, tmp_path):
        """Worktrees pointing to non-existent directories are filtered out."""
        from unittest.mock import patch
        import subprocess

        workspace = tmp_path / "project"
        workspace.mkdir()
        ghost_wt = tmp_path / "wt-ghost"  # NOT created — doesn't exist

        porcelain = (
            f"worktree {workspace}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {ghost_wt}\nHEAD def\nbranch refs/heads/old\n\n"
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=porcelain, stderr="",
        )
        with patch("subprocess.run", return_value=mock_result):
            policy = BuildPolicy(workspace_root=str(workspace))
            paths = policy._get_allowed_worktree_paths()
            assert str(ghost_wt) not in [os.path.abspath(p) for p in paths]

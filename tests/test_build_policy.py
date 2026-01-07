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

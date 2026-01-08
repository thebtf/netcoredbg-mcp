"""Tests for version detection utilities."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from netcoredbg_mcp.utils.version import (
    VersionCompatibility,
    VersionInfo,
    check_version_compatibility,
    get_dbgshim_version,
    get_target_runtime_version,
)


class TestVersionInfo:
    """Tests for VersionInfo parsing."""

    def test_from_string_three_parts(self):
        """Parse version with major.minor.patch."""
        version = VersionInfo.from_string("6.0.36")
        assert version is not None
        assert version.major == 6
        assert version.minor == 0
        assert version.patch == 36
        assert version.build is None
        assert str(version) == "6.0.36"

    def test_from_string_four_parts(self):
        """Parse version with major.minor.patch.build."""
        version = VersionInfo.from_string("9.0.13.2701")
        assert version is not None
        assert version.major == 9
        assert version.minor == 0
        assert version.patch == 13
        assert version.build == 2701
        assert str(version) == "9.0.13.2701"

    def test_from_string_empty(self):
        """Empty string returns None."""
        assert VersionInfo.from_string("") is None
        assert VersionInfo.from_string(None) is None  # type: ignore

    def test_from_string_invalid(self):
        """Invalid version string returns None."""
        assert VersionInfo.from_string("not-a-version") is None
        assert VersionInfo.from_string("v1.0") is None
        assert VersionInfo.from_string("1.2") is None

    def test_from_string_with_prefix(self):
        """Version embedded in larger string."""
        # Only matches from start
        assert VersionInfo.from_string("net6.0.36") is None
        # But version at start works
        version = VersionInfo.from_string("6.0.36-preview")
        assert version is not None
        assert version.major == 6


class TestGetTargetRuntimeVersion:
    """Tests for get_target_runtime_version."""

    def test_reads_version_from_runtimeconfig(self, tmp_path):
        """Read version from runtimeconfig.json."""
        # Create a mock program and runtimeconfig
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(
            json.dumps(
                {
                    "runtimeOptions": {
                        "tfm": "net6.0-windows",
                        "framework": {
                            "name": "Microsoft.WindowsDesktop.App",
                            "version": "6.0.36",
                        },
                    }
                }
            )
        )

        version = get_target_runtime_version(str(program))
        assert version is not None
        assert version.major == 6
        assert version.minor == 0
        assert version.patch == 36

    def test_reads_version_from_frameworks_array(self, tmp_path):
        """Read version from frameworks array."""
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(
            json.dumps(
                {
                    "runtimeOptions": {
                        "frameworks": [
                            {"name": "Microsoft.NETCore.App", "version": "8.0.11"},
                            {"name": "Microsoft.WindowsDesktop.App", "version": "8.0.11"},
                        ]
                    }
                }
            )
        )

        version = get_target_runtime_version(str(program))
        assert version is not None
        assert version.major == 8

    def test_no_runtimeconfig(self, tmp_path):
        """Returns None if no runtimeconfig.json."""
        program = tmp_path / "App.dll"
        program.touch()

        version = get_target_runtime_version(str(program))
        assert version is None

    def test_invalid_json(self, tmp_path):
        """Handles invalid JSON gracefully."""
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text("not valid json {{{")

        version = get_target_runtime_version(str(program))
        assert version is None

    def test_missing_version_in_config(self, tmp_path):
        """Handles missing version field."""
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(json.dumps({"runtimeOptions": {}}))

        version = get_target_runtime_version(str(program))
        assert version is None


class TestGetDbgshimVersion:
    """Tests for get_dbgshim_version."""

    def test_dbgshim_not_found(self, tmp_path):
        """Returns None if dbgshim.dll doesn't exist."""
        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.touch()

        version = get_dbgshim_version(str(netcoredbg))
        assert version is None

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific test")
    def test_reads_version_from_real_dbgshim(self):
        """Read version from real dbgshim.dll if available."""
        # Try to find a real dbgshim.dll
        dotnet_base = r"C:\Program Files\dotnet\shared\Microsoft.NETCore.App"
        if os.path.isdir(dotnet_base):
            versions = os.listdir(dotnet_base)
            if versions:
                dbgshim = os.path.join(dotnet_base, versions[0], "dbgshim.dll")
                if os.path.isfile(dbgshim):
                    # Create a fake netcoredbg.exe next to it for the test
                    netcoredbg = os.path.join(dotnet_base, versions[0], "netcoredbg.exe")
                    version = get_dbgshim_version(netcoredbg)
                    # Should either get a version or None (if API fails)
                    if version:
                        assert version.major >= 1


class TestCheckVersionCompatibility:
    """Tests for check_version_compatibility."""

    def test_compatible_versions(self, tmp_path):
        """Same major versions are compatible."""
        # Create mock program with .NET 6 runtimeconfig
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(
            json.dumps(
                {
                    "runtimeOptions": {
                        "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.36"}
                    }
                }
            )
        )

        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.touch()

        # Mock get_dbgshim_version to return version 6
        with patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version",
            return_value=VersionInfo(major=6, minor=0, patch=3624, build=51421),
        ):
            result = check_version_compatibility(str(program), str(netcoredbg))

        assert result.compatible is True
        assert result.target_version is not None
        assert result.target_version.major == 6
        assert result.dbgshim_version is not None
        assert result.dbgshim_version.major == 6
        assert result.warning is None

    def test_incompatible_versions(self, tmp_path):
        """Different major versions are incompatible."""
        # Create mock program with .NET 6 runtimeconfig
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(
            json.dumps(
                {
                    "runtimeOptions": {
                        "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.36"}
                    }
                }
            )
        )

        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.touch()

        # Mock get_dbgshim_version to return version 9 (mismatch!)
        with patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version",
            return_value=VersionInfo(major=9, minor=0, patch=13, build=2701),
        ):
            result = check_version_compatibility(str(program), str(netcoredbg))

        assert result.compatible is False
        assert result.target_version is not None
        assert result.target_version.major == 6
        assert result.dbgshim_version is not None
        assert result.dbgshim_version.major == 9
        assert result.warning is not None
        assert "mismatch" in result.warning.lower()
        assert "E_NOINTERFACE" in result.warning
        assert "0x80004002" in result.warning

    def test_missing_target_version(self, tmp_path):
        """Returns compatible=True if can't determine target version."""
        program = tmp_path / "App.dll"
        program.touch()
        # No runtimeconfig.json

        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.touch()

        with patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version",
            return_value=VersionInfo(major=9, minor=0, patch=13),
        ):
            result = check_version_compatibility(str(program), str(netcoredbg))

        assert result.compatible is True
        assert result.target_version is None
        assert result.warning is None

    def test_missing_dbgshim_version(self, tmp_path):
        """Returns compatible=True if can't determine dbgshim version."""
        program = tmp_path / "App.dll"
        program.touch()

        runtimeconfig = tmp_path / "App.runtimeconfig.json"
        runtimeconfig.write_text(
            json.dumps(
                {
                    "runtimeOptions": {
                        "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.36"}
                    }
                }
            )
        )

        netcoredbg = tmp_path / "netcoredbg.exe"
        netcoredbg.touch()

        with patch(
            "netcoredbg_mcp.utils.version.get_dbgshim_version", return_value=None
        ):
            result = check_version_compatibility(str(program), str(netcoredbg))

        assert result.compatible is True
        assert result.target_version is not None
        assert result.dbgshim_version is None
        assert result.warning is None

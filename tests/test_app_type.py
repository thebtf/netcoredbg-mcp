"""Tests for application type detection from runtimeconfig.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netcoredbg_mcp.utils.app_type import detect_app_type


@pytest.fixture()
def app_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with a dummy .dll."""
    dll = tmp_path / "MyApp.dll"
    dll.write_text("")
    return tmp_path


def _write_runtimeconfig(app_dir: Path, data: dict) -> None:
    """Write a runtimeconfig.json alongside MyApp.dll."""
    path = app_dir / "MyApp.runtimeconfig.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_deps(app_dir: Path, data: dict) -> None:
    """Write a deps.json alongside MyApp.dll."""
    path = app_dir / "MyApp.deps.json"
    path.write_text(json.dumps(data), encoding="utf-8")


class TestDetectAppTypeRuntimeconfig:
    """Tests based on runtimeconfig.json content."""

    def test_wpf_single_framework(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Microsoft.WindowsDesktop.App",
                    "version": "8.0.0",
                },
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "gui"

    def test_winforms_in_frameworks_array(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "frameworks": [
                    {"name": "Microsoft.NETCore.App", "version": "8.0.0"},
                    {"name": "Microsoft.WindowsDesktop.App", "version": "8.0.0"},
                ],
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "gui"

    def test_avalonia_in_framework(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Avalonia.App",
                    "version": "11.0.0",
                },
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "gui"

    def test_console_app(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Microsoft.NETCore.App",
                    "version": "8.0.0",
                },
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "console"

    def test_exe_extension(self, app_dir: Path) -> None:
        """Detection works with .exe paths too."""
        exe = app_dir / "MyApp.exe"
        exe.write_text("")
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Microsoft.WindowsDesktop.App",
                    "version": "6.0.0",
                },
            },
        })
        assert detect_app_type(str(exe)) == "gui"


class TestDetectAppTypeDepsJsonFallback:
    """Tests for deps.json fallback (Avalonia detection)."""

    def test_avalonia_desktop_in_deps(self, app_dir: Path) -> None:
        """Console runtimeconfig + Avalonia.Desktop in deps -> gui."""
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Microsoft.NETCore.App",
                    "version": "8.0.0",
                },
            },
        })
        _write_deps(app_dir, {
            "targets": {
                ".NETCoreApp,Version=v8.0": {
                    "Avalonia.Desktop/11.2.0": {"dependencies": {}},
                    "Avalonia/11.2.0": {"dependencies": {}},
                },
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "gui"

    def test_no_avalonia_in_deps_stays_console(self, app_dir: Path) -> None:
        """Console runtimeconfig + no Avalonia in deps -> console."""
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {
                "framework": {
                    "name": "Microsoft.NETCore.App",
                    "version": "8.0.0",
                },
            },
        })
        _write_deps(app_dir, {
            "targets": {
                ".NETCoreApp,Version=v8.0": {
                    "Newtonsoft.Json/13.0.0": {},
                },
            },
        })
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "console"


class TestDetectAppTypeEdgeCases:
    """Edge cases and error handling."""

    def test_no_runtimeconfig_returns_none(self, app_dir: Path) -> None:
        assert detect_app_type(str(app_dir / "MyApp.dll")) is None

    def test_malformed_json_returns_none(self, app_dir: Path) -> None:
        rc = app_dir / "MyApp.runtimeconfig.json"
        rc.write_text("not json {{{", encoding="utf-8")
        assert detect_app_type(str(app_dir / "MyApp.dll")) is None

    def test_missing_runtime_options_returns_none(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {"somethingElse": True})
        assert detect_app_type(str(app_dir / "MyApp.dll")) is None

    def test_empty_frameworks_array(self, app_dir: Path) -> None:
        _write_runtimeconfig(app_dir, {
            "runtimeOptions": {"frameworks": []},
        })
        # Has runtimeOptions but no frameworks -> console
        assert detect_app_type(str(app_dir / "MyApp.dll")) == "console"

    def test_nonexistent_program_path(self, tmp_path: Path) -> None:
        """Program path doesn't need to exist, only the config files matter."""
        result = detect_app_type(str(tmp_path / "nonexistent" / "App.dll"))
        assert result is None

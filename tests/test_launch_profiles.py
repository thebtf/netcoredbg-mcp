"""Tests for project launch profile environment resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.launch_profiles import (
    LAUNCH_PROFILE_FILENAME,
    LaunchProfileError,
    resolve_launch_environment,
)
from netcoredbg_mcp.tools.debug import register_debug_tools


def write_profile(tmp_path, data: dict) -> None:
    (tmp_path / LAUNCH_PROFILE_FILENAME).write_text(
        json.dumps(data),
        encoding="utf-8",
    )


def test_no_config_and_no_env_preserves_existing_launch_behavior(tmp_path):
    result = resolve_launch_environment(project_root=tmp_path)

    assert result.env is None
    assert result.metadata is None


def test_no_config_with_direct_env_uses_direct_env_only(tmp_path):
    result = resolve_launch_environment(
        project_root=tmp_path,
        explicit_env={"APP_MODE": "debug"},
    )

    assert result.env == {"APP_MODE": "debug"}
    assert result.metadata == {
        "source": "tool-argument",
        "profile": None,
        "path": None,
        "variable_names": ["APP_MODE"],
        "applied_count": 1,
    }


def test_default_profile_env_is_applied(tmp_path):
    write_profile(
        tmp_path,
        {
            "defaultProfile": "default",
            "profiles": {
                "default": {
                    "env": {
                        "DOTNET_ENVIRONMENT": "Development",
                        "APP_CONFIG": "debug",
                    },
                },
            },
        },
    )

    result = resolve_launch_environment(project_root=tmp_path)

    assert result.env == {
        "DOTNET_ENVIRONMENT": "Development",
        "APP_CONFIG": "debug",
    }
    assert result.metadata == {
        "source": "project-launch-profile",
        "profile": "default",
        "path": str(tmp_path / LAUNCH_PROFILE_FILENAME),
        "variable_names": ["DOTNET_ENVIRONMENT", "APP_CONFIG"],
        "applied_count": 2,
    }
    assert "Development" not in str(result.metadata)
    assert "debug" not in str(result.metadata)


def test_direct_env_overrides_profile_env_and_preserves_null_removal(tmp_path):
    write_profile(
        tmp_path,
        {
            "profiles": {
                "default": {
                    "env": {
                        "APP_MODE": "profile",
                        "REMOVE_ME": "profile",
                    },
                },
            },
        },
    )

    result = resolve_launch_environment(
        project_root=tmp_path,
        explicit_env={"APP_MODE": "direct", "REMOVE_ME": None},
    )

    assert result.env == {"APP_MODE": "direct", "REMOVE_ME": None}
    assert result.metadata is not None
    assert result.metadata["variable_names"] == ["APP_MODE", "REMOVE_ME"]


def test_profile_can_inherit_named_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", "synthetic-debug-home")
    write_profile(
        tmp_path,
        {
            "profiles": {
                "default": {
                    "inherit": ["APP_HOME"],
                },
            },
        },
    )

    result = resolve_launch_environment(project_root=tmp_path)

    assert result.env == {"APP_HOME": "synthetic-debug-home"}
    assert result.metadata is not None
    assert result.metadata["variable_names"] == ["APP_HOME"]
    assert "synthetic-debug-home" not in str(result.metadata)


def test_missing_explicit_profile_returns_clear_error(tmp_path):
    write_profile(tmp_path, {"profiles": {"default": {"env": {"A": "B"}}}})

    with pytest.raises(LaunchProfileError, match="Launch profile 'missing' not found"):
        resolve_launch_environment(project_root=tmp_path, launch_profile="missing")


def test_explicit_profile_without_config_returns_clear_error(tmp_path):
    with pytest.raises(LaunchProfileError, match="No launch profile file found"):
        resolve_launch_environment(project_root=tmp_path, launch_profile="default")


def test_malformed_json_returns_clear_error(tmp_path):
    (tmp_path / LAUNCH_PROFILE_FILENAME).write_text("{", encoding="utf-8")

    with pytest.raises(LaunchProfileError, match="Invalid launch profile JSON"):
        resolve_launch_environment(project_root=tmp_path)


def test_invalid_env_value_returns_clear_error(tmp_path):
    write_profile(
        tmp_path,
        {
            "profiles": {
                "default": {
                    "env": {"APP_MODE": 123},
                },
            },
        },
    )

    with pytest.raises(LaunchProfileError, match="env.APP_MODE must be string or null"):
        resolve_launch_environment(project_root=tmp_path)


def test_missing_inherited_env_returns_clear_error(tmp_path):
    write_profile(
        tmp_path,
        {
            "profiles": {
                "default": {
                    "inherit": ["MISSING_REQUIRED_ENV"],
                },
            },
        },
    )

    with pytest.raises(LaunchProfileError, match="required inherited environment"):
        resolve_launch_environment(project_root=tmp_path)


@pytest.mark.asyncio
async def test_start_debug_applies_project_profile_and_returns_redacted_metadata(tmp_path):
    write_profile(
        tmp_path,
        {
            "profiles": {
                "default": {
                    "env": {"APP_MODE": "profile-secret-value"},
                },
            },
        },
    )

    class ToolRegistry:
        def __init__(self) -> None:
            self.tools = {}

        def tool(self, annotations=None):
            def decorator(func):
                self.tools[func.__name__] = func
                return func

            return decorator

    registry = ToolRegistry()
    session = SimpleNamespace(
        project_path=str(tmp_path),
        state=SimpleNamespace(state="idle"),
        validate_program=MagicMock(side_effect=lambda program, must_exist=True: program),
        validate_path=MagicMock(side_effect=lambda path, must_exist=True: path),
        launch=AsyncMock(return_value={"success": True, "program": "app.dll"}),
    )

    async def notify_state_changed(ctx):
        return None

    async def resolve_project_root(ctx, session):
        session.project_path = str(tmp_path)

    register_debug_tools(
        registry,
        session,
        ownership=SimpleNamespace(release=MagicMock()),
        notify_state_changed=notify_state_changed,
        check_session_access=lambda ctx: None,
        execute_and_wait=AsyncMock(),
        resolve_project_root=resolve_project_root,
    )

    response = await registry.tools["start_debug"](
        SimpleNamespace(),
        program="app.dll",
        pre_build=False,
    )

    session.launch.assert_awaited_once()
    assert session.launch.await_args.kwargs["env"] == {"APP_MODE": "profile-secret-value"}
    metadata = response["data"]["launch_environment"]
    assert metadata == {
        "source": "project-launch-profile",
        "profile": "default",
        "path": str(tmp_path / LAUNCH_PROFILE_FILENAME),
        "variable_names": ["APP_MODE"],
        "applied_count": 1,
    }
    assert "profile-secret-value" not in str(metadata)

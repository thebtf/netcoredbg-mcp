"""Project-local launch profile environment resolution."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAUNCH_PROFILE_FILENAME = ".netcoredbg-mcp.launch.json"

EnvValue = str | None
LaunchMetadata = dict[str, Any]


class LaunchProfileError(ValueError):
    """Raised when a launch profile cannot be resolved safely."""


@dataclass(frozen=True)
class LaunchEnvironment:
    """Resolved launch environment plus secret-safe metadata."""

    env: dict[str, EnvValue] | None
    metadata: LaunchMetadata | None


def resolve_launch_environment(
    *,
    project_root: str | Path | None,
    launch_profile: str | None = None,
    explicit_env: Mapping[str, EnvValue] | None = None,
    process_env: Mapping[str, str] | None = None,
) -> LaunchEnvironment:
    """Resolve launch env from project profile and direct tool arguments."""

    profile_path = _profile_path(project_root)
    explicit_env_copy = _validate_explicit_env(explicit_env)

    if profile_path is None or not profile_path.is_file():
        if launch_profile:
            raise LaunchProfileError(
                f"No launch profile file found at {profile_path or LAUNCH_PROFILE_FILENAME}"
            )
        if explicit_env is None:
            return LaunchEnvironment(env=None, metadata=None)
        return LaunchEnvironment(
            env=explicit_env_copy,
            metadata=_metadata(
                source="tool-argument",
                profile=None,
                path=None,
                env=explicit_env_copy,
            ),
        )

    config = _load_profile_config(profile_path)
    profiles = _profiles(config)
    selected_profile = _select_profile_name(config, launch_profile)
    profile_data = profiles.get(selected_profile)
    if profile_data is None:
        raise LaunchProfileError(f"Launch profile '{selected_profile}' not found")
    if not isinstance(profile_data, dict):
        raise LaunchProfileError(f"Launch profile '{selected_profile}' must be an object")

    resolved_env: dict[str, EnvValue] = {}
    inherited_env = process_env if process_env is not None else os.environ
    for name in _inherit_names(profile_data):
        if name not in inherited_env:
            raise LaunchProfileError(
                f"Launch profile '{selected_profile}' is missing required inherited environment "
                f"variable '{name}', but it is not set"
            )
        resolved_env[name] = inherited_env[name]

    resolved_env.update(_profile_env(profile_data))
    if explicit_env is not None:
        resolved_env.update(explicit_env_copy)

    return LaunchEnvironment(
        env=resolved_env,
        metadata=_metadata(
            source="project-launch-profile",
            profile=selected_profile,
            path=str(profile_path),
            env=resolved_env,
        ),
    )


def _profile_path(project_root: str | Path | None) -> Path | None:
    if project_root is None:
        return None
    return Path(project_root) / LAUNCH_PROFILE_FILENAME


def _load_profile_config(profile_path: Path) -> dict[str, Any]:
    try:
        raw_config = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LaunchProfileError(f"Invalid launch profile JSON: {exc.msg}") from exc
    except OSError as exc:
        raise LaunchProfileError(f"Could not read launch profile file: {exc}") from exc

    if not isinstance(raw_config, dict):
        raise LaunchProfileError("Launch profile file must contain a JSON object")
    return raw_config


def _profiles(config: Mapping[str, Any]) -> dict[str, Any]:
    raw_profiles = config.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise LaunchProfileError("Launch profile file must define a profiles object")
    for name in raw_profiles:
        if not isinstance(name, str):
            raise LaunchProfileError("Launch profile names must be strings")
    return raw_profiles


def _select_profile_name(config: Mapping[str, Any], launch_profile: str | None) -> str:
    if launch_profile:
        return launch_profile

    default_profile = config.get("defaultProfile", "default")
    if not isinstance(default_profile, str):
        raise LaunchProfileError("defaultProfile must be a string")
    return default_profile


def _inherit_names(profile_data: Mapping[str, Any]) -> list[str]:
    raw_inherit = profile_data.get("inherit", [])
    if not isinstance(raw_inherit, list):
        raise LaunchProfileError("inherit must be a list of environment variable names")
    for index, name in enumerate(raw_inherit):
        if not isinstance(name, str):
            raise LaunchProfileError(f"inherit[{index}] must be a string")
    return raw_inherit


def _profile_env(profile_data: Mapping[str, Any]) -> dict[str, EnvValue]:
    raw_env = profile_data.get("env", {})
    if not isinstance(raw_env, dict):
        raise LaunchProfileError("env must be an object")

    env: dict[str, EnvValue] = {}
    for name, value in raw_env.items():
        if not isinstance(name, str):
            raise LaunchProfileError("env variable names must be strings")
        if value is not None and not isinstance(value, str):
            raise LaunchProfileError(f"env.{name} must be string or null")
        env[name] = value
    return env


def _validate_explicit_env(explicit_env: Mapping[str, EnvValue] | None) -> dict[str, EnvValue]:
    if explicit_env is None:
        return {}

    env: dict[str, EnvValue] = {}
    for name, value in explicit_env.items():
        if not isinstance(name, str):
            raise LaunchProfileError("Explicit env variable names must be strings")
        if value is not None and not isinstance(value, str):
            raise LaunchProfileError(f"Explicit env.{name} must be string or null")
        env[name] = value
    return env


def _metadata(
    *,
    source: str,
    profile: str | None,
    path: str | None,
    env: Mapping[str, EnvValue],
) -> LaunchMetadata:
    return {
        "source": source,
        "profile": profile,
        "path": path,
        "variable_names": list(env.keys()),
        "applied_count": len(env),
    }

"""Critical release-gate tests for user-visible netcoredbg-mcp behavior.

@critical
category: smoke, behavioral, data-consistency
features: cli-entrypoint, mcp-surface-registration, launch-environment-redaction
dev_stand: optional
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from netcoredbg_mcp import __version__
from netcoredbg_mcp.launch_profiles import LAUNCH_PROFILE_FILENAME, resolve_launch_environment
from netcoredbg_mcp.server import create_server


@pytest.mark.critical
def test_cli_version_reports_package_version() -> None:
    """@critical category: smoke — installed CLI exposes the packaged version."""

    env = dict(os.environ)
    src_path = os.path.abspath("src")
    env["PYTHONPATH"] = (
        src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )
    result = subprocess.run(
        [sys.executable, "-m", "netcoredbg_mcp", "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout


@pytest.mark.critical
@pytest.mark.asyncio
async def test_mcp_server_registers_core_surfaces() -> None:
    """@critical category: behavioral — MCP server exposes tools, prompts, resources."""

    server = create_server(str(os.getcwd()))

    tools = await server.list_tools()
    prompts = await server.list_prompts()
    resources = await server.list_resources()

    tool_names = {tool.name for tool in tools}
    prompt_names = {prompt.name for prompt in prompts}
    resource_uris = {str(resource.uri) for resource in resources}

    assert {"start_debug", "add_breakpoint", "get_call_stack"}.issubset(tool_names)
    assert {"debug", "dap-escape-hatch"}.issubset(prompt_names)
    assert {"debug://state", "debug://breakpoints", "debug://output"}.issubset(
        resource_uris
    )


@pytest.mark.critical
def test_launch_profile_metadata_never_exposes_environment_values(tmp_path) -> None:
    """@critical category: data-consistency — launch metadata is secret-safe."""

    profile = tmp_path / LAUNCH_PROFILE_FILENAME
    profile.write_text(
        """
{
  "profiles": {
    "default": {
      "inherit": ["APP_HOME"],
      "env": {
        "APP_MODE": "profile-secret"
      }
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    result = resolve_launch_environment(
        project_root=tmp_path,
        explicit_env={"DIRECT_SECRET": "direct-secret"},
        process_env={"APP_HOME": "inherited-secret"},
    )

    assert result.env == {
        "APP_HOME": "inherited-secret",
        "APP_MODE": "profile-secret",
        "DIRECT_SECRET": "direct-secret",
    }
    assert result.metadata == {
        "source": "project-launch-profile",
        "profile": "default",
        "path": str(profile),
        "variable_names": ["APP_HOME", "APP_MODE", "DIRECT_SECRET"],
        "applied_count": 3,
    }
    assert "inherited-secret" not in str(result.metadata)
    assert "profile-secret" not in str(result.metadata)
    assert "direct-secret" not in str(result.metadata)

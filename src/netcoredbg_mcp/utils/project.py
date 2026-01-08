"""Project root detection utilities.

Provides utilities for determining the project root directory from multiple sources:
1. MCP Roots from client (via Context.list_roots())
2. Environment variables (NETCOREDBG_PROJECT_ROOT, MCP_PROJECT_ROOT)
3. Startup CWD (when --project-from-cwd is used)

This follows the pattern established by Serena's project-from-cwd implementation.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from mcp.server.fastmcp import Context

logger = logging.getLogger(__name__)


@dataclass
class ProjectRootConfig:
    """Configuration for project root detection.

    Stores settings that affect how project root is determined.
    """

    startup_cwd: Path | None = None
    """CWD captured at server startup (when --project-from-cwd is used)."""

    use_project_from_cwd: bool = False
    """Whether --project-from-cwd flag was provided."""

    explicit_project_path: Path | None = None
    """Explicit project path from --project flag."""

    env_var_names: tuple[str, ...] = field(
        default_factory=lambda: ("NETCOREDBG_PROJECT_ROOT", "MCP_PROJECT_ROOT")
    )
    """Environment variable names to check for project root."""


# Global configuration (set at startup)
_config: ProjectRootConfig = ProjectRootConfig()


def configure_project_root(
    *,
    use_project_from_cwd: bool = False,
    explicit_project_path: str | Path | None = None,
    startup_cwd: str | Path | None = None,
) -> None:
    """Configure project root detection.

    Should be called once at server startup.

    Args:
        use_project_from_cwd: Whether --project-from-cwd was specified
        explicit_project_path: Explicit --project path if provided
        startup_cwd: CWD to use as fallback (captured at startup)
    """
    global _config
    _config = ProjectRootConfig(
        use_project_from_cwd=use_project_from_cwd,
        explicit_project_path=Path(explicit_project_path) if explicit_project_path else None,
        startup_cwd=Path(startup_cwd) if startup_cwd else None,
    )
    logger.debug(
        f"Project root configured: use_cwd={use_project_from_cwd}, "
        f"explicit={explicit_project_path}, startup_cwd={startup_cwd}"
    )


def get_config() -> ProjectRootConfig:
    """Get current project root configuration."""
    return _config


def parse_file_uri(uri: str) -> Path | None:
    """Parse a file:// URI to a Path.

    Handles platform-specific path formats:
    - Unix: file:///home/user/project → /home/user/project
    - Windows: file:///C:/Users/project → C:\\Users\\project
    - Windows UNC: file://server/share → \\\\server\\share

    Args:
        uri: A file:// URI string

    Returns:
        Path object if parsing succeeds, None otherwise
    """
    try:
        parsed = urlparse(str(uri))

        if parsed.scheme != "file":
            logger.warning(f"Not a file URI: {uri}")
            return None

        # URL-decode the path component
        path_str = unquote(parsed.path)

        # Handle Windows paths
        if sys.platform == "win32":
            # file:///C:/path → parsed.path = "/C:/path"
            # Need to strip the leading slash for drive letters
            if path_str.startswith("/") and len(path_str) > 2:
                # Check if second char is a drive letter (e.g., /C:)
                if path_str[2] == ":":
                    path_str = path_str[1:]  # Remove leading slash

            # Handle UNC paths: file://server/share
            if parsed.netloc:
                path_str = f"\\\\{parsed.netloc}{path_str}"

        path = Path(path_str)

        # Resolve to absolute path
        if not path.is_absolute():
            logger.warning(f"Parsed path is not absolute: {path}")
            return None

        return path

    except Exception as e:
        logger.warning(f"Failed to parse file URI '{uri}': {e}")
        return None


def find_dotnet_project_root(start_dir: Path | None = None) -> Path:
    """Find .NET project root by walking up from a directory.

    Searches for project markers in this order:
    1. .sln (solution file) - preferred for multi-project setups
    2. .csproj/.vbproj/.fsproj (project files)
    3. .git (git root as fallback)

    Falls back to start_dir if no marker is found.

    Args:
        start_dir: Directory to start search from. Defaults to CWD.

    Returns:
        Path to project root
    """
    current = (start_dir or Path.cwd()).resolve()

    def ancestors() -> Iterator[Path]:
        """Yield current directory and ancestors."""
        yield current
        yield from current.parents

    # First pass: look for .sln (solution - most specific for .NET)
    for directory in ancestors():
        if any(directory.glob("*.sln")):
            return directory

    # Second pass: look for project files (.csproj, .vbproj, .fsproj)
    for directory in ancestors():
        if (
            any(directory.glob("*.csproj"))
            or any(directory.glob("*.vbproj"))
            or any(directory.glob("*.fsproj"))
        ):
            return directory

    # Third pass: look for .git
    for directory in ancestors():
        if (directory / ".git").exists():
            return directory

    # Fall back to start directory
    return current


async def get_project_root(ctx: Context | None = None) -> Path | None:
    """Determine the project root directory from available sources.

    Priority order:
    1. MCP Roots from client (via ctx.list_roots()) - if client supports it
    2. Environment variable (NETCOREDBG_PROJECT_ROOT or MCP_PROJECT_ROOT)
    3. Explicit --project path (if configured)
    4. Startup CWD with .NET marker search (if --project-from-cwd)

    Args:
        ctx: MCP Context for accessing client-provided roots.
             Can be None if called outside of tool context.

    Returns:
        Path to project root, or None if not determinable

    Example:
        ```python
        @mcp.tool()
        async def my_tool(ctx: Context) -> str:
            project_root = await get_project_root(ctx)
            if not project_root:
                raise ToolError("Cannot determine project root")
            # Use project_root...
        ```
    """
    config = get_config()

    # 1. Try MCP Roots from client (highest priority - client knows best)
    if ctx is not None:
        try:
            roots = await ctx.list_roots()
            logger.info(f"MCP list_roots() returned {len(roots) if roots else 0} roots")
            if roots:
                # Use the first root
                uri = str(roots[0].uri)
                logger.info(f"Got root from client: {uri}")

                path = parse_file_uri(uri)
                if path and path.exists() and path.is_dir():
                    logger.info(f"Using project root from MCP client: {path}")
                    return path
                else:
                    logger.warning(f"MCP root path invalid or not accessible: {path}")
            else:
                logger.info("MCP client did not provide any roots")
        except Exception as e:
            # Client may not support roots - this is fine
            logger.info(f"Could not get roots from client: {e}")

    # 2. Check environment variables
    for env_var in config.env_var_names:
        env_value = os.environ.get(env_var)
        if env_value:
            path = Path(env_value)
            if path.exists() and path.is_dir():
                logger.info(f"Using project root from {env_var}: {path}")
                return path
            else:
                logger.warning(f"{env_var}={env_value} - path does not exist or is not a directory")

    # 3. Use explicit --project path if configured
    if config.explicit_project_path:
        if config.explicit_project_path.exists() and config.explicit_project_path.is_dir():
            logger.info(f"Using explicit project path: {config.explicit_project_path}")
            return config.explicit_project_path
        else:
            logger.warning(f"Explicit project path not valid: {config.explicit_project_path}")

    # 4. Use startup CWD with .NET marker search (if --project-from-cwd)
    if config.use_project_from_cwd and config.startup_cwd:
        project_root = find_dotnet_project_root(config.startup_cwd)
        logger.info(f"Using project root from CWD search: {project_root}")
        return project_root

    # 5. Fallback: return startup CWD without marker search if available
    if config.startup_cwd:
        logger.info(f"Using startup CWD as fallback: {config.startup_cwd}")
        return config.startup_cwd

    # Cannot determine project root
    logger.warning("Could not determine project root from any source")
    return None


def get_project_root_sync() -> Path | None:
    """Synchronous version of get_project_root (without MCP roots).

    Use this when you don't have access to MCP Context, e.g., at startup.
    Only checks environment variables and startup CWD.

    Returns:
        Path to project root, or None if not determinable
    """
    config = get_config()

    # 1. Check environment variables
    for env_var in config.env_var_names:
        env_value = os.environ.get(env_var)
        if env_value:
            path = Path(env_value)
            if path.exists() and path.is_dir():
                return path

    # 2. Use explicit --project path if configured
    if config.explicit_project_path:
        if config.explicit_project_path.exists() and config.explicit_project_path.is_dir():
            return config.explicit_project_path

    # 3. Use startup CWD with .NET marker search (if --project-from-cwd)
    if config.use_project_from_cwd and config.startup_cwd:
        return find_dotnet_project_root(config.startup_cwd)

    # 4. Fallback: return startup CWD if available
    if config.startup_cwd:
        return config.startup_cwd

    return None

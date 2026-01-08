"""Utility modules for netcoredbg-mcp."""

from .project import ProjectRootConfig, get_project_root, parse_file_uri
from .version import (
    VersionCompatibility,
    VersionInfo,
    check_version_compatibility,
    get_dbgshim_version,
    get_target_runtime_version,
)

__all__ = [
    "get_project_root",
    "parse_file_uri",
    "ProjectRootConfig",
    "VersionInfo",
    "VersionCompatibility",
    "get_target_runtime_version",
    "get_dbgshim_version",
    "check_version_compatibility",
]

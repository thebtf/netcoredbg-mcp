"""Utility modules for netcoredbg-mcp."""

from .project import (
    ProjectRootConfig,
    get_project_root,
    is_network_file_uri,
    is_unc_or_network_path,
    operator_project_scope_configured,
    parse_file_uri,
)
from .version import (
    VersionCompatibility,
    VersionInfo,
    check_version_compatibility,
    get_dbgshim_version,
    get_target_runtime_version,
)

__all__ = [
    "ProjectRootConfig",
    "VersionCompatibility",
    "VersionInfo",
    "check_version_compatibility",
    "get_dbgshim_version",
    "get_project_root",
    "get_target_runtime_version",
    "is_network_file_uri",
    "is_unc_or_network_path",
    "operator_project_scope_configured",
    "parse_file_uri",
]

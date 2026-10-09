# netcoredbg-mcp v0.23.12

Prepared: 2026-10-09

## Summary

`v0.23.12` is a PATCH hotfix for explicit build, debug-target, and source paths
outside the default project or Git worktree.

## Fixed behavior

1. Explicit build-project and output paths need no separate debug permission.
2. Explicit paths to existing DLL/EXE targets and source files need no directory allowlist, including paths outside the default project or worktree.
3. Configuration and troubleshooting guidance no longer asks users to grant directory access before building or debugging an explicit target.

The removed admission check was MCP-wrapper policy, not a native Samsung
`netcoredbg` operating-system sandbox.

## Compatibility

File and .NET target validity checks remain unchanged. Independent evidence,
restore, and source-search guards are not relaxed. The published Python entry
point and locked dependency versions are unchanged.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-01-12

### Added
- **UI Automation tools** for WPF/WinForms testing via pywinauto
  - `ui_get_window_tree` - get full window hierarchy
  - `ui_find_element` - find UI elements by criteria
  - `ui_click_element` - click buttons and controls
  - `ui_send_keys` - send keyboard input
  - `ui_get_element_info` - get element properties
  - `ui_invoke_pattern` - invoke UI automation patterns
- **MCP Spec Compliance**
  - Resources with proper `mime_type`
  - Progress notifications during long operations
  - Structured prompts for debugging workflows
  - Output search tool with regex support
- **Agent hints** in tool docstrings for breakpoint timing and GUI interaction
- **Git & Release workflow** documentation in AGENTS.md

### Changed
- `pre_build=True` is now the default for `start_debug` tool

### Fixed
- Test mocking for `_find_netcoredbg` method

## [0.1.0] - 2026-01-10

### Added
- Initial release
- MCP server for .NET debugging via netcoredbg
- DAP protocol implementation
- Build management with automatic cleanup
- Breakpoint management
- Variable inspection
- Step debugging (into, over, out)
- Exception handling

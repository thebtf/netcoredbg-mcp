# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- CR-002 DAP coverage expansion for v0.12.0: event coverage now includes the
  7 previously unhandled DAP events, 11 typed event body dataclasses, WARN
  logging for unhandled future events, `DebuggerBackend` capability scaffolding,
  progress tracking via `get_progress`, memory inspection via `read_memory` and
  `write_memory`, inspection surfaces for `get_loaded_sources`, `disassemble`,
  and `get_locations`, advertised `supportsProgressReporting` and
  `supportsMemoryReferences`, and a `dap-escape-hatch` prompt documenting 12
  unwrapped DAP commands reachable through lower-level `send_request` usage.

### Fixed
- Tracepoints in C# `async` methods (compiler-generated `MoveNext()` state
  machine frames) silently behaved as stopping breakpoints. Root cause: DAP
  adjusts the breakpoint line to the first executable IL line after the
  `await`; the tracepoint kept the original user-requested line and stopped
  matching. Now both lines are tracked (`line` + `dap_line`) and matching
  works for either. `add_tracepoint` and `list_breakpoints` responses now
  expose `dap_line` when the DAP adapter adjusted the line. Fixes engram
  cross-project issue #96 (blocker for novascript Phase 2).
- `remove_breakpoint(line=requested)` previously returned `{removed: false}`
  after DAP adjusted the breakpoint line. `Breakpoint.line` now keeps the
  user-requested identity and removal works as expected. When called with
  the DAP-adjusted line, the response includes a `hint` explaining the
  adjustment.

## [0.6.1] - 2026-04-07

### Changed
- **Comprehensive prompt/docs audit** — 20 findings fixed from redoc audit
  - Prompts: `get_exception_context` quick path (saves 3 tool calls per exception)
  - Prompts: `get_stop_context` quick path (saves 2 tool calls per stop)
  - Prompts: multi-threaded debugging section, `quick_evaluate`, `configure_exceptions`
  - Prompts: WinForms AccessibleName vs AutomationId guidance
  - Prompts: all `investigate` playbooks updated with `get_exception_context`
  - Tool docstrings: STOPPED warnings on 4 UI tools, pause_execution state hint
  - README: v0.6.0 What's New, 546 tests, 12 new env vars, phantom var removed
  - README.ru.md: synced with EN changes

### Fixed
- Tracepoint auto-resume — excluded tracepoint-owned breakpoints from user bp check
- XPath WinForms — use AccessibleName for UIA Name matching
- DataGrid smoke test added (multi_select, extract_text)
- 100/100 smoke tests (was 85/87)

## [0.6.0] - 2026-04-07

### Added
- **MCP Progress Notifications** — all long-running tools now report real-time progress
  - Build output streaming: each `dotnet build` line → `ctx.info()` (stdout) / `ctx.warning()` (stderr)
  - Phase-level progress for `start_debug`: 9 phases from 0% to 100%
  - Execution tool heartbeat: `continue_execution`, `step_*` report "Still waiting... (5s, 10s...)"
  - `restart_debug` progress: rebuild/no-rebuild phases
  - 500-line build output cap with summary
  - Circuit breaker: suppress notifications on client disconnect
  - Distinct messages for stopped/terminated/timed-out states

### Fixed
- Git worktree path validation (#31) + `NETCOREDBG_ALLOWED_PATHS` env var
- mcp-mux isolation: `session-aware` → `isolated` (cross-project scope fix)
- Tracepoint filename-only fallback matching + `os.path.normcase`
- Tracepoint timeout guards (5s check, 3s hit count)
- All hardcoded limits now configurable via 10 env vars

## [0.5.6] - 2026-04-07

### Fixed
- **Tracepoint path matching** — filename-only fallback when full path doesn't match PDB
- **Case-aware filesystems** — `os.path.normcase` instead of `.lower()` for cross-platform correctness
- **Screenshot default** — 1568px (Claude vision maximum), configurable via `NETCOREDBG_SCREENSHOT_MAX_WIDTH`
- **10 env vars** for all hardcoded limits (tracepoints, snapshots, output, session timeout)

## [0.5.5] - 2026-04-07

### Fixed
- **mcp-mux isolation** — changed `x-mux` capability from `session-aware` to `isolated` so each CC session gets its own daemon with correct cwd (fixes cross-project path rejection)
- `validate_path` in SessionManager now supports worktrees + `NETCOREDBG_ALLOWED_PATHS` (mirrors BuildPolicy logic)

## [0.5.4] - 2026-04-07

### Fixed
- **Git worktree support** — `validate_project_path` and `validate_output_path` now accept paths in git worktrees (#31)
- Auto-detect worktrees via `git worktree list --porcelain` (cached, 5s timeout)
- `NETCOREDBG_ALLOWED_PATHS` env var for additional allowed path prefixes
- Filter prunable worktrees and verify directory exists before allowing
- Tracepoint timeout guards (5s on `_check_tracepoint`, 3s on `_update_hit_count`)
- Debug logging for tracepoint path matching diagnostics

## [0.5.3] - 2026-04-05

### Added
- **stepInTargets** — choose which function to step into on multi-call lines
- **Variable paging** — `filter`, `start`, `count` params on `get_variables` for large collections
- **Output variablesReference** — structured data refs stored with output entries
- **Codebase search guidance** in debug prompts — agents now directed to use SocratiCode/Serena/LSP before setting breakpoints
- **Edit-Rebuild-Retest workflow** — explicit cycle for fix-and-verify debugging
- **Breakpoint timeout guidance** — `timed_out=True` recovery steps
- **Decision tree** — "Which Prompt to Use" table for 7 debugging prompts
- 19 new symptom mappings (JsonException, HttpRequestException, SqlException, etc.)
- State preconditions on 21 tool docstrings (STOPPED/RUNNING required)
- Sequencing hints in tool docstrings (call_stack → scopes → variables chain)
- MCP Resources section in debug guide (debug://state, debug://breakpoints, etc.)

### Changed
- `_on_continued` handler now respects `allThreadsContinued` field and clears thread state
- State machine diagram now shows all TERMINATED transitions
- Frozen-UI warning added to `ui_click`, `ui_invoke`, `ui_send_keys`

## [0.5.1] - 2026-04-04

### Added
- **ElementResolver** — ranked element search with scoring (depth penalty, ComboBox child penalty, dialog button bonus, enabled/visible)
- **ExtractText** — 5-strategy text extraction: ValuePattern → TextPattern → Name → LegacyIAccessible → TextDescendants
- **Client-side tracepoints** — pause → evaluate → resume with asyncio.Lock, 500ms timeout, 10 hits/sec rate limiting
- **State snapshots + diff** — FIFO eviction (max 20), diff shows added/removed/changed variables
- **Collection analyzer** — count, nulls, duplicates, numeric stats via DAP variables
- **Object summarizer** — recursive get_variables with depth tracking + circular ref detection via ancestors
- 9 new MCP tools: `add_tracepoint`, `remove_tracepoint`, `get_trace_log`, `clear_trace_log`, `create_snapshot`, `diff_snapshots`, `list_snapshots`, `analyze_collection`, `summarize_object`

### Fixed
- stackTrace retry on PROCESS_NOT_SYNCHRONIZED (0x80131302) — 3 retries with 100ms delay
- Tracepoint events no longer leak as STOPPED events to clients
- Path traversal validation in `add_tracepoint`
- Cross-platform path normalization via `os.path.normcase`

## [0.5.0] - 2026-04-04

### Added
- **ui_invoke** — InvokePattern with Click fallback for buttons
- **ui_toggle** — TogglePattern for CheckBox/ToggleButton with state cycle
- **ui_file_dialog** — multi-strategy Windows Open/Save dialog automation (4 fallback strategies)
- **root_id** parameter on 11 tools — scope element search to subtree via AutomationId
- **xpath** parameter on 11 tools — XPath element search (FlaUI backend only)
- `find_by_xpath` with matchCount warning on multiple matches
- `FindElementCascade` — priority cascade: automationId > xpath > name+controlType
- `ResolveSearchRoot` — self-match check before descendant search
- `_escape_sendkeys_path` — SendKeys special character escaping for file paths

### Fixed
- `[STAThread]` moved from CountToTen to Main in WinForms smoke app
- XPath delegation from FindElement when xpath is the only criterion
- `ui_get_selected_item` now uses root_id for scoped search
- `_find_ui_element` propagates root_id via `_find_element_scoped` on pywinauto

## [0.4.0] - 2026-04-04

### Added
- **DAP Coverage Expansion** — 19 new capabilities
- Client-side breakpoint hit counting (location-based)
- Breakpoint changed event handling
- `get_modules` tool (event-based)
- `get_exception_context` — combined exception info + call stack + variable dump
- `get_build_diagnostics` — compiler errors/warnings from build output
- `get_stop_context` — one-shot stopped state summary
- `configure_exceptions` — configure exception breakpoint filtering
- `get_exception_info` — detailed exception data when stopped on exception
- Quick evaluate with atomic pause/eval/resume pattern

### Changed
- Stepping tools return stopped context automatically after step completes

## [0.3.1] - 2026-04-03

### Fixed
- `send_keys` Alt key combination handling
- 4 new UI tools added to server registration

## [0.3.0] - 2026-04-03

### Added
- **UIBackend abstraction** — FlaUI primary, pywinauto fallback
- **FlaUI C# subprocess bridge** — JSON-RPC over stdin/stdout for UIA3 automation
- Backend auto-detection based on FlaUIBridge.exe availability
- `ui_click_annotated` — click by annotation index from SoM screenshot
- `ui_double_click`, `ui_right_click`, `ui_drag`, `ui_scroll`
- `ui_select_items` — multi-select via Ctrl+click
- `ui_wait_for` — wait for element state changes
- `ui_get_focused_element` — current keyboard focus
- `ui_send_keys_focused` — send keys to focused element

### Changed
- All UI tools use backend abstraction layer instead of direct pywinauto calls

## [0.2.0] - 2026-02-15

### Added
- **MCP ImageContent screenshots** + session temp manager
- **Annotated screenshots** — Set-of-Marks (SoM) element annotation with indices
- **Parameterized debug prompts** — targeted investigation plans
- **mcp-mux session-aware** multiplexing support
- **Process Reaper** — track and cleanup debug processes on session end
- Desktop UI debugging workflow

### Changed
- Server split into tool modules for better organization
- Screenshot transport via WebP for smaller payloads

### Fixed
- UI automation reliability — element cache + coordinate click fallback + downsampling
- Context import for tool registration
- Annotated screenshot size optimization

## [0.1.1] - 2026-01-12

### Added
- **UI Automation tools** for WPF/WinForms testing via pywinauto
  - `ui_get_window_tree`, `ui_find_element`, `ui_click_element`, `ui_send_keys`, `ui_get_element_info`, `ui_invoke_pattern`
- **MCP Spec Compliance** — resources, progress notifications, structured prompts, output search
- Agent hints in tool docstrings
- Git & Release workflow documentation

### Changed
- `pre_build=True` is now the default for `start_debug`

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

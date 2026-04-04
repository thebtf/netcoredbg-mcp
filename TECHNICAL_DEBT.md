# Technical Debt — netcoredbg-mcp

## From DAP Coverage Gap Analysis (2026-04-04)

Full reports: `.agent/data/mcp-dap-coverage-gaps.md`, `.agent/data/netcoredbg-dap-capabilities.md`

### HIGH Priority

#### H1: ~~Expose logpoints via add_breakpoint~~ BLOCKED
**Status:** BLOCKED — netcoredbg does NOT parse `logMessage` at all (source audit confirmed).
**Workaround:** Use `quick_evaluate` (Q1) instead — pause/eval/resume atomically.
**Future:** Contribute logMessage support upstream to samsung/netcoredbg, or implement in FlaUI bridge.

#### ~~H2: Client-side breakpoint hit counting (location-based)~~ DONE v0.4.0
**Implemented in:** `session/manager.py:_on_stopped` + `_update_hit_count`, `tools/breakpoints.py:list_breakpoints`

#### ~~H3: Handle breakpoint changed events~~ DONE v0.4.0
**Implemented in:** `session/manager.py:_on_breakpoint`, `dap/events.py:BreakpointEventBody`

### MEDIUM Priority

#### ~~M1: Add get_modules tool~~ DONE v0.4.0
**Implemented in:** `tools/inspection.py:get_modules` (event-based, not DAP modules request)

#### M2: ~~Add breakpoint_locations tool~~ BLOCKED
**Status:** BLOCKED — netcoredbg does NOT support `breakpointLocations`.

#### M3: ~~Add data breakpoints~~ BLOCKED
**Status:** BLOCKED — netcoredbg does NOT support `supportsDataBreakpoints`.

#### ~~M4: Separate output by category~~ DONE v0.4.0
**Implemented in:** `session/state.py:OutputEntry`, `tools/output.py` (category filter on all 3 tools)

#### ~~M5: Surface stopped event description/text~~ DONE v0.4.0
**Implemented in:** `session/manager.py:_on_stopped`, `server.py:_build_stopped_response`

#### M6: ~~Add goto/gotoTargets~~ BLOCKED
**Status:** BLOCKED — netcoredbg does NOT support `goto`/`gotoTargets`.

#### M7: ~~Add loadedSources~~ BLOCKED
**Status:** BLOCKED — netcoredbg does NOT support `loadedSources`.

#### ~~M8: Handle module events~~ DONE v0.4.0
**Implemented in:** `session/manager.py:_on_module`, `dap/events.py:ModuleEventBody`, `session/state.py:ModuleInfo`

### LOW Priority

#### ~~L1: Query adapter capabilities before sending requests~~ DONE v0.4.0
**Implemented in:** `dap/client.py:capabilities` property, `tools/debug.py:terminate_debug` (capability check)

#### L2: Add stepInTargets
#### L3: Handle continued event body (allThreadsContinued)

#### ~~L4: Add terminate request (graceful shutdown)~~ DONE v0.4.0
**Implemented in:** `dap/client.py:terminate()`, `tools/debug.py:terminate_debug`

#### L5: Support variable paging (large collections)
#### L6: Parse output variablesReference (structured data)

### Quick Wins (already researched)

#### ~~Q1: quick_evaluate tool~~ DONE v0.4.0
**Implemented in:** `session/manager.py:quick_evaluate`, `tools/inspection.py:quick_evaluate`

## From PR Reviews (2026-04-04)

#### ~~PR24: Review findings pending~~ DONE
**Resolved in:** PR #24 merged with all review findings addressed.

---

## Roadmap: v0.5.0 — Agent Intelligence & UI Gaps (from competitor analysis 2026-04-04)

Reference: `.agent/data/competitor-analysis-detail.md`

### v0.5.0: Exception Autopsy + Context Tools (HIGH)

#### R1: Exception autopsy tool (`exception_get_context`)
**What:** Single tool call that returns: exception type, message, isFirstChance, inner exception
chain (with depth), stack frames with source locations, local variables for N top frames.
Replaces 3-4 sequential tool calls (get_exception_info + get_call_stack + get_scopes + get_variables).
**Why:** Agent wastes 4+ tool calls per exception. debug-mcp proved this pattern works.
**How:** Use DAP `evaluate` with `$exception.GetType().FullName`, `$exception.Message`,
`$exception.StackTrace`, walk inner chain via `$exception.InnerException.*`.
Combine with existing get_stack_trace + get_variables for locals.
**Effort:** M — compose existing DAP calls, new tool + service.

#### R2: Context autopsy on stop (`get_stop_context`)
**What:** When stopped at ANY breakpoint (not just exception), one call returns:
stop reason, stack trace with source context, locals in top frame, hit count,
recent output (last 10 lines). Replaces the manual inspect-resume cycle.
**Why:** Agent on screenshot does 5+ tool calls every time it stops. Should be 1.
**Effort:** M — compose existing calls.

#### R3: Execution flow tracing via tracepoints
**What:** Set non-stopping breakpoints that log an expression's value.
`add_tracepoint(file, line, expression)` → logs `{file}:{line} → {value}` to a
separate trace buffer without pausing. `get_trace_log()` returns ordered entries.
**Why:** Agent can trace execution flow across many lines without manual step-by-step.
Enables "set 10 tracepoints → continue → read flow" workflow.
**How:** DAP `setBreakpoints` with `logMessage` — BLOCKED on netcoredbg.
Alternative: client-side tracepoints using `quick_evaluate` pattern
(pause briefly, evaluate, resume, log). ~50ms per hit.
**Effort:** H — complex timing, needs async queue.

### ~~v0.5.0: UI Tools Expansion (from Winapp-MCP analysis)~~ DONE PR #26

#### ~~R4: `ui_invoke` — InvokePattern (no mouse movement)~~ DONE v0.5.0
**Implemented in:** `tools/ui.py:ui_invoke`, `bridge/Commands/PatternCommands.cs:InvokeElement`

#### ~~R5: `ui_toggle` — TogglePattern for CheckBox/ToggleButton~~ DONE v0.5.0
**Implemented in:** `tools/ui.py:ui_toggle`, `bridge/Commands/PatternCommands.cs:ToggleElement`

#### ~~R6: `ui_file_dialog` — Standard Windows Open/Save dialog~~ DONE v0.5.0
**Implemented in:** `tools/ui.py:ui_file_dialog` (multi-strategy: set_value id=1148 → keyboard Alt+N → invoke id=1 → Enter)

#### ~~R7: `root_id` parameter on all find/click tools~~ DONE v0.5.0
**Implemented in:** 11 tools with `root_id` param, `bridge/Commands/ElementCommands.cs:ResolveSearchRoot`

#### ~~R8: XPath element search~~ DONE v0.5.0
**Implemented in:** 11 tools with `xpath` param, `bridge/Commands/ElementCommands.cs:FindByXPath` with matchCount warning

**Remaining from UI expansion (post-merge):**
- T030: WPF SmokeTestApp with checkbox/invoke button scenarios (needs new WPF project)
- T031: Smoke test checks for new tools (needs real GUI runtime)
- T033: Scoped search performance measurement (needs real UI tree with 100+ elements)

### ~~v0.5.1: Advanced Debugging (from debug-mcp features)~~ DONE

#### ~~R9: State snapshots + diff~~ DONE v0.5.1
**Implemented in:** `session/snapshots.py:SnapshotManager`, `tools/inspection.py:create_snapshot, diff_snapshots, list_snapshots`

#### ~~R10: Collection analyzer~~ DONE v0.5.1
**Implemented in:** `tools/inspection.py:analyze_collection` (count, type, nulls, duplicates, numeric stats, first/last N)

#### ~~R11: Object summarizer~~ DONE v0.5.1
**Implemented in:** `tools/inspection.py:summarize_object` (recursive get_variables, depth tracking, circular ref detection)

#### R3-alt: Client-side tracepoints DONE v0.5.1
**Implemented in:** `session/tracepoints.py:TracepointManager`, `tools/inspection.py:add_tracepoint, remove_tracepoint, get_trace_log, clear_trace_log`
**Note:** netcoredbg does NOT support DAP logMessage. Tracepoints use client-side pause→evaluate→resume (500ms timeout, rate limiting 10/sec).

### Remaining (LOW priority)

#### L2: Add stepInTargets
#### L3: Handle continued event body (allThreadsContinued)
#### L5: Support variable paging (large collections)
#### L6: Parse output variablesReference (structured data)

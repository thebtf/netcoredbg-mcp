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

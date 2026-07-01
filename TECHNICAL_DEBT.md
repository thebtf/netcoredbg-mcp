# Technical Debt — netcoredbg-mcp

## Strategic: Python → C#/.NET platform migration (strangler-fig, deferred)

**Decision (2026-07-01, operator-approved):** the two-language split (Python core
+ C# FlaUI bridge over JSON-RPC) is a standing architectural debt. Long-term
direction is a single .NET platform. Approach is **strangler-fig, NOT rewrite**:
gradually "strangle" Python module by module while the product keeps working —
never a from-scratch rewrite (94k LOC of working, bug-fixed code + 52k LOC tests
would be thrown away).

**Why it makes sense:** the product debugs .NET and the UI layer is already C#;
Python exists only because FlaUI needed C#. Removing the split deletes the
JSON-RPC bridge tax, makes FlaUI native, ships one self-contained `.exe` (no
pip + dotnet-SDK-for-bridge-build), and matches the .NET audience. Official C#
MCP SDK is mature (`/modelcontextprotocol/csharp-sdk`, verified 2026-07-01);
netcoredbg DAP is language-agnostic over stdio; all Python deps have .NET
equivalents (pywinauto→FlaUI native, Pillow→ImageSharp, jsonpath-ng→Json.NET,
psutil→System.Diagnostics, pyyaml→YamlDotNet).

**Process-supervision argument (added 2026-07-01, from live incident):** the
Python packaging forces a wrapper launch chain
`mcp-mux → uv run → python launcher → python worker (asyncio MCP loop)`. That is
4 process layers, and it is exactly why mcp-mux went blind during the 2026-07-01
hang: mux supervises its direct child (the `uv` launcher), but the MCP event
loop lives in the grandchild; a dead loop left the launcher alive with
`pending: 0`, so every call timed out for 12–17h while mux reported healthy (see
mcp-mux issue #355). A native self-contained .NET binary collapses the chain to
`mcp-mux → netcoredbg-mcp.exe` — one process, and it IS the one holding the MCP
loop. mux then supervises the right process, `pending` stops lying, reap hits the
target, and orphan `uv` launchers cannot exist. The two-language split does not
just cost bridge latency — it inserts an interpreter+wrapper layer that defeats
process supervision.

**Why NOT now:** v0.21.0 is stable, runtime-smoke v2 (18k LOC `session/`) just
shipped — rewriting the newest/most-complex code is peak regression risk for
zero user payoff. Trigger for starting is a concrete pain, not aesthetics:
JSON-RPC bridge latency becomes a bottleneck, maintaining Python+C# starts
blocking delivery, users hit friction on the pip+dotnet install path, or the
wrapper-launch supervision gap keeps causing hung-upstream incidents like
2026-07-01 (#355). That last one is now a NAMED, observed trigger — not
hypothetical — which moves this debt from "someday" toward "when the next
supervision incident lands, start the strangler-fig."

**Migration order when triggered (strangler-fig):**
1. Stand up a C# MCP host beside the Python server; proxy tool-by-tool into
   existing Python.
2. Migrate UI/FlaUI tools first (already C# — bridge tax vanishes immediately =
   real early payoff).
3. Migrate `dap/` then `session/` per-module, each under green tests.
4. Python fades module by module; product works every day.

Sizing (2026-07-01): Python src 41,460 LOC (125 files) — `session/` 18k,
`tools/` 9.3k, `ui/` 6.9k, `dap/` 955; tests 52,614 LOC; existing C# 8,571 LOC.

## From DAP Coverage Gap Analysis (2026-04-04)

Full reports: `.agent/data/mcp-dap-coverage-gaps.md`, `.agent/data/netcoredbg-dap-capabilities.md`

> **Status note (2026-07-01):** All non-BLOCKED items in this section are DONE
> (shipped through v0.5.x; project is now on v0.21.0). The remaining open rows
> are `BLOCKED` on upstream netcoredbg capability gaps (logMessage,
> breakpointLocations, dataBreakpoints, goto/gotoTargets, loadedSources).
> Current adapter: `netcoredbg 3.1.3-1 (8b8b222)`. These BLOCKED rows must be
> re-audited against the DAP `initialize` capabilities whenever netcoredbg is
> upgraded — do not assume they are still blocked on a newer build.

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

#### ~~L4: Add terminate request (graceful shutdown)~~ DONE v0.4.0
**Implemented in:** `dap/client.py:terminate()`, `tools/debug.py:terminate_debug`

_(L2 stepInTargets, L3 continued-event, L5 variable-paging, L6 output variablesReference are all DONE v0.5.2 — see the "LOW priority DONE v0.5.2" section below.)_

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

#### ~~R1: Exception autopsy tool (`get_exception_context`)~~ DONE
**Implemented in:** `tools/inspection.py:get_exception_context` (inspection.py:465). Single call returns exception type, message, inner chain, stack frames + locals for top N frames.

#### ~~R2: Context autopsy on stop (`get_stop_context`)~~ DONE
**Implemented in:** `tools/inspection.py:get_stop_context` (inspection.py:503). One call on any stop: reason, stack + source context, top-frame locals, hit count, recent output.

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
**Implemented in:** `tools/ui.py:ui_file_dialog` (multi-strategy: 1. set_value ComboBox id=1148, 2. keyboard Alt+N → Ctrl+A → type path, 3. invoke button id=1 or by name, 4. Enter fallback. Path escaped for SendKeys special chars.)

#### ~~R7: `root_id` parameter on all find/click tools~~ DONE v0.5.0
**Implemented in:** 11 tools with `root_id` param, `bridge/Commands/ElementCommands.cs:ResolveSearchRoot`

#### ~~R8: XPath element search~~ DONE v0.5.0
**Implemented in:** 11 tools with `xpath` param, `bridge/Commands/ElementCommands.cs:FindByXPath` with matchCount + warning on multiple matches. `FindElement` delegates to XPath when xpath is the only criterion.

**Remaining from UI expansion (post-merge):**
- ~~T030: WPF SmokeTestApp with checkbox/invoke button scenarios~~ DONE — `tests/fixtures/WpfSmokeApp` (+ SmokeTestApp, AvaloniaSmokeApp) exist and build.
- ~~T031: Smoke test checks for new tools~~ DONE — manual smoke suite covers new UI tools (227 checks as of v0.21.0).
- T033: Scoped search performance measurement — NON-GOAL. Perf timing on 100+ element trees is low value for an agent-facing debugger; deprioritized unless a concrete latency complaint appears.

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

### ~~LOW priority~~ DONE v0.5.2

#### ~~L2: Add stepInTargets~~ DONE v0.5.2
**Implemented in:** `tools/debug.py:get_step_in_targets`, `tools/debug.py:step_into(target_id=)`, `dap/client.py:step_in_targets`

#### ~~L3: Handle continued event body (allThreadsContinued)~~ DONE v0.5.2
**Implemented in:** `session/manager.py:_on_continued` — clears current_thread_id when allThreadsContinued

#### ~~L5: Support variable paging (large collections)~~ DONE v0.5.2
**Implemented in:** `tools/inspection.py:get_variables(filter=, start=, count=)`, `dap/client.py:variables(filter, start, count)`

#### ~~L6: Parse output variablesReference (structured data)~~ DONE v0.5.2
**Implemented in:** `session/manager.py:_on_output`, `session/state.py:OutputEntry.variables_reference`

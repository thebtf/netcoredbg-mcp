# Technical Debt — netcoredbg-mcp

## Strategic: Python → C#/.NET platform migration (strangler-fig, in progress)

**Decision (2026-07-01, operator-approved):** the two-language split (Python core
+ C# FlaUI bridge over JSON-RPC) is a standing architectural debt. Long-term
direction is a single .NET platform. Approach is **strangler-fig, NOT rewrite**:
gradually "strangle" Python module by module while the product keeps working —
never a from-scratch rewrite (94k LOC of working, bug-fixed code + 52k LOC tests
would be thrown away).

**Status (2026-07-15):** the first compatibility facade is merged through PR
#227 at `952b5a1`. `host/NetCoreDbg.Mcp.Host` is a .NET 8 stdio MCP process
that launches the unchanged Python server and dynamically proxies its
`tools/list` and `tools/call` contracts. Post-merge Release build and real MCP
SDK initialize/list/call evidence pass. The Python console entrypoint remains
the published default, and no tool family has moved to native C# yet. The next
integration wave is front-door parity: downstream roots (Engram #385), upstream
progress/log notifications (Engram #386), and a resources/prompts/callback audit.
No entrypoint cutover is allowed until that wave passes.

**Executable migration backlog (strangler-fig):**

These are delivery units with explicit dependencies, not permission for a
from-scratch rewrite. Once R0 is delivered, R1/R2/R3 research and contract
work (MCP semantics, schemas, parity-test design), Q0 (critical-suite host
smoke), and UX0 (installed-consumer playbook prep) can all fan out
immediately in parallel. R1 and R2 *implementation* both depend on R0.5
(FD-000) — one shared bidirectional relay seam — and only become disjoint,
parallel work once R0.5 lands: R1 owns the roots request/response path, R2
owns the notification push path, and neither owns the seam itself. R4-R6
share one process-wide `SessionOwnership`/`SessionManager`/
`ProcessRegistry`/`SessionTempManager` singleton across UI/replay, DAP, and
lifecycle state, so they migrate as strictly sequential, single-owner waves.
The R4 decision is made, not open: once R1+R3 land, the `code_search`/
static-prompts pilot proves native route mechanics only, while SS-000 — the
session-state/ownership seam ADR, starting after R0.5+R3 and running
alongside the pilot and R2 — decides the seam itself; R4 does not start
until that decided seam is implemented (see "R4/R5 sequencing risk" below).
`code_search` and static prompts are exempt from the seam: `code_search` has
zero `SessionOwnership`/`check_session_access` coupling, but its current
root resolution writes `SessionManager.project_path` (a native `code_search`
must resolve roots locally and never write that shared field) using MCP
client roots the same way Python does; preparation can start once R3 clears,
but cutover is root-scoped and also needs R1 roots parity, and may overlap
R2. Static prompts have no session dependency at all and may go native once
R3 clears alone. R7 depends on R4-R6 finishing plus Q0 and UX0 both passing,
and R8 (Python retirement) is gated on R4-R7 all passing.

| Unit | Status / owner | Scope | Exit gate |
|---|---|---|---|
| R0. Compatibility facade | **DELIVERED** — F-004 CR-001/CR-002, Engram #384, PR #227 | Freeze `runtime_smoke_*`; add the .NET host; proxy typed `tools/list` and `tools/call` to Python. | Merged-main build and real SDK initialize/list/call pass; Python remains published. |
| R0.5 (FD-000). Bidirectional relay seam | **PLANNED** — blocks R1/R2 implementation | One maker/checker pair restructures `host/NetCoreDbg.Mcp.Host/Program.cs` — today one static class holding `Main`, a single `RunProxyAsync` handler, and process management in one file — into a small composition/startup point plus capability-specific relay files, and owns the common relay abstractions and test fixtures needed to represent the direction `tools/call` does not cover: Python-initiated requests needing a client response (e.g. `roots/list`) and Python-initiated one-way pushes (e.g. progress/log notifications); claims no feature behavior itself. | The route model represents both MCP directions, capability negotiation, and request/notification correlation under shared cancellation/timeout rules, without changing current tools-only behavior; the composition point lets R1, R2, and required R3 proxies land additively as separate files/PRs instead of merging into one shared handler body. |
| Q0. Host proxy critical gate | **PLANNED** — parallel now | Promote a minimal real .NET-host initialize/list/call smoke into `tests/critical` (joining `test_release_critical.py`, `test_runtime_smoke_v2_critical.py`, `test_stealth_critical.py`); R1, R2, and R3 each extend it with their own proxied behavior once landed. | The critical suite includes a real stdio exchange against the .NET host, not only direct Python; Q0 passes before front-door integration is accepted and before R7. |
| UX0. .NET installed-consumer UXDD playbook | **PLANNED** — prep parallel now, final validation at R7 | Extend `docs/PRODUCTION-TESTING-PLAYBOOK.md` and its harness with a candidate self-contained .NET entrypoint journey, prepared ahead of time so the release gate is not designed from scratch at cutover. | At R7, the extended playbook journey against the candidate .NET entrypoint reports `PRODUCT_WORKS`; R7 cannot proceed without it. |
| R1. Downstream roots parity | **OPEN** — Engram #385, after R0.5 | Relay downstream MCP roots through the host without callback deadlock. | A scoped Python tool observes a downstream root different from host cwd; explicit project modes remain unchanged. |
| R2. Progress/log parity | **OPEN** — Engram #386, after R0.5 | Relay upstream logging notifications and progress notifications with monotonically increasing `progress` per `progressToken`, per the MCP spec — not a global order across tokens or notification types. | Real downstream client receives equivalent, correctly-scoped notifications; gate 2's cross-cutting contracts (final results, cancellation, `_meta`, stdout purity) hold. |
| R3. Remaining protocol-surface audit | **PLANNED** | Audit capability/version/init fidelity; `notifications/tools/list_changed`; resource templates/subscriptions/`notifications/resources/list_changed`/`notifications/resources/updated`; `notifications/prompts/list_changed`; `completion/complete`; sampling; elicitation; experimental capabilities (e.g. tasks) if negotiated; `ping`; and bidirectional cancellation/timeouts, across the existing 8 prompts and 4 resources. Classify every surface as public, retired, or parity-required. | Every audited surface and capability is classified public, parity-required, or retired-with-evidence; the host's advertised `initialize` capabilities match Python's for every negotiated feature. A documented gap on a still-consumer-visible surface is non-terminal: it blocks front-door acceptance and becomes a bounded R3P slice. |
| R3P. Required protocol-surface parity slices | **PLANNED** — after R0.5 and per-surface R3 classification; may run parallel with R1/R2 when files are disjoint, but common composition stays integration-owned | Implement and test every surface R3 classifies parity-required (e.g. resource templates/subscriptions, prompts, `completion/complete`, sampling, elicitation, negotiated experimental callbacks, as applicable); retirement instead of implementation is accepted only with consumer evidence that the surface is unused. | Zero unresolved advertised/negotiated surfaces remain: every one is proxied and tested, native and tested, or explicitly retired with consumer evidence. |
| SS-000. Session-state/ownership seam ADR | **PLANNED** — after R0.5+R3; runs beside the `code_search`/static-prompts pilot and R2 | Decide the cross-process session-state/ownership seam gate 6, R4, and R5 require: authoritative owner, claim/release, stale/crash recovery, read-only state projection (e.g. proxying `debug://state`/`debug://threads` instead of duplicating them), write latency, bridging `JsonRpcHandler`'s static-per-process state to per-session instances, host TFM/RID, and sub-CR boundaries for the families that will consume it. | The ADR is reviewed and merged; R4 does not start until the seam it specifies is implemented, not merely decided. |
| R4. Native UI/FlaUI routes | **PLANNED** — after front-door parity (R1, R2, R3, and all R3P slices), the accepted `code_search`/static-prompts pilot, SS-000 implemented, and the mandatory bridge sub-CR; NOT a lift-and-shift, see "R4/R5 sequencing risk" below | Move the 40+-tool `ui.py`/`ui_evidence.py` family behind native C# handlers, one bounded family at a time, and remove the corresponding bridge route. | Gate 6 satisfied; exactly one owner per public name; schema, result, error, timing, session, runtime-replay, cleanup, and rollback parity pass. |
| R5. Native DAP routes | **PLANNED** — after R4 | Move `dap/`-adjacent families (`debug`, `breakpoints`, `inspection`, `memory`, `process`, `enc`) module by module while unported names continue to proxy to Python; shares R4's singleton-forking risk (gate 6). | Gate 6 satisfied; each family passes contract/state/error/timing/cancellation/cleanup parity before its Python route is removed. |
| R6. Native session and remaining tool routes | **PLANNED** — after R5, except `code_search` (prep after R3, cutover after R1+R3) and static prompts (after R3 alone) | Move `session/`, ownership/lifecycle infrastructure, and every remaining public tool family to C#; `code_search` (zero `SessionOwnership` coupling, but must resolve roots locally instead of writing `SessionManager.project_path`; cutover needs R1 too, since it resolves MCP client roots the same way Python does) and static prompts (no session dependency; needs only R3) do not wait for R4/R5. | No public tool, prompt, resource, callback, or session behavior depends on a Python-owned route. |
| R7. .NET package/entrypoint cutover | **PLANNED** — after R4-R6, Q0, and UX0, plus reviewed release CR | Publish the self-contained .NET executable as the consumer entrypoint. If any proxy route remains, Python stays an explicit runtime dependency. | Front-door wave passes; Q0 and UX0 both pass, with UX0 reporting `PRODUCT_WORKS` against the candidate .NET entrypoint; packaging/install smoke passes; rollback is proven. |
| R8. Final Python retirement | **PLANNED** — only after R4-R7 | Remove Python launch, runtime package, source, build, test, and documentation surfaces after the last route has moved. | Zero Python-owned public routes or runtime imports/processes; full installed-consumer, parity, critical, packaging, and release gates pass. |

**Non-negotiable migration gates:**

1. The facade route table has exactly one owner for every public name: native C#
   or proxied Python, never both.
2. Public names, schemas, annotations, final results, errors, cancellation,
   `_meta`, notifications, roots, stdout/stderr separation, cleanup, and
   session ownership are cross-cutting contracts verified for every unit
   R0, R0.5, R1-R8, and every R3P route slice, not only the unit that first
   introduces them; notification ordering is scoped to
   progress-per-`progressToken` monotonicity (R2), not a global order
   across tokens or notification types.
3. Tests are mandatory supporting evidence, never sufficient alone: the
   installed/public consumer UXDD journey reaching `PRODUCT_WORKS` is the
   primary gate for entrypoint cutover and for Python deletion.
4. Every subtractive native cutover — R4-R6 and the `code_search`/
   static-prompts pilot alike — is one self-contained CR: Python remains
   authoritative until that family's reviewed parity gate passes, and its
   implementation/tests stay present but unregistered — not deleted — until
   R8. Before merge, an isolated revert proof restores Python ownership and
   removes native registration without leaving duplicate owners; the
   recorded revert-PR recipe is the post-merge rollback route.
5. Python source, packaging, build, and docs are removed only inside R8, and
   only after R4-R7 exit gates all pass; no earlier unit deletes Python code.
6. A tool family may go fully native only when it owns equivalent state
   itself (e.g. `code_search`) or a proven cross-process session-state/
   ownership seam exists; no native route may fork `SessionOwnership`,
   `SessionManager`, `ProcessRegistry`, or `SessionTempManager` into two
   independent trackers.

**R4/R5 sequencing risk (2026-07-15, architecture review):** most mutating
tool families — `debug`, `breakpoints`, `inspection`, `memory`,
`runtime_smoke`, `ui`, `ui_evidence`, `process`, `enc` — share one
process-wide `SessionOwnership` singleton (`server.py:114`, threaded via
`check_session_access`/`ownership=` into `register_*_tools` at
`server.py:268-339`) plus `SessionManager`/`DebugState`/`ProcessRegistry`/
`SessionTempManager`. `tools/ui.py` alone has 30+ `check_session_access`
call sites plus direct `session.process_registry`/`session.state.process_id`/
`session.temp_manager` reads. Moving UI (R4) or DAP (R5) into the host
process while the other stays in Python would fork that singleton into two
independent trackers unless a cross-process session-state/ownership seam
exists first. The R4 decision is made, not open: once R1+R3 land, the
`code_search`/static-prompts pilot proves native route mechanics only — it
is not a substitute for the seam. SS-000 (the session-state/ownership seam
ADR) starts once R0.5+R3 land, runs alongside the pilot and R2, and decides
the seam itself; R4 does not start until that decided seam is implemented.
`code_search` preparation starts after R3 clears and may overlap R1/R2
implementation; cutover requires R1 roots parity and may overlap unfinished
R2. Separately, removing the JSON-RPC bridge tax (not relocating it) is a
mandatory, isolated pre-R4 sub-CR — not a deferrable follow-up — that
refactors `JsonRpcHandler`'s static `_mainWindow`/`_automation`/
`_processId`/`Stealth`/`HeldModifiers` fields (a documented
process-per-session invariant) into per-session instances and aligns the
host's TFM/RID/`UseWPF` with the bridge's `net8.0-windows`/`win-x64`
self-contained profile; R4 requires the accepted pilot, this sub-CR, and
the implemented SS-000 seam.

**Compatibility boundary:** this first host slice proxies only `tools/list` and
`tools/call`; it does not relay downstream MCP roots, progress/log
notifications, or other client callbacks. Until later reviewed CRs add that
front-door parity, launch the host with an explicit `--project`, set
`NETCOREDBG_PROJECT_ROOT`, or use `--project-from-cwd` from the intended project
directory. Published-entrypoint cutover is blocked on roots (#385),
notifications (#386), and any still-consumer-visible resources/prompts, so the
current Python entrypoint retains its direct client-context behavior.

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

**Why migration remains incremental:** the observed supervision failure (#355)
was enough to start the strangler host, but it does not justify rewriting the
newest and most complex Python code. Each boundary must provide concrete user or
operational value and retain green parity evidence; working modules move only
when their owning CR is reviewed.

**Sizing snapshot (2026-07-01, pre-facade):** Python src 41,460 LOC (125
files) — `session/` 18k, `tools/` 9.3k, `ui/` 6.9k, `dap/` 955; tests
52,614 LOC; C# 8,571 LOC before the CR-002 host was added. The backlog table
above is the authoritative migration order; this snapshot is historical
scale context only.

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

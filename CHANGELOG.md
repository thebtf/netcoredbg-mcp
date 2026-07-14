# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Selector-scoped pointer hover through the direct `ui_hover` tool and the
  runtime-smoke v2 `ui.hover` action. The Windows FlaUI route requires one
  uniquely resolved foreground target and returns exact-window, unchanged-focus,
  pointer, hit-test, timeout, and mutation-state evidence without activating the
  window or sending button input. `no_global_input` blocks before pointer
  movement, and unsupported pywinauto use returns bounded evidence.
- Read-only `inspect_debug_launch_compatibility(program)` diagnostics for the
  target runtime, active `dbgshim`, cached same-major candidate, and predicted
  launch-time replacement without building, launching, claiming ownership, or
  mutating the shared debugger directory (Engram #380, CR-113).
- Read-only `debuggee_activity(window_ms)` telemetry over adapter-owned
  continued/stopped/step, output, module, and trace deltas with same-epoch and
  cancellation safety. Executed-instruction counts are reported explicitly as
  unavailable rather than estimated or fabricated (Engram #356, CR-111).

## [0.22.0] - 2026-07-03

### Added
- Adapter-native debuggee liveness telemetry on `get_debug_state` (Engram FR
  #356, Tier 1 of 3). New additive fields let an agent tell running vs
  stopped-at-breakpoint vs stopped-at-exception vs terminated through adapter
  calls only, without falling back to Win32 `Get-Process`/`Process.Responding`
  (which reports a live debuggee as "not responding" when it is under the
  debugger, running automated input replay, windowless, or in a nested modal
  loop):
  - `execState` — a single fused field derived from the debug state plus stop
    reason (`running`, `stopped-at-breakpoint`, `stopped-at-exception`,
    `stepping`, `stopped-at-pause`, `stopped-other`, `terminated`, and the
    lifecycle states passed through).
  - `lastResumeAt` / `lastStopAt` — monotonic transition timestamps stamped in
    the existing continued/stopped event handlers.
  - `threadCount`, `debuggeeAlive` (the adapter's own view, not `Get-Process`),
    and `debuggeePid` (from the tracked DAP process event).
  All fields are additive; no new DAP requests or netcoredbg capabilities are
  required. `execState: running` holds even when Win32 `Responding` is `False`,
  and a debuggee parked at a breakpoint reports `stopped-at-breakpoint` with the
  reason and location instead of looking hung.

## [0.21.0] - 2026-07-01

### Added
- Runtime-smoke v2 no-operator input monitoring now separates emulated runner
  input from real external keyboard/mouse input at the source. A dedicated
  low-level input event recorder captures each event's provenance, and runner
  injection paths are stamped with a shared `RunnerInputSignature`
  (`bridge/Commands/InputSignature.cs`) so runner-signed events classify
  `CLEAN_PROVEN` while foreign-injected and physical events classify
  `DIRTY_UNPROVEN`.

### Changed
- `run_confidence` replaces the previous `windows_last_input_info` window
  heuristic and the `RUNNER_GLOBAL_INPUT_AMBIGUOUS` verdict with 3-way
  event-stream attribution (runner-injected / foreign-injected / physical). An
  empty-but-present event stream is proven `CLEAN`; a malformed or absent stream
  fails closed as `DIRTY_UNPROVEN` / unproven.
- Documented runner-controlled global-input ambiguity for no-operator
  runtime-smoke plans that intentionally allow covered `ui.drag` input.

### Fixed
- Function breakpoint clearing now rolls back on a failed
  `_sync_function_breakpoints` response instead of silently reporting success,
  restoring the intended clear/add error propagation (#220, #80).

## [0.20.5] - 2026-06-22

### Changed
- Republished README, Russian README, production playbook, runtime-smoke
  example, and release notes for the shipped `v0.20.4` no-operator
  input-monitor documentation surface.

## [0.20.4] - 2026-06-22

### Added
- Runtime-smoke v2 now ships a default `runtime.input_monitor.check` adapter on
  Windows, backed by current desktop-session `GetLastInputInfo` evidence for
  `run_confidence.no_operator` runs.

### Changed
- Missing input-monitor adapter evidence no longer blocks ordinary
  `runtime_smoke_run_plan` no-operator plans on supported Windows desktop
  sessions; clean windows report `CLEAN_PROVEN`, while detected input or
  unsupported environments still fail closed as dirty or unproven.

## [0.20.3] - 2026-06-22

### Changed
- Recorded the post-`v0.20.2` downstream-wait boundary so provider-code work is
  not reopened without fresh downstream provider `FAIL` evidence.
- Updated README release copy and release notes to distinguish shipped provider
  readiness from external NovaScript acceptance and broader full-isolation
  roadmap scope.

## [0.20.2] - 2026-06-22

### Added
- Runtime-smoke v2 plans can request `run_confidence.no_operator` evidence for
  no-operator runs, backed by the testable `runtime.input_monitor.check`
  adapter contract.

### Changed
- No-operator runtime-smoke runs now distinguish clean product verdicts from
  dirty or unproven operator-contaminated windows with bounded
  `run_confidence` evidence.

### Fixed
- Dirty or unproven no-operator scenario evidence now returns terminal
  `BLOCKED` with restart guidance instead of being recorded as product `FAIL`.
- Unknown non-`PASS` confidence statuses fail closed as `BLOCKED` instead of
  falling through to `PASS`.

## [0.20.1] - 2026-06-22

### Added
- Runtime-smoke v2 plans can declare `input_policy.no_global_input` to request
  operator-isolated action execution.
- Runtime-smoke v2 action and result evidence now records input policy, action
  input classification, physical fallback attempt state, and operator-isolated
  status.

### Changed
- Runtime-smoke v2 dispatch now classifies action routes as `BACKGROUND_SAFE`,
  `APP_DISPATCH_SAFE`, `REQUIRES_GLOBAL_INPUT`, or `UNSUPPORTED_BY_PROVIDER`.
- Built-in `ui.click` remains available under `no_global_input` through the
  existing app-dispatch/UIA invoke route instead of being treated as physical
  pointer input.

### Fixed
- Physical/global-input runtime-smoke actions now return `BLOCKED` before
  focus, keyboard, mouse, drag, cursor, or physical fallback adapter calls when
  `input_policy.no_global_input` is active.
- Malformed action-level `input_policy` payloads fail validation before action
  dispatch instead of reaching adapters.

## [0.20.0] - 2026-06-22

### Added
- Named `oracle_pack` and `app_diagnostics` runs now expose bounded
  `pack_manifest` descriptors through `runtime_smoke_run_plan`,
  `runtime_smoke_run_probe`, `runtime_smoke_evidence_bundle`, and
  `runtime_smoke_get_event_delta`.
- Runtime-smoke evidence directories can materialize `pack-manifest.json` for
  named packs when final evidence is available, with per-source classifications
  and cleanup, freshness, redaction, and limits rollups.
- Provider docs now track the NovaScript action-oracle app-diagnostics replay
  as downstream `PASS`, including the adapted `ui.grid.select` action and
  bounded broad-issue keep-open decisions.

### Changed
- Release and reproduction docs now preserve explicit no-repeat boundaries for
  CR-100 through CR-104 so broad `#268..#272` work is not re-opened from stale
  roadmap text.
- Agent workflow docs now include worktree trace and reproduction-first
  debugging protocols used by release/readiness sessions.

### Fixed
- Pack manifest refs now reject unsafe absolute or Windows drive-qualified
  source refs portably and keep app diagnostics artifact refs relative or
  omitted when no safe artifact is available.
- Final evidence bundles and event deltas refresh pack manifest status from the
  retained final result so a before-phase `PASS` cannot mask a later
  `FAIL` or `BLOCKED`.
- Multi-pack selection now preserves the selected failing or blocking pack id
  instead of assigning another pack's status to the first pack.
- Source distributions now exclude local agent scratch directories, virtual
  environments, build outputs, and fixture `bin` / `obj` residue so release
  artifacts cannot ship workstation-only evidence or dependencies.

## [0.19.0] - 2026-06-21

### Added
- Runtime-smoke v2 now records WPF DataGrid positive row-target drag/drop flows
  with offscreen target realization, viewport preflight evidence, before/after
  `ui.grid.viewport` proof, and bounded negative no-op backend limitations.
- Runtime-smoke app-diagnostics event deltas, live diagnostic history, and
  intra-case progress reporting expose richer evidence for customer-mode UI
  replay.
- NovaScript action-oracle templates can generate bounded `app_diagnostics`
  probes for action success oracles without forcing every oracle through
  `file.json`.
- UI grid helpers now cover visible-row viewport evidence, assert-range,
  ensure-visible, right-click row, and double-click row proof paths.

### Changed
- Runtime-smoke wait, run-probe, event-delta, and cleanup flows now preserve
  source-aware cursors and return explicit next-action guidance for empty,
  stale, or mixed diagnostic states.
- Production testing guidance and runtime-smoke examples now document the WPF
  DataGrid drag/drop customer-mode release gate.
- Reproduction/backlog documentation now keeps broad issues `#268` through
  `#272` open while recording the bounded slices and Engram follow-up evidence
  that were completed in this release window.

### Fixed
- Offscreen drag/drop replay now fails closed when source or target evidence is
  hidden, stale, or too close to the visible edge instead of reporting a false
  `PASS`; negative no-op drag still reports actionable bounded `BLOCKED` when
  the backend lacks no-op evidence.
- App-diagnostics and event-delta flows no longer consume stale or cleared
  output as successful evidence.
- Runtime-smoke agent-mode and validation flows keep cleanup and source cursor
  routing explicit so contaminated sessions do not silently retarget shared
  state.

## [0.18.8] - 2026-06-19

### Added
- `app_diagnostics.poll` now accepts an optional `since` cursor
  `{mtime_ns, name}` for directory polling, ignores stale/equal diagnostic
  snapshots, and returns the matched file cursor for the next incremental poll.

## [0.18.7] - 2026-06-19

### Added
- Runtime-smoke v2 now supports verified right-click and double-click actions
  through `ui.right_click_verified` and `ui.double_click_verified`.
- Runtime-smoke operation adapters now expose `ui.right_click` and
  `ui.double_click` through the same verified-click target proof and
  postcondition behavior as `ui.click_verified`.

### Fixed
- Pywinauto fallback click-center resolution now accepts `rectangle` element
  geometry payloads as an alias for `rect`, preserving coordinate-click
  behavior on fallback backends.

## [0.18.6] - 2026-06-19

### Added
- DataGrid row select and click actions now support explicit
  `ensure_visible=True` composition across the public `ui_grid` helper, legacy
  runtime-smoke route, and runtime-smoke v2 action runner.

### Fixed
- Runtime-smoke v2 row actions now normalize unsupported or invalid
  ensure-visible preflight results to terminal failure statuses, preventing
  skipped row actions from silently reporting `PASS`.

## [0.18.5] - 2026-06-19

### Added
- Public `ui_grid(action="viewport")` now exposes bounded visible-row DataGrid
  viewport identity snapshots through the existing runtime-smoke
  `ui.grid.viewport` adapter.

### Fixed
- Direct viewport helper calls now reject comparison-only expectations such as
  `viewport_moved` or `direction`, preventing a single-snapshot helper call from
  silently reporting `PASS` for checks that require runtime-smoke v2 before/after
  probe state.

## [0.18.4] - 2026-06-19

### Added
- `app_diagnostics.poll` now consumes an explicit evidence directory with a
  file-name glob pattern such as `diagnostic-*.json`, preserving matched path,
  observation, poll count, and timeout metadata.
- `app_diagnostics.wait_json` now supports a bounded
  `{jsonpath, expected}` condition contract and keeps polling until the
  app-written diagnostic JSON reaches the requested oracle state.

### Fixed
- Directory poll candidates are revalidated through the session path policy
  before read, preventing out-of-scope or symlinked matches from being consumed.
- `wait_json.condition` compares JSON values type-safely, so booleans no
  longer match numeric `0` / `1` expectations through Python equality.

## [0.18.3] - 2026-06-19

### Fixed
- Runtime-smoke v2 runner-exception finalization now records raised case
  cleanup adapter exceptions as cleanup failure evidence and continues to
  plan-level cleanup.
- Plan-level cleanup declared in v2 failure plans, including `debug.stop` and
  `process.registry.assert_empty`, remains attempted after case cleanup
  failures so failure evidence cannot silently skip process hygiene.
- Cleanup adapter exception evidence now includes traceback diagnostics
  alongside exception type and message.

## [0.18.2] - 2026-06-19

### Added
- Runtime-smoke `plan_path` inputs now accept YAML `.yaml` / `.yml` files for
  the existing validate and run-plan facades while preserving JSON behavior.
- Added read-only `runtime_smoke_validate_probe` for single-probe v2 authoring
  and validation without durable run creation, target launch, session ownership,
  or evidence-directory side effects.
- App diagnostics freshness contracts now include top-level `loaded_sources`
  and schema validation for source expectations.

### Fixed
- Durable v2 runtime-smoke runner exceptions now return v2-shaped exception
  evidence, run declared cleanup, preserve cleanup contamination guidance, and
  expose exception type/message instead of falling back to opaque legacy
  teardown.
- App-written diagnostic `PASS` results are checked against live debug freshness
  expectations for process, module, source, workspace, and artifact evidence.
- `wait_json` / `poll` app-diagnostic payload merging preserves caller-declared
  nested freshness expectations when an artifact reports only a partial `app`
  object.

## [0.18.1] - 2026-06-19

### Fixed
- Runtime-smoke `plan_path` validation no longer auto-claims mux ownership
  when the caller only wants a read-only validation result.
- Read-only runtime-smoke plan validation keeps `session.project_path`
  unchanged, so observer-style checks cannot silently retarget the shared
  debug session.
- Project path validation now scopes worktree lookup caching by the supplied
  project root, preventing cross-project stale-cache decisions during release
  and multi-worktree validation.

## [0.18.0] - 2026-06-19

### Added
- Semantic UI automation helpers for monitor events, text and grid evidence,
  focus assertions, read-only property checks, annotated screenshots, verified
  click evidence, DataGrid ensure-visible actions, selected-row evidence, and
  FlaUI focused-element queries.
- Runtime-smoke facades for v2 plan validation, one-call plan execution,
  diagnostic probes, event cursors, wait results, debug preflight, tracepoint
  policy guardrails, diagnostic orchestration, app diagnostics, agent-mode
  defaults, trace cursor deltas, and plan-file inputs.
- Reproduction and lifecycle ledgers for the issue-backlog hardening roadmap,
  including portable NovaScript replay packets and release-readiness evidence.

### Fixed
- Runtime-smoke cleanup, selector safety, text replacement, textbox state
  oracles, single-flight cleanup, and plan-file validation now fail closed with
  bounded evidence instead of leaking stale or ambiguous state.
- FlaUI bridge and UI helpers now handle stale bridge sessions, pointer routing,
  selection compatibility, screenshot orientation, transient focus exceptions,
  and target-process focus boundaries.
- Tracepoint cursor and policy handling now preserves cursor boundaries,
  disambiguates same-timestamp events, reports stale dropped counts, and avoids
  unsafe tracepoint reuse.

### Changed
- Release preparation now has a project-specific release protocol covering
  PyPI/TestPyPI, GitHub Release, version parity, release notes, critical-suite,
  production playbook, local deploy smoke, and post-tag publication evidence.

## [0.17.2] - 2026-05-17

### Fixed
- Runtime-smoke v2 `ui.drag` now resolves WPF DataGrid viewport endpoints via
  `grid_snapshot` only after selector miss/bounds miss, while preserving
  blocked ambiguous/backend selector failures.
- Runtime-smoke v2 cleanup now stops debug sessions before process-registry
  assertions and preserves cleanup evidence across elapsed-time budget
  exhaustion.
- Runtime-smoke v2 budget validation now rejects malformed action and elapsed
  budgets instead of coercing invalid values or raising uncaught parser errors.

## [0.17.1] - 2026-05-15

### Fixed
- `setup --enc` now installs the portable `3.1.3-1062-enc.2` netcoredbg
  release, so managed installs load `ncdbhook.dll` from
  `~/.netcoredbg-mcp/netcoredbg` without requiring `NETCOREDBG_PATH`.

## [0.17.0] - 2026-05-15

### Added
- Live Edit-and-Continue apply path that enables Hot Reload, resolves the loaded
  target module, builds Roslyn deltas from the active module baseline, and
  applies IL/metadata/PDB deltas without restarting the debuggee.
- Release regression coverage for loaded-module baselines, multi-target module
  TFM reference resolution, netstandard reference-pack selection, and line
  update compatibility validation.

### Fixed
- `apply_code_change` now rejects line-changing edits until real `lineUpdates`
  payloads are emitted, while preserving same-line blank replacements.
- Framework reference resolution now derives the TFM from the loaded module path
  before falling back to the project file and probes POSIX dotnet install roots.

## [0.16.0] - 2026-05-15

### Added
- Runtime smoke v2 drag/drop scenarios for visible-row reorder, edge-scroll,
  multi-row selected payload preservation, and negative no-op classification.
- Backend route evidence for bridge `Drag`, including path points and final
  pointer coordinates for customer-mode smoke diagnostics.

### Fixed
- Visible-row drag smoke now uses threshold-aware bridge dragging for
  two-point row reorders while preserving route evidence.
- Coordinate drag primitives now settle after moving to the source point before
  pressing the left button, avoiding WPF input races where fresh fixtures stayed
  at `Ready` and never entered `DoDragDrop`.

## [0.15.1] - 2026-05-14

### Added
- Runtime smoke v2 state-only transitions for setup, observation, and cleanup
  plans that do not need a selector-driven UI action.
- A state-only file JSON matrix example covering stable write/read/delete
  routes with fresh run-id oracles.

### Fixed
- `ui_get_window_tree` now returns a bounded structured `BLOCKED` result when
  UI tree enumeration exceeds the discovery timeout.
- Runtime smoke v2 now rejects invalid setup/action shapes and non-integer
  `wait.idle_ms` durations before executing the affected transition.

## [0.15.0] - 2026-05-13

### Added
- Stealth-mode GUI debugging for background-safe launch, click, send-keys,
  screenshot, and explicit bring-to-front workflows.
- Edit-and-Continue support with a Roslyn delta compiler wrapper, DAP
  `applyDeltas` integration, `apply_code_change`, and `setup --enc` packaging.
- Project-scoped code search tools for symbol lookup, references, source
  context, and regex search with `.gitignore` support.

### Fixed
- Code search now rejects external symlink targets and prevents direct source
  context reads from bypassing source-extension and ignore-rule eligibility.

## [0.14.0] - 2026-05-10

### Added
- Runtime smoke v2 state-oracle plans via `netcoredbg.runtime_smoke.v2`,
  including baseline setup, case transitions, before/after probes, diffs,
  cleanup aggregation, compact result envelopes, and schema dispatch through
  the existing `run_runtime_smoke` tool.
- V2 probes, actions, and templates for UI property/text/grid checks, debug
  evaluation, tracepoints, output assertions, JSONPath file assertions, process
  metrics, key-sequence actions, and matrix-generated A/B cases.
- Release-gate coverage for the v2 state oracle, including critical tests,
  WPF/Avalonia fixture scenarios, manual smoke inventory entries, and runnable
  JSON examples under `docs/examples/`.

### Changed
- Runtime smoke adapters now preserve actionable `BLOCKED` evidence for
  selector misses, bridge availability failures, process-registry failures, and
  unavailable metric fields instead of reporting false PASS results.
- Full-project static analysis and type checking are now ratcheted clean for
  the release branch.

### Fixed
- UI adapter bridge exceptions from `ui.get_property`, `ui.find_element`, and
  `ui.set_focus` are converted into structured runtime-smoke `BLOCKED` results
  instead of crashing the smoke runner.

## [0.13.1] - 2026-05-07

### Fixed
- Hardened the WPF one-call runtime smoke workflow: UI automation connects
  eagerly after launch, primary-window selection uses a stable deterministic
  tie-breaker, DataGrid cell evidence merges structured `GridPattern` cells with
  descendant fallback text without scanning expensive fallback trees when row
  coverage is already complete, short grid retry timeouts no longer sleep longer
  than requested, and the FlaUI bridge client restarts after timed-out or
  mismatched JSON-RPC responses so stale bridge output cannot poison the next UI
  operation.
- Windows runtime-smoke cleanup and tests now fail fast on WinAPI attribute
  failures instead of silently ignoring `SetFileAttributesW` errors.

### Changed
- Release documentation now treats the WPF one-call workflow and Avalonia UI
  fixture compatibility as explicit customer-mode playbook gates before release.

## [0.13.0] - 2026-05-07

### Added
- Runtime smoke orchestration tools for release and manual verification:
  `debug_hygiene_preflight`, instrumentation groups, output checkpoints,
  freshness verification, and `run_runtime_smoke` provide a bounded scenario
  runner with cleanup and compact evidence.
- WPF and Avalonia smoke fixtures under `tests/fixtures/` so GUI evidence is
  covered beyond the baseline WinForms fixture. The manual smoke scenario list
  now includes WPF Shift/DataGrid evidence and Avalonia UI fixture compatibility
  when those fixture binaries are built.

### Changed
- Manual smoke guidance now treats `SmokeTestApp`, `WpfSmokeApp`, and
  `AvaloniaSmokeApp` as the expected fixture set for full GUI coverage.
- Publish workflow artifact downloads use `skip-decompress: true` with an
  explicit single-archive unzip step, avoiding the deprecated `Buffer()`
  dependency path inside the download action while keeping PyPI uploads strict.
- TestPyPI release rehearsal uses `skip-existing: true` so repeated rehearsals
  can verify trusted publishing without hiding real PyPI duplicate upload
  errors.

## [0.12.0] - 2026-05-06

### Added
- Launch environment profiles for v0.12.0: `start_debug` can load a
  project-local `.netcoredbg-mcp.launch.json`, merge inherited process
  variables, preserve explicit `null` environment values for DAP, and return
  only redacted launch-environment metadata.
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
  cross-project issue #96 (blocker for sampleapp Phase 2).
- `remove_breakpoint(line=requested)` previously returned `{removed: false}`
  after DAP adjusted the breakpoint line. `Breakpoint.line` now keeps the
  user-requested identity and removal works as expected. When called with
  the DAP-adjusted line, the response includes a `hint` explaining the
  adjustment.
- DAP launch requests now inherit the MCP server process environment by
  default, preserving Windows GUI variables such as `WINDIR`, `SystemRoot`,
  `PATH`, `TEMP`, and `TMP` while keeping explicit caller env values as the
  override layer. Launch env debug logs report only the variable count and do
  not expose variable names or values.

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

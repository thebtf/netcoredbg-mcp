# Feature: UI Input Primitives Expansion

**Slug:** ui-input-primitives-expansion
**Created:** 2026-04-17
**Status:** Draft
**Author:** AI Agent (autopilot, reviewed by user)

> **Provenance:** Specified by Claude Opus 4.7 on 2026-04-17.
> Evidence from: engram issues #79 / #80 / #81 (novascript → netcoredbg-mcp,
> all HIGH/MEDIUM priority), existing bridge code (`bridge/Commands/InputCommands.cs`,
> `ClickCommands.cs`), existing Python drag implementation
> (`src/netcoredbg_mcp/ui/automation.py:_send_drag`, `src/netcoredbg_mcp/tools/ui.py:ui_drag`),
> FlaUI 5.0.0 `Keyboard.Press/Release` + `Mouse` APIs.
> Confidence: VERIFIED.

## Overview

Add three missing UI input primitives to the FlaUI bridge + MCP tool surface so that
the NovaScript WPF agent can reproduce automatically the three bug classes it is
currently blocked on: drag-drop reordering, OS theme-change crashes, and
Ctrl+click multi-selection edge cases.

## Context

NovaScript (a WPF application tracked by engram) is executing an autonomous UX
regression test loop against its own codebase. Three bug reports are CURRENTLY
BLOCKED because the existing UI automation tools in netcoredbg-mcp lack the
right primitives:

- **novascript #56 (CRITICAL):** Row drag crashes the app. Cannot be reproduced
  automatically — `ui_click` is a sub-pixel mouse down+up that never crosses
  WPF's `SystemParameters.MinimumHorizontalDragDistance` /
  `MinimumVerticalDragDistance` thresholds, so `DragDrop.DoDragDrop` is never
  triggered.

- **novascript #75 (CRITICAL):** App silently crashes when switching Windows
  theme from dark to light. Cannot be reproduced automatically — no tool can
  trigger an OS-level personalisation change from inside the debug session,
  so `SystemEvents.UserPreferenceChanged` and
  `SystemParameters.StaticPropertyChanged` never fire in the debuggee.

- **novascript #67:** Batch character replace is intermittent. Cannot be
  reproduced automatically because Ctrl+click multi-selection requires the
  Ctrl modifier to be held across multiple discrete mouse clicks. The existing
  `ui_send_keys_batch` presses and releases modifiers within a single batch;
  no primitive keeps a modifier held across subsequent `ui_click` /
  `ui_send_keys_batch` calls.

### What exists today

Input-side surface in the repo (audited 2026-04-17):

| Capability | Where | Coverage |
|------------|-------|----------|
| `ui_click(automation_id \| x,y)` | `bridge/Commands/ClickCommands.cs:Click`, `tools/ui.py` | Single point click, no drag threshold handling |
| `ui_click_at(x,y)` | `tools/ui.py` | Absolute-coord click |
| `ui_right_click` / `ui_double_click` | `bridge/Commands/ClickCommands.cs`, `tools/ui.py` | Same pattern |
| `ui_send_keys` / `ui_send_keys_batch` / `ui_send_keys_focused` | `bridge/Commands/InputCommands.cs`, `tools/ui.py` | Modifiers are pressed+released within a single batch via `Keyboard.Press(SHIFT)` / `Keyboard.Release(SHIFT)`. No persistent state between batches. |
| `ui_drag(from/to automation_id)` (Python-only, no bridge command) | `src/netcoredbg_mcp/tools/ui.py:1472`, `src/netcoredbg_mcp/ui/automation.py:_send_drag` | `_send_drag` performs `SetCursorPos → LEFTDOWN → 10-step smooth move → LEFTUP` via Win32 `mouse_event`. Does NOT route through FlaUI bridge. No `speed_ms` parameter. Not explicitly tested against WPF drag threshold. |
| Win32 `SendInput` / `mouse_event` | `src/netcoredbg_mcp/ui/automation.py:_send_click`, `_send_drag` | Available Python-side |
| FlaUI `Mouse`, `Keyboard` API | `FlaUI.Core.Input` in `bridge/Commands/*.cs` | `Mouse.Click(Point)` used in bridge; `Keyboard.Press/Release(VirtualKeyShort.CONTROL)` used but only within the scope of a single batch; neither supports long-lived state |

Coverage gaps (each gap = one engram issue):

- #79 — no bridge-side drag command, no composable `mouse_down/move/up` primitives, no threshold guarantees, no configurable speed.
- #80 — no primitive that triggers `WM_SETTINGCHANGE` / `BroadcastSystemMessage` / theme registry flip from the debug session.
- #81 — no primitive that keeps a modifier held across subsequent `ui_*` tool calls. Session-end cleanup for held modifiers is also absent (if added).

## Functional Requirements

### FR-1: Bridge command `drag`

A new JSON-RPC method `drag` on the FlaUI bridge. Inputs: `{x1, y1, x2, y2, speed_ms? = 200, hold_modifiers? = []}`. Sends `mouse_down` at `(x1,y1)`, smoothly moves through intermediate waypoints over `speed_ms` milliseconds, sends `mouse_up` at `(x2,y2)`. The trajectory MUST cross the WPF drag threshold within the first ~5 waypoints (i.e., the cursor moves at least `max(MinimumHorizontalDragDistance, MinimumVerticalDragDistance) + 1 px` — default is 4 px in each axis). `hold_modifiers`, if present, lists `["ctrl"|"shift"|"alt"]` to press before `mouse_down` and release after `mouse_up`.

### FR-2: MCP tool `ui_drag` enhancement

`ui_drag` in `src/netcoredbg_mcp/tools/ui.py` gains a `speed_ms: int = 200` parameter and a `hold_modifiers: list[str] | None = None` parameter. When the FlaUI backend is in use, `ui_drag` MUST route through the new bridge `drag` command (no more silent fallback to the Python `_send_drag`). When the pywinauto backend is in use, `ui_drag` falls back to the existing `_send_drag` but ALSO respects `speed_ms` and `hold_modifiers` (holds modifiers via `SendInput` before the gesture, releases after).

### FR-3: Bridge command `send_system_event`

A new JSON-RPC method `send_system_event` on the FlaUI bridge. Scope for v1 is limited to one event: `theme_change`. Inputs: `{event: "theme_change", mode: "light"|"dark"|"toggle"}`. Procedure: writes `AppsUseLightTheme` and `SystemUsesLightTheme` under `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize` (0=dark, 1=light), then calls `SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 100ms, ...)` so that `SystemEvents.UserPreferenceChanged` and `SystemParameters.StaticPropertyChanged` fire in the debugged WPF process. Returns the old mode and the new mode so callers can compose a toggle.

### FR-4: MCP tool `ui_send_system_event`

A new MCP tool `ui_send_system_event(event: str, mode: str = "toggle")` in `src/netcoredbg_mcp/tools/ui.py`. FlaUI backend routes to bridge `send_system_event`; pywinauto backend raises a structured unsupported response (`{"ok": False, "unsupported": True, "reason": "..."}`), following the same pattern used for `ui_switch_window` on pywinauto.

### FR-5: Bridge commands `hold_modifiers` and `release_modifiers`

Two new JSON-RPC methods on the FlaUI bridge:

- `hold_modifiers`: `{modifiers: ["ctrl"|"shift"|"alt"|"win"]}`. Presses each listed modifier via `Keyboard.Press(VirtualKeyShort.X)`, records each pressed modifier in a session-scoped hash set stored on `JsonRpcHandler` (e.g., `HeldModifiers`). Idempotent: pressing an already-held modifier is a no-op, not a double-press.
- `release_modifiers`: `{modifiers: [...] | "all"}`. Releases each listed modifier and removes it from the held set. `"all"` releases every currently held modifier.

The held-modifier set is consulted by `ui_click`, `ui_right_click`, `ui_double_click`, `ui_drag`, `ui_send_keys`, `ui_send_keys_batch`, `ui_send_keys_focused` — i.e., every input primitive that produces a click or a keystroke. These primitives MUST NOT double-press a modifier that is already held; they use whichever is currently held plus any per-call modifier from the key syntax (`^`, `%`, `+`) as a superset. The drag-threshold guarantees in this spec apply to `ui_drag` only; plain click tools keep their existing click semantics and are not upgraded into threshold-crossing drags.

### FR-6: MCP tools `ui_hold_modifiers` and `ui_release_modifiers`

Two new MCP tools mirroring FR-5. Inputs validated to the four-modifier whitelist (`ctrl`, `shift`, `alt`, `win`). FlaUI backend forwards; pywinauto backend raises structured unsupported response per the pattern in FR-4.

### FR-7: Auto-release on disconnect

When the FlaUI bridge disconnects (either via explicit `disconnect` command or because the underlying MCP session ends and the bridge subprocess exits), every modifier in the held set MUST be released via `Keyboard.Release` before process exit. This prevents a crashed session from leaving Ctrl/Shift/Alt stuck across the whole Windows desktop — any other application would otherwise inherit the sticky modifier state.

### FR-8: Inspection tool `ui_get_held_modifiers`

A read-only MCP tool `ui_get_held_modifiers()` returning `{modifiers: [...]}`. Enables callers (and tests) to assert the hold state without relying on timing or side effects. Bridge command `get_held_modifiers`.

### FR-9: Documentation update

`src/netcoredbg_mcp/prompts.py` GUI debugging section adds a row for each of the six new tools (`ui_drag` — updated; `ui_send_system_event`; `ui_hold_modifiers`; `ui_release_modifiers`; `ui_get_held_modifiers`) and a worked example showing the Ctrl+click multi-select workflow end-to-end.

## Non-Functional Requirements

### NFR-1: Drag threshold reliability

`ui_drag` MUST trigger `DragDrop.DoDragDrop` in a WPF app that calls `DragDrop.DoDragDrop` from a `PreviewMouseMove` handler at least 95% of invocations under the default `speed_ms = 200`. Tested via a dedicated SmokeTestApp WPF drag scenario (see FR-11 in the testing section below). "95%" is measured over 20 consecutive invocations in the smoke test run — 19/20 minimum pass.

### NFR-2: System event propagation latency

Between a `send_system_event` bridge call returning success and `SystemParameters.StaticPropertyChanged` firing in a debugged WPF process running on the same host, the observed latency MUST be under 500 ms at p95. Measured in the smoke test by setting a tracepoint on a `StaticPropertyChanged` handler.

### NFR-3: Held modifier auto-release reliability

On the normal .NET shutdown path (including stdin EOF, Ctrl+C handled as process shutdown, window close, or `Main` returning), the `finally` block plus `AppDomain.ProcessExit` handler MUST release every held modifier. Verified by a dedicated smoke scenario: hold Ctrl, terminate bridge on a graceful shutdown path, assert no subsequent typing in a Notepad-like external app produces Ctrl+character behaviour. Force-kill paths such as `taskkill /F` are explicitly outside the guarantee.

### NFR-4: Backwards compatibility

Existing `ui_drag` call sites MUST continue to work. Adding `speed_ms` / `hold_modifiers` as optional keyword arguments with sensible defaults preserves the existing contract. Existing `ui_click`, `ui_send_keys`, etc., semantics are unchanged when no modifier is held — the held-modifier machinery is a no-op when the held set is empty (verified by existing smoke suite continuing to pass 112/112 unchanged).

### NFR-5: Windows version support

All three primitives MUST work on Windows 10 (21H2 and later) and Windows 11 (22H2 and later), both Home and Pro. Registry path, UIA provider behaviour, and `WM_SETTINGCHANGE` broadcasting semantics are identical across these versions — no version-specific code paths.

### NFR-6: Security boundary

`send_system_event` writes under `HKCU` only. Never touches `HKLM`. Registry operation is limited to the four specific values under the `Themes\Personalize` key — no arbitrary registry writes exposed via MCP tools. Each call logs the old → new transition to `Program.Log` for auditability.

## User Stories

### US1: Reproduce drag-drop crash in NovaScript (P1)
**As a** NovaScript autonomous test agent, **I want** a single `ui_drag` call that actually triggers `DragDrop.DoDragDrop`, **so that** I can reproduce novascript#56 (Row drag crashes) without manual intervention.

**Acceptance Criteria:**
- [ ] `ui_drag(from_automation_id="CueDataGrid_Row_5", to_automation_id="CueDataGrid_Row_9")` on a WPF DataGrid with a `PreviewMouseMove` drag-drop initiator triggers `DragDrop.DoDragDrop` within 500 ms.
- [ ] `speed_ms=50` (fast) and `speed_ms=500` (slow) both trigger the drag, with observed drag duration within ±15% of the requested value.
- [ ] `hold_modifiers=["ctrl"]` holds Ctrl for the whole gesture (triggers "copy" drag semantics in apps that differentiate).
- [ ] Failing to cross the drag threshold (e.g., `speed_ms=0` which would skip intermediate points) returns a structured error rather than a silent no-op.

### US2: Reproduce theme-change crash in NovaScript (P1)
**As a** NovaScript autonomous test agent, **I want** a single `ui_send_system_event(event="theme_change", mode="toggle")` call, **so that** I can trigger the WPF theme-change event pipeline in the debuggee and reproduce novascript#75.

**Acceptance Criteria:**
- [ ] After calling `ui_send_system_event(event="theme_change", mode="light")` on a host currently in dark mode, the registry value `AppsUseLightTheme` under `HKCU\...\Themes\Personalize` flips to `1`.
- [ ] A tracepoint on `SystemParameters.StaticPropertyChanged` in the debuggee fires within 500 ms of the MCP call returning.
- [ ] `mode="toggle"` flips whichever mode is currently active, returning both `{from: "dark", to: "light"}` in the response.
- [ ] Calling the tool back-to-back 3 times alternates the theme each time with no stuck state.

### US3: Reproduce Ctrl+click multi-select crash in NovaScript (P2)
**As a** NovaScript autonomous test agent, **I want** to hold Ctrl across multiple `ui_click` calls, **so that** I can reproduce novascript#67 (batch character replace intermittency) using the WPF DataGrid's native Ctrl+click multi-select path.

**Acceptance Criteria:**
- [ ] The sequence `ui_hold_modifiers(["ctrl"])` → `ui_click(automation_id="Row_3")` → `ui_click(automation_id="Row_5")` → `ui_click(automation_id="Row_7")` → `ui_release_modifiers(["ctrl"])` leaves rows 3, 5, 7 selected on a `SelectionMode=Extended` DataGrid.
- [ ] `ui_get_held_modifiers()` after the first `ui_hold_modifiers` returns `{modifiers: ["ctrl"]}`; after the release, returns `{modifiers: []}`.
- [ ] Nested holds compose: `hold_modifiers(["ctrl"])` then `hold_modifiers(["shift"])` leaves both held; a subsequent `ui_click` applies Ctrl+Shift+Click semantics.
- [ ] If the bridge process is killed while a modifier is held, the modifier is released before process exit (no stuck Ctrl across the desktop after an abnormal exit).

### US4: Smoke coverage for all three primitives (P2)
**As a** netcoredbg-mcp maintainer, **I want** end-to-end smoke coverage for each primitive, **so that** regressions are caught without relying on the downstream NovaScript test loop.

**Acceptance Criteria:**
- [ ] The SmokeTestApp fixture (`tests/fixtures/SmokeTestApp`) gains a drag-reorder list that starts `DoDragDrop` only after `MouseMove` crosses `SystemInformation.DragSize`, plus a theme-change tracepoint and a multi-select control. All three fit in the existing `gui` scenario (no new scenario required).
- [ ] `tests/smoke_test_manual.py` gains one new scenario each: `Drag Primitive`, `System Event`, `Persistent Modifier Hold`. The drag smoke reads back the live list order after each drag when UIA text extraction is available, and otherwise falls back to checking the bridge returned non-error responses with duration close to the requested `speed_ms`.
- [ ] Total smoke count rises from 112 → ≥130; zero regressions in the existing 112.

### US5: Unit coverage for Python routing + backend contracts (P2)
**As a** netcoredbg-mcp maintainer, **I want** unit tests that verify the Python layer routes correctly for all three primitives, **so that** a pywinauto-only environment still returns clean unsupported responses and FlaUI routing exercises the new bridge commands.

**Acceptance Criteria:**
- [ ] `tests/test_input_primitives.py` covers: `FlaUIBackend.drag` forwards params; `FlaUIBackend.send_system_event` forwards params; `FlaUIBackend.hold_modifiers` / `release_modifiers` / `get_held_modifiers`; `PywinautoBackend` returns `{unsupported: True}` for the three FlaUI-only primitives (`send_system_event`, `hold_modifiers`, `release_modifiers`).
- [ ] Existing 565 unit tests + 13 multi-window tests continue to pass unchanged.

## Edge Cases

- Drag `speed_ms` < 20 ms: fewer than 5 intermediate waypoints. MUST emit a structured error `"speed_ms below drag-threshold safety floor (minimum 20)"`. Do not silently degrade to a zero-step drag.
- Drag source and target coordinates identical: MUST emit a structured error `"from and to coordinates are identical (0 px distance)"`. Cannot fall through — the WPF drag threshold would not be crossed.
- `send_system_event` on a machine where the `Themes\Personalize` key does not exist: MUST create the key, write both values, and broadcast. Emit a log note that the key was created.
- `hold_modifiers` with an unknown modifier name: MUST reject the whole call with a structured error listing the accepted four values. Partial hold ("pressed ctrl then failed on `foo`") would leave inconsistent state.
- `release_modifiers` listing a modifier that is not held: MUST be a no-op, not an error (release-of-unheld is idempotent).
- Multiple bridge subprocesses sharing a Windows host: each bridge has its own `HeldModifiers` set. A modifier held by bridge A does NOT affect bridge B. The UIA / SendInput layer is global to the desktop, so a modifier pressed by bridge A IS globally pressed — this is a known constraint, and users are expected to run one bridge at a time per target process.
- Bridge killed with `taskkill /F` (no `finally` / `ProcessExit` path): on a truly unclean exit, a modifier can be left stuck. `AppDomain.ProcessExit` runs on the normal .NET shutdown path, including stdin EOF leading to `Main` returning, but does NOT run on force-kill. Users must re-run `hold_modifiers` only after they have manually recovered any stuck modifier state. The smoke test covers graceful exit only.

## Out of Scope

- Non-theme system events. `ui_send_system_event` v1 handles only `event="theme_change"`. A future spec may add `display_scale_change`, `locale_change`, `power_setting_change` — they are NOT in this spec.
- True click-hold (long press) as a separate primitive. If the novascript agent needs it, it MUST use `ui_hold_modifiers` + `ui_click` + `ui_release_modifiers` with whatever modifier simulates the semantics they want; a dedicated "press and hold mouse button across calls" primitive is deferred.
- Extended mouse-button coverage (middle-click drag, X1/X2 mouse buttons). v1 covers left-button drag only.
- Cross-machine or remote modifier state. `hold_modifiers` operates on the host running the bridge. If the bridge runs remotely (not a supported config today), modifier state would apply to the remote host's desktop, not the caller's.

## Dependencies

- FlaUI 5.0.0 (already present): `FlaUI.Core.Input.Mouse`, `FlaUI.Core.Input.Keyboard`, `FlaUI.Core.WindowsAPI.VirtualKeyShort`. No new package required.
- Windows-only. `bridge/FlaUIBridge.csproj` already targets `net8.0-windows`.
- Existing `~/.netcoredbg-mcp/bridge/FlaUIBridge.exe` distribution path.
- Compatible with existing `ui_switch_window` multi-window support shipped in v0.9.0 — the new primitives operate on whichever window is currently the bridge's active target.

## Success Criteria

- [ ] All three blocker engram issues (#79, #80, #81) transition from `acknowledged` → `resolved`, with a test-pass confirmation comment from novascript's side.
- [ ] Smoke suite expands by ≥18 checks (6 per primitive minimum) with zero regressions on existing 112.
- [ ] Unit suite expands by ≥12 tests (4 per primitive minimum) with zero regressions on existing 578.
- [ ] PR #48 (or successor) merged to `main` with `/pr:review` completed and all findings resolved.
- [ ] Release v0.10.0 tagged with structured release notes documenting the three new tool families.
- [ ] Bridge rebuilt and redeployed to `~/.netcoredbg-mcp/bridge/` + `D:/Bin/FlaUIBridge.exe`.
- [ ] `src/netcoredbg_mcp/prompts.py` GUI debugging reference table updated.

## Open Questions

No open questions — `/nvmd-clarify` may still surface clarifications around sub-details
(e.g., exact waypoint count, which modifier VK codes for "win", whether
`SendMessageTimeout` or `PostMessage` is preferred for the broadcast). Those are
implementation-level decisions, not spec-blocking.

## Provenance Footer

Engram issues consulted (as of 2026-04-17 acknowledgement timestamp):
- #79 HIGH `Need drag-drop UI primitive (ui_drag or split mouse_down/move/up)`
- #80 HIGH `Need primitive to trigger Windows system events (theme change) from UI automation`
- #81 MEDIUM `Need persistent modifier-hold primitive (Ctrl-click multi-select, Shift-drag)`

Source project `d219e203` (novascript). All three are feature requests, all three
target `netcoredbg-mcp`. All three were acknowledged via the engram REST API prior
to this spec being written.

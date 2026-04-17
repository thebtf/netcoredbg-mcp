# Tasks: UI Input Primitives Expansion

**Spec:** `spec.md`

---

## Phase 1: Bridge Commands (C#)

- [x] T01: Bridge command `drag` — smooth gesture crossing WPF threshold
  - Files: `bridge/Commands/ClickCommands.cs` (new `Drag` method + registration), `bridge/JsonRpcHandler.cs`
  - AC: Accepts `{x1, y1, x2, y2, speed_ms? = 200, hold_modifiers? = []}`. Calls `Mouse.MoveTo(x1, y1)` → `Mouse.Down(MouseButton.Left)` → loops `Mouse.MoveTo(interp, interp)` over ≥10 waypoints with total duration ≈ speed_ms → `Mouse.Up(MouseButton.Left)`. Holds listed modifiers via `Keyboard.Press(VirtualKeyShort.X)` before down, releases after up. Rejects speed_ms < 20 and identical from/to coordinates with structured error. Returns `{dragged: true, x1, y1, x2, y2, steps, duration_ms}`.

- [x] T02: Bridge command `send_system_event` — theme change via registry + broadcast
  - Files: `bridge/Commands/SystemEventCommands.cs` (new), `bridge/JsonRpcHandler.cs`
  - AC: Accepts `{event: "theme_change", mode: "light" | "dark" | "toggle"}`. Only `theme_change` supported in v1 (others → structured error listing supported events). Reads current `AppsUseLightTheme` from `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize` (creates key if missing). For `toggle`: flips current. Writes both `AppsUseLightTheme` and `SystemUsesLightTheme` to target mode (0 dark, 1 light). Broadcasts via `SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 100, ...)`. Returns `{event: "theme_change", from: "dark" | "light", to: "dark" | "light"}`. Logs old→new transition.

- [x] T03: Bridge commands `hold_modifiers` / `release_modifiers` / `get_held_modifiers` — persistent modifier state
  - Files: `bridge/Commands/ModifierCommands.cs` (new), `bridge/JsonRpcHandler.cs` (new `HeldModifiers` HashSet<VirtualKeyShort> static)
  - AC: `hold_modifiers({modifiers: ["ctrl"|"shift"|"alt"|"win"]})` — validates whitelist, idempotent (no double-press), adds to `JsonRpcHandler.HeldModifiers` hash set, calls `Keyboard.Press(vk)` for newly added only. `release_modifiers({modifiers: [...] | "all"})` — calls `Keyboard.Release(vk)` for held modifiers only, removes from set. `get_held_modifiers()` — returns `{modifiers: ["ctrl", ...]}` sorted. Returns structured errors for unknown modifier names. Registered in JsonRpcHandler.

- [x] T04: Auto-release held modifiers on bridge shutdown
  - Files: `bridge/Program.cs`
  - AC: Register `AppDomain.CurrentDomain.ProcessExit += (_, _) => { foreach (vk in HeldModifiers) Keyboard.Release(vk); }` at program start. Also add `try/finally` in Main so graceful exit via stdin EOF releases. Covered by smoke test NFR-3.

## Phase 2: Python Backend + MCP Tools

- [x] T05: FlaUIBackend Python methods for new bridge commands
  - Files: `src/netcoredbg_mcp/ui/flaui_client.py`
  - AC: New async methods on `FlaUIBackend`: `drag(x1, y1, x2, y2, speed_ms=200, hold_modifiers=None)` forwards to bridge `drag`; `send_system_event(event, mode="toggle")` forwards to `send_system_event`; `hold_modifiers(modifiers)` / `release_modifiers(modifiers_or_all)` / `get_held_modifiers()` forward to respective commands. Each raises `RuntimeError` on non-dict bridge responses (same pattern as `switch_window`).

- [x] T06: UIBackend protocol + PywinautoBackend stubs
  - Files: `src/netcoredbg_mcp/ui/backend.py`, `src/netcoredbg_mcp/ui/pywinauto_backend.py`
  - AC: Protocol adds `drag`, `send_system_event`, `hold_modifiers`, `release_modifiers`, `get_held_modifiers`. `PywinautoBackend` implementations: `drag` falls back to existing `_send_drag` (Win32) with new `speed_ms`/`hold_modifiers` support via `SendInput`; `send_system_event` / `hold_modifiers` / `release_modifiers` return `{switched: False, unsupported: True, reason: "FlaUI bridge required"}` dict (same pattern as `switch_window`). `get_held_modifiers` returns `{modifiers: []}` on pywinauto (no persistent state to report).

- [x] T07: MCP tool `ui_drag` enhancement
  - Files: `src/netcoredbg_mcp/tools/ui.py`
  - AC: Existing `ui_drag` signature extended with `speed_ms: int = 200` and `hold_modifiers: list[str] | None = None`. FlaUI backend routes through new `backend.drag(...)` method (no more `_send_drag` fallthrough). Pywinauto backend uses updated `backend.drag(...)` (which internally calls `_send_drag` with new params). Input validation: `speed_ms < 20` → error; identical from/to → error. Structured error responses preserved.

- [x] T08: MCP tools for system event and modifier hold
  - Files: `src/netcoredbg_mcp/tools/ui.py`
  - AC: Four new MCP tools registered: `ui_send_system_event(ctx, event, mode="toggle")`, `ui_hold_modifiers(ctx, modifiers)`, `ui_release_modifiers(ctx, modifiers)`, `ui_get_held_modifiers()`. Each validates input, routes to backend method, surfaces `{unsupported: True}` backend responses as `build_error_response`. Modifier validation whitelists `{"ctrl", "shift", "alt", "win"}` case-insensitively before passing to backend.

## Phase 3: Tests

- [x] T09: Unit tests — FlaUI routing + Pywinauto unsupported contracts
  - Files: `tests/test_input_primitives.py` (new)
  - AC: ≥12 tests covering: `FlaUIBackend.drag` forwards params; `send_system_event` forwards; `hold_modifiers`/`release_modifiers`/`get_held_modifiers` forward; `PywinautoBackend.send_system_event` returns unsupported; `PywinautoBackend.hold_modifiers` returns unsupported; pywinauto `drag` with `speed_ms`/`hold_modifiers` forwards to `_send_drag` with correct args; input validation (unknown modifier, speed_ms < 20, identical coords) raises. All 565 + 13 existing tests continue to pass unchanged.

- [x] T10: Smoke scenario — Drag Primitive
  - Files: `tests/fixtures/SmokeTestApp/Program.cs`, `tests/smoke_test_manual.py`
  - AC: SmokeTestApp gains a ListBox (or DataGridView with AllowDrop) populated with 5 items that supports native WinForms drag-drop reorder (`DoDragDrop` handler). Smoke scenario: ensure ListBox visible, call `ui_drag(from_x, from_y, to_x, to_y, speed_ms=200)`, verify item order changed. Also test `speed_ms=50` (fast) and `speed_ms=500` (slow) both succeed. Verify `speed_ms=10` returns error.

- [x] T11: Smoke scenario — System Event
  - Files: `tests/smoke_test_manual.py`
  - AC: Read current registry `AppsUseLightTheme`, call `ui_send_system_event(event="theme_change", mode="toggle")`, verify: (a) registry value flipped, (b) response contains `{from: <old>, to: <new>}`, (c) calling again flips back. Optional tracepoint scenario: if WinForms app has `SystemEvents.UserPreferenceChanged` handler, verify handler fired within 500 ms.

- [x] T12: Smoke scenario — Persistent Modifier Hold
  - Files: `tests/fixtures/SmokeTestApp/Program.cs`, `tests/smoke_test_manual.py`
  - AC: SmokeTestApp gains a `ListBox` or `DataGridView` with `SelectionMode = MultiExtended`. Smoke scenario: `ui_hold_modifiers(["ctrl"])` → 3× `ui_click` on different items → `ui_release_modifiers(["ctrl"])` → verify 3 items selected. Verify `ui_get_held_modifiers` returns `["ctrl"]` mid-sequence and `[]` after release. Verify nested holds (ctrl + shift) compose via `ui_get_held_modifiers`.

## Phase 4: Integration + Release

- [ ] T13: Prompts + docs update
  - Files: `src/netcoredbg_mcp/prompts.py`, existing readme/docs if any
  - AC: GUI debugging action table adds 5 new rows (ui_drag updated, ui_send_system_event, ui_hold_modifiers, ui_release_modifiers, ui_get_held_modifiers). Worked example block for Ctrl+click multi-select workflow end-to-end added.

- [ ] T14: Bridge rebuild + deploy to home dir
  - Files: build outputs under `bridge/bin/Release/publish-sc/`, deploy to `~/.netcoredbg-mcp/bridge/` + `D:/Bin/FlaUIBridge.exe`
  - AC: `dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true` produces new FlaUIBridge.exe ~154MB. Deployed to both paths. Verified via `ls -la` size + mtime matches.

- [ ] T15: Version bump 0.9.0 → 0.10.0
  - Files: `pyproject.toml`, `src/netcoredbg_mcp/__init__.py`
  - AC: MINOR bump (three new tool families + one enhanced tool = significant surface area). Both files updated to `0.10.0`.

- [ ] T16: PR + review + merge + release + close issues
  - Files: N/A (git + gh + engram REST API)
  - AC: `gh pr create` with structured body listing engram #79/#80/#81 and acceptance criteria. `/mcp__pr__review` kicked off via pr-reviewer agent. All findings resolved. Merge to main. Tag `v0.10.0` via `gh release create` with structured release notes. Update engram issues #79/#80/#81 to `resolved` with verification comment.

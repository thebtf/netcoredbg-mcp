# FlaUI pattern expansion — v0.11.1 (Tier 1) + v0.11.2 (Tier 2)

## Context

netcoredbg-mcp currently exposes 27 `ui_*` MCP tools backed by the FlaUI bridge. These cover input (click, drag, send_keys, modifier hold), discovery (get_window_tree, find_element), and observation (screenshot, read_text, wait_for). Several UIA patterns that are available in FlaUI are NOT yet exposed — window lifecycle, expand/collapse, range value, clipboard, virtualized items, grid iteration, event subscription.

This feature fills the gap proactively (not reactive to a single consumer request) because sampleapp is a feature-locked legacy bugfix branch with no roadmap to drive individual requests. We bundle pattern-identical cheap wrappers into a single release to amortize the per-tool overhead (fixture + smoke + unit + pywinauto fallback + docs).

## Scope

Split across two releases:

### v0.11.1 — Tier 1 (this spec)

Tools that are pattern-identical to v0.10.0, synchronous, predictable:

| Tool | FlaUI pattern | Purpose |
|---|---|---|
| `ui_close_window` | `WindowPattern.Close` | Close top-level window |
| `ui_maximize_window` | `WindowPattern.SetWindowVisualState(Maximized)` | Maximize |
| `ui_minimize_window` | `WindowPattern.SetWindowVisualState(Minimized)` | Minimize |
| `ui_restore_window` | `WindowPattern.SetWindowVisualState(Normal)` | Restore to normal |
| `ui_move_window` | `TransformPattern.Move(x, y)` | Move window (if CanMove) |
| `ui_resize_window` | `TransformPattern.Resize(w, h)` | Resize window (if CanResize) |
| `ui_expand` | `ExpandCollapsePattern.Expand` | Expand TreeView node, ComboBox dropdown |
| `ui_collapse` | `ExpandCollapsePattern.Collapse` | Collapse node/dropdown |
| `ui_set_value` | `RangeValuePattern.SetValue` | Set slider/spinner/progressbar value |
| `ui_clipboard_read` | Win32 `System.Windows.Clipboard.GetText` | Read clipboard text (STA thread) |
| `ui_clipboard_write` | Win32 `System.Windows.Clipboard.SetText` | Write clipboard text (STA thread) |
| `ui_realize_virtualized_item` | `ItemContainerPattern.FindItemByProperty` + `VirtualizedItemPattern.Realize` | Realize a virtualized list/grid item so it enters the visual tree |

**12 new tools.** All accept `automation_id` or window/element targeting consistent with existing tools.

### v0.11.2 — Tier 2 (separate spec)

Design-heavy work deferred to a follow-up spec because it requires clarification:

- `TablePattern`/`GridPattern` — DataGrid cell iteration. Open question: snapshot vs streaming semantics for large grids.
- UIA event subscription — `RegisterStructureChangedEvent`, `RegisterPropertyChangedEvent`. Open question: callback→poll bridge model over JSON-RPC stdio.

Not covered by this spec. See `.agent/specs/flaui-patterns-events/spec.md` (to be written for v0.11.2).

## User stories

**US-1: Autonomous test resets window state.** A test must start with a clean window layout. Test calls `ui_maximize_window` then `ui_restore_window` to reset before drag scenarios that depend on visible viewport geometry.

**US-2: Large-DataGrid regression test reaches row 150.** Novascript's CueDataGrid virtualizes beyond ~30 visible rows. Test calls `ui_realize_virtualized_item(container="CueDataGrid", property="AutomationId", value="CueDataGrid_Row_150")` which returns `{found: true, element_id, bounding_rect}` and subsequent `ui_click(automation_id="CueDataGrid_Row_150")` succeeds.

**US-3: TreeView integration test.** Test expands `Characters` node, expands nested `Main cast` node, verifies count of leaf items. Calls `ui_expand(automation_id="Characters")` then `ui_expand(automation_id="Main cast")` — both synchronous, expanded state persists for subsequent `ui_find_element` queries.

**US-4: Slider regression test.** A WPF Slider controls scene duration. Test sets value to 15.0 via `ui_set_value(automation_id="DurationSlider", value=15.0)` instead of multiple arrow-key send_keys round-trips.

**US-5: Clipboard-driven scenario.** Test verifies Ctrl+C on a text selection puts expected text on clipboard: after `ui_send_keys_batch(["^c"])` calls `ui_clipboard_read()` → returns selected text. Conversely seeds test data with `ui_clipboard_write("expected text")` then `Ctrl+V`.

**US-6: Modal dialog cleanup.** After a test scenario leaves a modal dialog open, teardown calls `ui_close_window(window_title="Dialog")` to close it cleanly without relying on OK-button coordinates.

## Functional requirements

**FR-1.** Every new tool MUST accept the same targeting conventions as existing `ui_*` tools: `automation_id` for element-bound patterns, `window_title` for top-level windows where unambiguous.

**FR-2.** Every new tool MUST return a structured dict with an operation-specific success flag (e.g. `{"closed": true, "window_title": "..."}`, `{"expanded": true, "automation_id": "..."}`) plus the minimum fields needed for a chained call.

**FR-3.** Bridge commands MUST throw `InvalidOperationException` when the target element does not support the requested pattern — silent success on wrong-patterned elements is prohibited. Python layer surfaces this as `RuntimeError` via `build_error_response`.

**FR-4.** `ui_move_window` / `ui_resize_window` MUST check `TransformPattern.CanMove` / `CanResize` before invoking the action. Return `{"moved": false, "reason": "window not movable"}` rather than silent no-op.

**FR-5.** `ui_set_value` MUST validate the value is within `RangeValuePattern.Minimum..Maximum` of the target element and return `{"set": false, "reason": "value X out of range [min..max]"}` on violation.

**FR-6.** `ui_clipboard_read` / `ui_clipboard_write` MUST execute on an STA thread (required by `System.Windows.Clipboard`). Bridge uses `Thread(ApartmentState.STA)` wrapper.

**FR-7.** `ui_realize_virtualized_item` MUST return `{"realized": true, "element_id": "...", "bounding_rect": {...}}` on success, `{"realized": false, "reason": "item not found" | "container does not support ItemContainerPattern"}` on failure. On success, the item's `AutomationId` becomes usable in subsequent `ui_find_element` / `ui_click` calls.

**FR-8.** `PywinautoBackend` fallbacks MUST return `{"unsupported": true, "reason": "FlaUI bridge required for <pattern>"}` for every new tool. No silent no-ops, no errors. Consistent with v0.10.0 fallback pattern.

**FR-9.** New tools MUST appear in `src/netcoredbg_mcp/prompts.py` GUI debugging table with 1-line description and at least one worked example for the most complex (realize_virtualized_item, set_value with range validation).

**FR-10.** Unit tests in `tests/test_pattern_expansion.py` MUST cover: happy path, wrong-pattern element rejection, pywinauto unsupported fallback, input validation (out-of-range value, missing automation_id).

**FR-11.** Smoke scenarios MUST extend `tests/fixtures/SmokeTestApp/Program.cs` with: `TreeView` (for expand/collapse), `Slider` with defined Min/Max (for set_value), virtualized `ListBox` with `VirtualizingStackPanel.IsVirtualizing=true` and `VirtualizationMode=Recycling` carrying 500+ items (for realize_virtualized_item).

**FR-12.** Smoke runner in `tests/smoke_test_manual.py` MUST include 4 new scenarios: `test_window_lifecycle` (max/min/restore/close), `test_expand_collapse_tree`, `test_set_value_slider`, `test_realize_virtualized_item`, `test_clipboard_roundtrip`.

## Non-functional requirements

**NFR-1.** No regression in existing 586 unit tests; no regression in existing smoke scenarios.

**NFR-2.** Bridge build output self-contained (.NET 8, net8.0-windows/win-x64/publish) remains under 160 MB (current 147 MB).

**NFR-3.** New bridge commands MUST have `EnsureForeground(mainWindow)` where appropriate (input-affecting commands only; clipboard/query-only commands do NOT require foreground).

**NFR-4.** `ui_realize_virtualized_item` MUST complete within 2 seconds for a 1000-row virtualized ListBox on typical hardware (Ryzen 7 / 16 GB / Win11). Beyond that — investigate ItemContainerPattern performance.

**NFR-5.** Every new tool MUST be registered in `JsonRpcHandler` command dispatch with exact snake_case method names matching Python client calls.

**NFR-6.** Documentation MUST include a note in `prompts.py` that `ui_realize_virtualized_item` is idempotent (re-realizing an already-realized item is safe).

## Edge cases

- **Closed window post-close:** after `ui_close_window`, subsequent `ui_get_window_tree` should NOT include it. If session's `mainWindow` was the closed window, new `ui_*` calls return `{"error": "no active window"}`.
- **Expand-already-expanded:** `ui_expand` on an already-expanded node returns `{"expanded": true, "was_already": true}`.
- **Clipboard unicode:** `ui_clipboard_write` with emoji / CJK / RTL text round-trips correctly.
- **Virtualized item not in data source:** `ui_realize_virtualized_item(value="nonexistent")` returns `{"realized": false, "reason": "item not found"}` after ItemContainerPattern exhausts its scan.
- **Move/resize constrained window:** `ui_move_window` on a dialog with `CanMove=false` returns the structured `{"moved": false, "reason": "..."}` — does NOT throw.

## Out of scope

- TablePattern/GridPattern (deferred to v0.11.2).
- UIA event subscription (deferred to v0.11.2).
- Clipboard formats other than text (image/file/custom — deferred, not needed by any known consumer).
- Window z-order manipulation (BringToTop / SendToBottom) — not a documented consumer need.
- Touch / pen input — no consumer touches tablets.

## Acceptance criteria

- 12 new MCP tools exposed via `tools/ui.py`.
- All smoke scenarios pass on Windows 11.
- 586 + N new unit tests all pass (N ≥ 20, budget 40).
- Bridge builds clean, deployed to `~/.netcoredbg-mcp/bridge/` + `D:/Bin/FlaUIBridge.exe`.
- Version bumped `0.10.0` → `0.11.1` in `pyproject.toml` + `src/netcoredbg_mcp/__init__.py`.
- PR merged after code review.
- Release `v0.11.1` created with structured release notes.
- CONTINUITY.md updated.

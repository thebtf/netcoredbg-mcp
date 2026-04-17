# Tasks — FlaUI pattern expansion v0.11.1

## Phase 1: Bridge (C#)

- [ ] T01 Create `bridge/Commands/WindowCommands.cs` — `CloseWindow`, `SetWindowVisualState(Maximized|Minimized|Normal)` using `WindowPattern`. Match v0.10.0 structure (Program.Log, EnsureForeground where relevant, JsonObject returns).
- [ ] T02 Create `bridge/Commands/TransformCommands.cs` — `MoveWindow(x, y)`, `ResizeWindow(w, h)` using `TransformPattern`. Check `CanMove`/`CanResize` before invoking; return structured `{moved:false, reason:...}` on unsupported.
- [ ] T03 Extend `bridge/Commands/PatternCommands.cs` — `ExpandElement`, `CollapseElement` (ExpandCollapsePattern), `SetRangeValue(value)` (RangeValuePattern with Min/Max validation).
- [ ] T04 Create `bridge/Commands/ClipboardCommands.cs` — `ReadClipboard`, `WriteClipboard(text)` using STA thread wrapper (`new Thread(...) { SetApartmentState(STA) }`). System.Windows.Clipboard requires STA.
- [ ] T05 Create `bridge/Commands/VirtualizationCommands.cs` — `RealizeVirtualizedItem(container_automationId, property, value)`. Uses `ItemContainerPattern.FindItemByProperty` + `VirtualizedItemPattern.Realize`. Return `{realized, element_id, bounding_rect}`.
- [ ] T06 Register 12 new commands in `bridge/JsonRpcHandler.cs` dispatch. Exact method names: `close_window`, `maximize_window`, `minimize_window`, `restore_window`, `move_window`, `resize_window`, `expand`, `collapse`, `set_value`, `clipboard_read`, `clipboard_write`, `realize_virtualized_item`.

## Phase 2: Python layer

- [ ] T07 `src/netcoredbg_mcp/ui/flaui_client.py` — add 12 async methods on `FlaUIBackend`. Each forwards to bridge, raises `RuntimeError` on non-dict response (matches v0.10.0 contract).
- [ ] T08 `src/netcoredbg_mcp/ui/pywinauto_backend.py` — add 12 fallback methods returning `{"unsupported": True, "reason": "FlaUI bridge required for <pattern>"}` dicts. Matches v0.10.0 parity.
- [ ] T09 `src/netcoredbg_mcp/tools/ui.py` — register 12 new `@mcp.tool` async functions. Input validation: window_title OR automation_id exactly one, value numeric for set_value, text non-None for clipboard_write. Surface `{unsupported:True}` via `build_error_response`.

## Phase 3: Documentation

- [ ] T10 `src/netcoredbg_mcp/prompts.py` — extend GUI debugging table with 12 new rows (brief), add 2 worked examples: virtualized item scroll+click, slider set_value with validation.

## Phase 4: Tests

- [ ] T11 `tests/test_pattern_expansion.py` — unit tests covering: happy path per tool, wrong-pattern rejection (e.g. `ui_set_value` on Button), input validation, pywinauto fallback. Target ≥ 20 new tests.
- [ ] T12 `tests/fixtures/SmokeTestApp/Program.cs` — add TreeView (Characters → Main cast → 3 leaves), Slider (Min=0, Max=100, Value=50), virtualized ListBox (500 items, AutomationId=`VirtList_Row_N`), TextBox for clipboard round-trip.
- [ ] T13 `tests/smoke_test_manual.py` — add 5 scenarios: `test_window_lifecycle`, `test_expand_collapse_tree`, `test_set_value_slider`, `test_realize_virtualized_item` (realize row 150, click, verify selected), `test_clipboard_roundtrip` (write → Ctrl+V into TextBox → read text).

## Phase 5: Build, version, ship

- [ ] T14 Build bridge self-contained (`dotnet publish -c Release -r win-x64 --self-contained true`), deploy to `~/.netcoredbg-mcp/bridge/FlaUIBridge.exe` + `D:/Bin/FlaUIBridge.exe`. Verify mtime + size.
- [ ] T15 Bump version `0.10.0` → `0.11.1` in `pyproject.toml` + `src/netcoredbg_mcp/__init__.py` + `uv.lock`.
- [ ] T16 Commit, push, create PR #49, run `/pr:review` with CodeRabbit + Gemini + Copilot, fix findings, merge squash, tag `v0.11.1` with structured release notes.
- [ ] T17 Update `CONTINUITY.md` + `.agent/CONTINUITY-CODER.md` with v0.11.1 delivery.

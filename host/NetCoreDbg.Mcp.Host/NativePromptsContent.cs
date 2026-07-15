namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Verbatim text content for the native prompt catalog, ported byte-for-byte from
/// <c>src/netcoredbg_mcp/prompts.py</c>. Kept in its own file, separate from
/// <c>NativePrompts.cs</c>, to mirror that file's own "content separated from
/// registration logic" structure.
///
/// The content blocks below are intentionally flush-left (no per-line indentation)
/// inside their raw string literals: C# raw string literals strip a common leading
/// -whitespace prefix from every line based on the closing delimiter's column, and
/// flush-left content with a flush-left closing delimiter guarantees zero stripping,
/// so every line here reproduces the Python source exactly, including its own
/// intentional interior indentation (e.g. nested list continuations).
/// </summary>
internal static partial class NativePrompts
{
    // ── Main debugging guide ────────────────────────────────────────────

    internal const string DebugGuideText = """
# .NET Debugger Guide

You control the debugger. The user cannot see debug output, cannot interact
with a paused app, cannot read variable values. Everything flows through you.

## The State Machine

Every tool response includes `state` and `next_actions`. Respect them.

```
IDLE ──start_debug──> RUNNING ──breakpoint──> STOPPED ──continue──> RUNNING
  ^                      |          |             |
  |                      v          v             v
  +---stop_debug---  TERMINATED  TERMINATED   TERMINATED
                    (exit/crash) (terminate)  (terminate)
```

If you call start_debug while already debugging, it will return an error. Call stop_debug first.

**IDLE** — No session. Only `start_debug` or `attach_debug`.
**RUNNING** — App executing. Old variable refs are INVALID. Do NOT call get_variables.
**STOPPED** — App FROZEN. UI won't paint. User cannot interact. Inspect then RESUME.
**TERMINATED** — App exited. Read output. Call stop_debug.

## Find Code Before Debugging

BEFORE setting breakpoints, locate the right code:
- If you have codebase search tools (SocratiCode, Serena, LSP), USE THEM:
  - Semantic search: "authentication handler" → finds the exact file and method
  - find_symbol("ProcessPayment") → exact location
  - find_references("SaveChanges") → all callers
- These tools work ALONGSIDE the debugger — use them to narrow down, THEN set targeted breakpoints
- Don't guess file names and line numbers — search first, debug second

## CRITICAL: Check State Before Asking User to Act

BEFORE asking the user to interact with the app (click a button, enter data, use the UI),
ALWAYS call `get_debug_state()` first. If state is STOPPED:
1. The app is FROZEN — the user CANNOT interact with it
2. Call `continue_execution()` to resume the app FIRST
3. THEN ask the user to interact

WRONG: "Please click the Save button" (while app is paused at breakpoint — user sees frozen window)
CORRECT: get_debug_state() → state=STOPPED → continue_execution() → state=RUNNING → "Please click Save"

This applies to ALL user-facing requests: clicking, typing, navigating menus, dragging, etc.
If you're not sure of the state, check it. The cost of checking is one tool call.
The cost of NOT checking is a confused user staring at a frozen app.

## Execution Tools Block Automatically

continue_execution, step_over, step_into, step_out all BLOCK until the program
stops again. You get the result (state, location, source context) in ONE call.
No polling needed. No loops. One call = one answer.

If execution times out (timed_out=True in response):
- The breakpoint was NOT hit — the code path may not have been reached
- Check: get_output_tail() — did the app crash or exit before the breakpoint?
- Check: list_breakpoints() — is the breakpoint verified (confirmed by debugger)?
- Try: add_function_breakpoint(function_name="MethodName") — catches all entry paths
- Try: configure_exceptions(filters=["all"]) — catch exceptions that bypass breakpoints

## The Inspect-Resume Cycle

**Quick path: use `get_stop_context()` — one call replaces steps 1-3.**
Returns call stack + variables + recent output in a single response.
Use the manual steps below only when you need to inspect specific scopes or set variables.

When stopped at a breakpoint:
1. get_call_stack() — where are you? Response includes surrounding source lines.
2. get_scopes(frame_id) — get variable scope references
3. get_variables(reference) — read actual values
3b. (Optional) Deep inspection:
   - analyze_collection(ref) — count, nulls, min/max for collections
   - summarize_object(ref, max_depth=2) — flatten nested objects
   - create_snapshot("name") — save variable state for later comparison
4. Decide: step deeper? continue? set more breakpoints?
5. RESUME — always resume. A frozen app = broken user experience.

## Evaluating While Running

If the app is RUNNING and you need a quick value check WITHOUT stopping:
```
quick_evaluate(expression="myVariable.Count")
```
This atomically pauses, evaluates, resumes — the user sees no pause.
Use for: checking counters, verifying state, monitoring values during execution.

## Output Is Your Responsibility

The user CANNOT see stdout/stderr. After significant execution:
- get_output_tail(lines=30) — check for errors, warnings, log messages
- NEVER say "check the console" or "look at output"
- Summarize what the program printed

## Build Warnings

Hidden by default. If debugging leads nowhere:
- get_build_diagnostics() — see ALL warnings
- CS8602 nullable → NullReferenceException
- NU1701 compatibility → assembly load failures
- CS4014 unawaited async → swallowed exceptions

## Clean Slate: Always Clear Before Starting Fresh

BEFORE starting a new debug scenario or investigation:
1. clear_breakpoints() for ALL files that had breakpoints in previous runs
2. configure_exceptions(filters=[]) — remove exception filters
3. clear_trace_log() — remove old tracepoint data

Leftover breakpoints cause UNEXPECTED STOPS. The user will report "app freezes"
or "app went into breakpoint" — and it's YOUR old breakpoints from a previous scenario.
This is the #1 cause of confusion during multi-scenario debugging sessions.

WRONG: start new investigation with old breakpoints still active
CORRECT: clear_breakpoints → set ONLY the breakpoints you need NOW → proceed

## Process Management

cleanup_processes() — view/kill tracked processes. Never use taskkill.
restart_debug(rebuild=True) — rebuild + relaunch after code changes.

## Tracepoints & Snapshots

When step-by-step inspection is too slow, use tracepoints:
1. add_tracepoint(file, line, expression) — logs expression value each time line is hit
2. continue_execution() — let the app run through the tracepoints
3. get_trace_log() — read all logged values in order
This replaces manual step-over loops for execution flow analysis.

Snapshots compare state between stops:
1. create_snapshot("before") — capture all locals
2. step_over() or continue_execution()
3. create_snapshot("after") — capture again
4. diff_snapshots("before", "after") — see exactly what changed

step_into with target selection:
1. get_step_in_targets() — see available functions on current line
2. step_into(target_id=N) — step into the specific function

## Edit-Rebuild-Retest Cycle

After finding and fixing a bug:
1. continue_execution() or stop_debug() — unfreeze the app
2. Edit the source code (your normal editing tools)
3. restart_debug(rebuild=True) — rebuilds and relaunches
4. Breakpoints PERSIST — no need to re-set them
5. Reproduce the scenario to verify the fix

If you need a clean slate:
1. stop_debug()
2. Edit code
3. start_debug(..., pre_build=True) — fresh session with rebuild

## Apply Code Changes at Runtime

When the session is STOPPED and the target uses an EnC-capable netcoredbg build,
prefer `apply_code_change` for supported method-body fixes:

1. Find the source with `find_code_symbol`, `find_code_references`,
   `get_source_context`, or `search_source`.
2. Confirm the app is in STOPPED state with `get_debug_state()`.
3. Call `apply_code_change(file="Relative/Path.cs", edits=[...])` with
   1-based inclusive line ranges.
4. Read the result. On success, the source file and runtime method body are
   updated; the session remains STOPPED so you decide when to continue.
5. Call `continue_execution()` or step through the changed path to verify the
   runtime behavior.

`apply_code_change` requires `ncdbhook.dll` next to `netcoredbg.exe`; if the
tool reports missing EnC support, run `netcoredbg-mcp setup --enc` or restart
with a manually built ncdbhook-enabled debugger. Rude edits such as adding
fields, changing method signatures, or changing generics cannot be applied at
runtime. When you get a rude edit, use `restart_debug(rebuild=True)` instead.

## Multi-Threaded Debugging

When the app has multiple threads (WPF UI + background workers, ASP.NET request threads):

1. get_threads() — see all threads with names
2. get_call_stack(thread_id=N) — inspect a specific thread's stack
3. Most tools default to `current_thread_id` — the thread that triggered the stop
4. To switch threads: pass explicit `thread_id` to get_call_stack, step_*, continue_*

Common patterns:
- UI thread frozen → check if background thread holds a lock (pause → get_threads → inspect all stacks)
- Wrong thread stopped → continue_execution(thread_id=current) to resume only that thread
- Background crash → get_exception_context shows which thread threw

## Build Failures

If start_debug or restart_debug fails due to build error:
1. The response includes the error message — READ IT
2. get_build_diagnostics() — see all compiler errors and warnings
3. Fix the code
4. Retry: start_debug(...) or restart_debug(rebuild=True)
Common: CS1002 (missing semicolon), CS0103 (undefined name), CS0246 (missing using)

## Valid Actions by State

| State | You CAN do |
|-------|-----------|
| IDLE | start_debug, attach_debug |
| RUNNING | pause_execution, get_output*, get_debug_state, debuggee_activity, add_breakpoint, stop_debug, quick_evaluate |
| STOPPED | get_call_stack, get_variables, get_scopes, evaluate_expression, step_*, continue_execution, set_variable, ui_*, apply_code_change, stop_debug, add_tracepoint, create_snapshot, analyze_collection, summarize_object, get_step_in_targets |
| TERMINATED | get_output*, stop_debug, start_debug |

*get_output, get_output_tail, search_output work in all non-IDLE states.

## Which Prompt to Use

| Situation | Prompt |
|-----------|--------|
| First time debugging | debug (this guide) |
| GUI app (WPF/Avalonia/WinForms) | debug-gui |
| Exception or crash | debug-exception |
| Need to see the UI visually | debug-visual |
| Known exception type or symptom | investigate("NullReferenceException") |
| Specific problem description | debug-scenario("button click doesn't save") |
| Quick anti-pattern check | debug-mistakes |

## MCP Resources

Subscribe to these for real-time updates (no polling needed):
- debug://state — current session state (JSON, updates on every state change)
- debug://breakpoints — all active breakpoints (JSON)
- debug://output — program stdout/stderr (text)
- debug://threads — active threads (JSON, updates when stopped)

""";

    // -- DAP escape hatch -------------------------------------------------

    internal const string DapEscapeHatchText = """
# DAP Escape Hatch

Most debugger workflows should use the typed MCP tools first. When DAP exposes
a command that netcoredbg-mcp has not wrapped yet, use the lower-level DAP
client path and call `send_request(command, args)` with the command name and
arguments from the DAP spec.

## Unwrapped Commands

- `cancel` — ask the adapter to cancel a long-running request.
  Example: `send_request("cancel", {"requestId": 12})`
- `restart` — restart the current debuggee when the adapter supports restart.
  Example: `send_request("restart", {"arguments": {}})`
- `restartFrame` — restart execution at a stack frame.
  Example: `send_request("restartFrame", {"frameId": 42})`
- `goto` — continue execution at a target returned by `gotoTargets`.
  Example: `send_request("goto", {"threadId": 1, "targetId": 7})`
- `gotoTargets` — list possible goto targets for a source location.
  Example: `send_request("gotoTargets", {"source": {"path": "Program.cs"}, "line": 25})`
- `stepBack` — step backwards when the adapter supports reverse execution.
  Example: `send_request("stepBack", {"threadId": 1})`
- `reverseContinue` — continue backwards until the adapter stops again.
  Example: `send_request("reverseContinue", {"threadId": 1})`
- `terminateThreads` — terminate selected threads.
  Example: `send_request("terminateThreads", {"threadIds": [1, 2]})`
- `setInstructionBreakpoints` — set breakpoints at instruction addresses.
  Example: `send_request("setInstructionBreakpoints", {"breakpoints": []})`
- `source` — fetch source contents by `sourceReference`.
  Example: `send_request("source", {"sourceReference": 7})`
- `completions` — request expression completions at a frame and cursor position.
  Example: `send_request("completions", {"frameId": 42, "text": "obj.", "column": 5})`
- `setExpression` — assign a new value to an expression.
  Example: `send_request("setExpression", {"expression": "counter", "value": "10"})`

Prefer adding a typed MCP wrapper when a command becomes common in agent
workflows. The escape hatch is for rare or adapter-version-specific operations.

""";

    // ── GUI app debugging ───────────────────────────────────────────────

    internal const string DebugGuiText = """
# Debugging WPF / Avalonia / WinForms Apps

GUI apps have a critical difference from console apps: the UI thread.
When the debugger pauses, the ENTIRE UI thread freezes. The window stops
painting, buttons stop responding, animations freeze mid-frame.

## The Golden Rules

**1. NEVER set breakpoints before the window is visible.**

**2. ALWAYS check debug state before asking user to interact with the app.**
Call `get_debug_state()`. If STOPPED → `continue_execution()` first. A paused
GUI app has a frozen, unresponsive window — the user cannot click or type anything.

Setting breakpoints in initialization code (constructors, OnLoaded, App.xaml.cs)
before starting the app will freeze the app DURING initialization. The window
will never appear. The user will think the app crashed.

## Correct Workflow

```
# 1. Launch (no breakpoints yet!)
start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj")
# Response has app_type="gui"

# 2. Confirm window loaded
ui_get_window_tree()

# 3. (Optional) See the actual UI
ui_take_annotated_screenshot()

# 4. NOW set breakpoints
add_breakpoint(file="MainViewModel.cs", line=42)

# 5. Trigger the code path via UI interaction
ui_click(automation_id="btnSave")

# 6. Execution blocks until breakpoint hit — inspect
get_call_stack()
get_scopes(frame_id=...)
get_variables(ref=...)

# 7. RESUME — the app is frozen while you inspect
continue_execution()

# 8. Done
stop_debug()
```

## Stealth Mode

Use stealth mode when the user needs to keep working in another foreground app
while you inspect or drive a GUI debuggee in the background:

```
start_debug(
    program="bin/Debug/net8.0/App.dll",
    build_project="App.csproj",
    stealth_mode=True,
)
ui_get_window_tree()
ui_click(automation_id="btnSave")
```

In stealth mode, UI tree reads and automation-id clicks avoid foreground
activation. `ui_send_keys` and `ui_send_keys_batch` use flash-focus: the bridge
briefly activates the debuggee, sends input, then restores the previous
foreground window. Screenshots use `PrintWindow`; if WPF renders a blank frame,
the bridge may fall back to flash-focus capture and report that fallback.

Call `ui_bring_to_front()` when you explicitly want to exit stealth behavior and
activate the debuggee window. After that, normal foreground-oriented UI driving
is appropriate.

## Exception — Startup Debugging

If the bug IS in startup code, use stop_at_entry:
```
start_debug(program="App.dll", ..., stop_at_entry=True)
# App pauses at Main() — before any UI
step_over()  # step through init one line at a time
```

## UI Interaction While Debugging

When the app is RUNNING (not stopped at breakpoint), you can interact with it.
If FlaUIBridge.exe is available, interactions use FlaUI (UIA3) for reliable
access to WPF patterns (SelectionItem, ExpandCollapse, Value). Without it,
pywinauto is used as fallback — works for most controls.

| Action | Tool |
|--------|------|
| Activate button (preferred) | ui_invoke(automation_id="btnSave") — uses InvokePattern, no mouse |
| Bring debuggee to foreground | ui_bring_to_front() — exits stealth by explicitly activating the window |
| Click button (coordinate) | ui_click(automation_id="btnSave") — mouse click, needs visible element |
| Toggle checkbox | ui_toggle(automation_id="chkEnabled") — returns new state On/Off |
| Complete file dialog | ui_file_dialog(path="C:/data/test.txt") — enters path and clicks Open |
| Type text | ui_send_keys(keys="hello", automation_id="txtInput") |
| Right-click | ui_right_click(automation_id="dataGrid") |
| Double-click | ui_double_click(automation_id="listItem") |
| Select rows | ui_select_items(automation_id="grid", indices=[0,1,2]) |
| Scroll | ui_scroll(automation_id="list", direction="down", amount=5) |
| Drag (threshold-safe) | ui_drag(from_automation_id="src", to_automation_id="dst", speed_ms=200) — crosses WPF drag threshold, triggers DoDragDrop |
| Drag with modifier | ui_drag(from_x=..., from_y=..., to_x=..., to_y=..., speed_ms=300, hold_modifiers=["ctrl"]) |
| Complex keys | ui_set_focus(automation_id="grid") then ui_send_keys_focused(keys="^{END}") |
| Scoped search | ui_click(automation_id="btn", root_id="panel1") — search within subtree |
| XPath search | ui_click(xpath="//Button[@Name='Save']") — FlaUI backend only |
| OS theme toggle | ui_send_system_event(event="theme_change", mode="toggle") — fires SystemEvents.UserPreferenceChanged in debuggee |
| Hold Ctrl across clicks | ui_hold_modifiers(modifiers=["ctrl"]) → multiple ui_click → ui_release_modifiers(modifiers="all") |
| Inspect held modifiers | ui_get_held_modifiers() — returns the list of currently-held modifier names |
| Close window | ui_close_window(window_title="Dialog") — close modal by title; omit for main window |
| Maximize window | ui_maximize_window() — maximize main window via WindowPattern |
| Minimize window | ui_minimize_window() — minimize main window via WindowPattern |
| Restore window | ui_restore_window() — restore from maximized/minimized to normal |
| Move window | ui_move_window(x=100, y=100) — move window; returns {moved: false} if CanMove=false |
| Resize window | ui_resize_window(width=800, height=600) — resize; returns {resized: false} if CanResize=false |
| Expand tree node | ui_expand(automation_id="CharactersTreeRoot") — ExpandCollapsePattern; safe on already-expanded |
| Collapse tree node | ui_collapse(automation_id="CharactersTreeRoot") — ExpandCollapsePattern |
| Set slider value | ui_set_value(automation_id="DurationSlider", value=75.0) — RangeValuePattern; returns {set: false, reason} on out-of-range |
| Read clipboard | ui_clipboard_read() — returns {text: "...", has_text: bool} via STA thread |
| Write clipboard | ui_clipboard_write(text="hello") — writes Unicode text; use before Ctrl+V |
| Realize virtualized item | ui_realize_virtualized_item(container_automation_id="VirtList", property="AutomationId", value="VirtList_Row_150") — idempotent; safe to re-realize |

PREFER ui_invoke over ui_click for buttons — it works even when the element is off-screen or obscured.
PREFER ui_toggle over ui_click for checkboxes — it returns the actual state after toggling.

### Worked example A: Realize a virtualized row and click it

```
# A DataGrid virtualizes rows beyond the visible viewport.
# Row 150 does not have an AutomationElement until realized.
result = ui_realize_virtualized_item(
    container_automation_id="CueDataGrid",
    property="AutomationId",
    value="CueDataGrid_Row_150"
)
# result: {realized: true, element_id: "CueDataGrid_Row_150", bounding_rect: {...}}
# Now the item is in the visual tree — click it normally:
ui_click(automation_id="CueDataGrid_Row_150")
# ui_realize_virtualized_item is idempotent: calling it again on the same row is safe.
```

### Worked example B: Set slider value with out-of-range handling

```
# Set a WPF Slider (Min=0, Max=100) to 75:
result = ui_set_value(automation_id="DurationSlider", value=75.0)
# result: {set: true, automation_id: "DurationSlider", value: 75.0, minimum: 0.0, maximum: 100.0}

# Attempt with out-of-range value:
result = ui_set_value(automation_id="DurationSlider", value=200.0)
# result: {set: false, reason: "value 200.0 out of range [0.0..100.0]", minimum: 0.0, maximum: 100.0}
# No exception is raised — check result["set"] to determine success.
```

### Ctrl+click multi-select workflow

Discontiguous multi-selection (Ctrl+click) requires the Ctrl modifier to stay
held across multiple discrete click calls. `ui_send_keys_batch` releases
modifiers at the end of each batch, so use the persistent-modifier primitives:

```
ui_hold_modifiers(modifiers=["ctrl"])
ui_click(automation_id="Row_3")
ui_click(automation_id="Row_5")
ui_click(automation_id="Row_7")
ui_release_modifiers(modifiers="all")
# Rows 3, 5, 7 are now selected on a SelectionMode=Extended/MultiExtended control.
```

The bridge auto-releases any held modifiers on graceful shutdown (stdin EOF,
disconnect, or normal process exit) so the session does not leave
Ctrl/Shift/Alt stuck across the Windows desktop. Force-kill paths are not
supported and may still require manual recovery.

## WinForms Differences

WinForms controls expose UIA properties differently from WPF:
- `AccessibleName` → UIA `Name` property (NOT `AutomationId`)
- `Control.Name` → UIA `AutomationId` (but often blank)
- Many controls lack AutomationId entirely → use `name=` parameter instead of `automation_id=`

When element search fails by automationId, try:
1. ui_find_element(name="Save") — by visible text/AccessibleName
2. ui_get_window_tree(max_depth=3) — inspect what's actually in the tree
3. ui_take_annotated_screenshot() — see elements visually with IDs

## Debugging Intelligence Tools

| Action | Tool |
|--------|------|
| Trace execution flow | add_tracepoint(file, line, expression) → get_trace_log() |
| Compare state between stops | create_snapshot("before") → step_over() → create_snapshot("after") → diff_snapshots("before", "after") |
| Analyze collection | analyze_collection(variables_reference=ref) — count, nulls, min/max, duplicates |
| Summarize nested object | summarize_object(variables_reference=ref, max_depth=2) — flat property list |

TRACEPOINT WORKFLOW: Set 5-10 tracepoints on key lines → continue → read trace log.
This replaces manual step-by-step inspection for execution flow analysis.
SNAPSHOT WORKFLOW: Snapshot before action → step_over()/step_into()/continue_execution() → snapshot after → diff to see exact changes.

## Key Syntax Quick Reference

Modifier prefixes apply to the NEXT character or {KEY}:
- `^` = Ctrl, `%` = Alt, `+` = Shift
- Alt+Z → `"%z"`, Ctrl+C → `"^c"`, Shift+Tab → `"+{TAB}"`
- Alt+F4 → `"%{F4}"`, Ctrl+Shift+S → `"^+s"`
- Special keys in braces: `{ENTER}`, `{TAB}`, `{ESC}`, `{LEFT}`, `{RIGHT}`, `{UP}`, `{DOWN}`

WRONG: `"{ALT}z"`, `"Alt+Z"`, `"{ALT}{Z}"`
RIGHT: `"%z"`

## "App Doesn't Respond" Checklist

1. get_debug_state() — is it STOPPED? If yes, you hit a breakpoint. Resume.
2. Is it RUNNING but UI frozen? Possible deadlock.
   pause_execution() → get_call_stack() for all threads → look for async deadlock.
3. Still unclear? ui_take_screenshot() — see what the user sees.

""";

    // ── Exception investigation ─────────────────────────────────────────

    internal const string DebugExceptionText = """
## Exception Investigation Protocol

### Quick Path: One Call (preferred)
```
get_exception_context()
```
Returns exception info + call stack + local variables + recent output in ONE call.
Use this FIRST. Only use the manual steps below if you need more detail.

Execute in order. Do NOT skip steps.

### Step 1: Get Exception Details
```
get_exception_info()
```
Read: exception type, message, inner exception if any.

### Step 2: Get Stack Trace
```
get_call_stack()
```
Find: which method threw, which file/line, the call chain.
Response includes 5 lines of source around the crash point.

### Step 3: Inspect Local State
```
get_scopes(frame_id=<from step 2>)
get_variables(variables_reference=<from scopes>)
```
Look for: null values, unexpected types, out-of-range indices.

### Step 4: Check Recent Output
```
get_output_tail(lines=30)
```
Look for: error messages logged before the exception, failed operations.

### Step 5: Report to User

Summarize clearly:
- **What:** Exception type and message
- **Where:** File, line, method name
- **Why (likely):** Based on local variable state
- **Fix (suggested):** Concrete code change

### Step 6: Decision
- continue_execution() — skip this exception, see if there are more
- stop_debug() — end session, go fix the code
- set_variable(...) — modify a value and continue (test hypothesis)

### If No Exception Was Caught
If the app crashes without the debugger stopping:
```
configure_exceptions(filters=["all"])
restart_debug()
```
This enables breaking on ALL exceptions, including caught ones. Reproduce the crash — the debugger will now stop at the throw site.

### Common .NET Exceptions

| Exception | Usual Cause | What to Check |
|-----------|------------|---------------|
| NullReferenceException | Uninitialized or missing data | get_variables — find the null |
| InvalidOperationException | Wrong state (collection modified, disposed object) | Call stack — what operation was attempted |
| ArgumentOutOfRangeException | Bad index or parameter | get_variables — check index vs collection.Count |
| ObjectDisposedException | Using disposed resource | Call stack — trace the object's lifetime |
| TaskCanceledException | Timeout or cancellation | Check CancellationToken state, timeout values |

""";

    // ── Visual UI inspection ────────────────────────────────────────────

    internal const string DebugVisualText = """
# Visual UI Inspection

Sometimes the automation tree isn't enough. You need to SEE the UI.

## Basic Screenshot

```
ui_take_screenshot()
```
Returns base64 PNG of the app window. You see what the user sees.

Use when:
- Verifying layout after a debug step
- Checking if an element is visually present but not in automation tree
- Debugging rendering issues (wrong colors, clipped text, overlapping elements)

## Annotated Screenshot (Set-of-Mark)

```
ui_take_annotated_screenshot(max_depth=3, interactive_only=True)
```
Returns screenshot with numbered red boxes around interactive elements,
PLUS a JSON element index.

Response:
```json
{
  "image": "base64_png...",
  "elements": [
    {"id": 1, "name": "Save", "type": "Button", "automationId": "btnSave"},
    {"id": 2, "name": "", "type": "TextBox", "automationId": "txtName"}
  ]
}
```

Then click by number:
```
ui_click_annotated(element_id=1)  # clicks "Save" button
```

Use when:
- Element has no AutomationId (use annotation ID instead)
- You need to understand spatial layout before acting
- Multiple similar elements and you need to pick the right one

## Workflow: Debug a Visual Bug

```
# 1. Screenshot to see current state
ui_take_screenshot()

# 2. Set breakpoint in the rendering/data-binding code
add_breakpoint(file="ItemTemplate.xaml.cs", line=30)

# 3. Trigger re-render
ui_click(automation_id="btnRefresh")

# 4. Inspect at breakpoint
get_call_stack()
get_variables(ref=...)

# 5. Step through rendering logic
step_over()
step_over()

# 6. Resume and screenshot again
continue_execution()
ui_take_screenshot()

# 7. Compare before/after visually
```

## Multi-Row Selection in DataGrid

For selecting multiple rows, do NOT use click coordinates. Use UIA pattern:
```
ui_select_items(automation_id="dataGrid", indices=[4,5,6,7,8], mode="replace")
```
This uses SelectionItemPattern — works even for off-screen rows, no scrolling needed.

""";

    // ── Anti-patterns reference ─────────────────────────────────────────

    internal const string DebugMistakesText = """
# Debugging Anti-Patterns

## 1. Setting breakpoints before GUI window loads

WRONG:
```
start_debug(program="App.dll", ...)
add_breakpoint(file="MainWindow.xaml.cs", line=10)
# Window never appears. App frozen during init.
```

CORRECT:
```
start_debug(program="App.dll", ...)
ui_get_window_tree()   # wait for window
add_breakpoint(file="MainWindow.xaml.cs", line=10)
```

## 2. Polling for state

WRONG:
```
continue_execution()   # before v0.2: returned immediately, needed polling
get_debug_state()      # running...
get_debug_state()      # running...
get_debug_state()      # stopped!
```

CORRECT:
```
continue_execution()   # BLOCKS until stopped. One call. Done.
```

## 3. Using stale variable references

WRONG:
```
scopes = get_scopes(frame_id=1)
vars = get_variables(ref=42)
continue_execution()         # refs invalidated!
get_variables(ref=42)        # GARBAGE or error
```

CORRECT:
```
continue_execution()
new_scopes = get_scopes(frame_id=...)   # fresh refs
get_variables(ref=new_scopes[0].ref)    # valid
```

## 4. Telling user to "check the console"

WRONG:
```
"Check the debug output for errors"
"Look at the console for the exception message"
```

CORRECT:
```
output = get_output_tail(lines=30)
"The app logged: 'Connection timeout after 30s to database server'"
```

## 5. Leaving the app frozen at breakpoint

WRONG:
```
get_call_stack()
get_variables(ref=...)
# ... thinking for 30 seconds while app is frozen ...
```

CORRECT:
```
get_call_stack()
get_variables(ref=...)
continue_execution()  # resume THEN think about findings
```

## 6. Guessing instead of inspecting

WRONG:
```
"The variable is probably null because the constructor didn't initialize it"
```

CORRECT:
```
get_variables(ref=...)
"The variable `customer` IS null. The `LoadCustomer()` returned null
because the query found 0 results for ID=42."
```

## 7. Using taskkill instead of the process manager

WRONG:
```
taskkill /F /IM dotnet.exe   # kills ALL dotnet processes
```

CORRECT:
```
cleanup_processes(force=True)  # kills only our processes
```

## 8. Ignoring build warnings

WRONG:
```
# Build succeeded, moving on...
# App crashes with NullReferenceException
# "I don't know why"
```

CORRECT:
```
get_build_diagnostics()
# CS8602: Possible null reference. Line 42 in Service.cs
# THAT'S why — nullable dereference the compiler warned about!
```

## 9. Not reading source context from execution response

WRONG:
```
continue_execution()
# Response has location + source_context but agent ignores it
# Reads the entire file separately
```

CORRECT:
```
continue_execution()
# Response: stopped at Service.cs:42, source_context shows 5 lines around it
# Already have the context — no need to read the file
```

## 10. Asking user to interact with paused app

WRONG:
```
# App is stopped at breakpoint (agent doesn't check)
"Please click the Save button in the app"
# User sees frozen, unresponsive window
```

CORRECT:
```
get_debug_state()        # state=STOPPED
continue_execution()     # resume app
"Please click the Save button"
```

Always check state before ANY request for user interaction with the app.

""";
}

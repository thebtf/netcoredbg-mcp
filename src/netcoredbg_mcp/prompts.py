"""MCP prompts — inline skills for AI agent debugging.

Each prompt is a self-contained guide for a specific debugging scenario.
The agent receives these when connecting to the MCP server and can invoke
them via the MCP prompts mechanism.

Design principles:
- Every prompt answers: what to do, in what order, and what NOT to do
- Anti-patterns are shown as concrete WRONG/CORRECT pairs
- State machine awareness is embedded in every workflow
- User cannot see debug output — agent must read and report everything
"""

from mcp.server.fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register MCP prompts (inline skills) on the server."""

    # ── Main debugging guide ────────────────────────────────────────────

    @mcp.prompt(
        name="debug",
        description=(
            "Complete guide to debugging .NET apps. "
            "Start here before your first debug session. "
            "Covers state machine, tool usage, anti-patterns, workflows."
        ),
    )
    def debug_guide() -> list[dict]:
        """The foundational debugging skill — read this first."""
        return [{"role": "user", "content": _DEBUG_GUIDE}]

    # ── GUI app debugging ───────────────────────────────────────────────

    @mcp.prompt(
        name="debug-gui",
        description=(
            "WPF and Avalonia Desktop UI debugging workflow. "
            "Use when debugging GUI apps — critical breakpoint timing "
            "and UI interaction rules that differ from console apps."
        ),
    )
    def debug_gui() -> list[dict]:
        """GUI-specific debugging — WPF, WinForms, Avalonia."""
        return [{"role": "user", "content": _DEBUG_GUI}]

    # ── Exception investigation ─────────────────────────────────────────

    @mcp.prompt(
        name="debug-exception",
        description=(
            "Step-by-step exception investigation. "
            "Use when the debugger stops on an exception or app crashes."
        ),
    )
    def debug_exception() -> list[dict]:
        """Exception investigation protocol."""
        return [
            {"role": "user", "content": "The debugger stopped on an exception."},
            {"role": "assistant", "content": "I'll investigate. Let me gather details."},
            {"role": "user", "content": _DEBUG_EXCEPTION},
        ]

    # ── Visual UI inspection ────────────────────────────────────────────

    @mcp.prompt(
        name="debug-visual",
        description=(
            "Visual UI inspection via screenshots and Set-of-Mark annotation. "
            "Use when you need to SEE the app UI, verify layout, or click "
            "elements by visual position."
        ),
    )
    def debug_visual() -> list[dict]:
        """Screenshot and annotation workflow."""
        return [{"role": "user", "content": _DEBUG_VISUAL}]

    # ── Anti-patterns reference ─────────────────────────────────────────

    @mcp.prompt(
        name="debug-mistakes",
        description=(
            "Common debugging anti-patterns with WRONG/CORRECT examples. "
            "Use as a checklist to avoid known pitfalls."
        ),
    )
    def debug_mistakes() -> list[dict]:
        """What NOT to do — concrete anti-patterns."""
        return [{"role": "user", "content": _DEBUG_MISTAKES}]

    # ── Parameterized: targeted investigation ───────────────────────────

    @mcp.prompt(
        name="investigate",
        description=(
            "Targeted investigation for a specific exception type or symptom. "
            "Pass the exception name or symptom description to get a focused "
            "debugging plan with exact tools and steps."
        ),
    )
    def investigate(symptom: str, app_type: str = "gui") -> list[dict]:
        """Generate a targeted investigation plan based on symptom."""
        plan = _build_investigation_plan(symptom, app_type)
        return [{"role": "user", "content": plan}]

    # ── Parameterized: debug scenario ───────────────────────────────────

    @mcp.prompt(
        name="debug-scenario",
        description=(
            "Get a step-by-step debugging plan for a specific scenario. "
            "Pass a description of the problem and get exact tool calls to execute."
        ),
    )
    def debug_scenario(
        problem: str,
        app_type: str = "gui",
        file_hint: str = "",
    ) -> list[dict]:
        """Generate a debugging plan for a specific problem."""
        plan = _build_scenario_plan(problem, app_type, file_hint)
        return [{"role": "user", "content": plan}]


# ═══════════════════════════════════════════════════════════════════════
# Prompt content — separated for readability and testability
# ═══════════════════════════════════════════════════════════════════════

_DEBUG_GUIDE = """\
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
| RUNNING | pause_execution, get_output*, get_debug_state, add_breakpoint, stop_debug, quick_evaluate |
| STOPPED | get_call_stack, get_variables, get_scopes, evaluate_expression, step_*, continue_execution, set_variable, ui_*, stop_debug, add_tracepoint, create_snapshot, analyze_collection, summarize_object, get_step_in_targets |
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
"""

_DEBUG_GUI = """\
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
| Click button (coordinate) | ui_click(automation_id="btnSave") — mouse click, needs visible element |
| Toggle checkbox | ui_toggle(automation_id="chkEnabled") — returns new state On/Off |
| Complete file dialog | ui_file_dialog(path="C:/data/test.txt") — enters path and clicks Open |
| Type text | ui_send_keys(keys="hello", automation_id="txtInput") |
| Right-click | ui_right_click(automation_id="dataGrid") |
| Double-click | ui_double_click(automation_id="listItem") |
| Select rows | ui_select_items(automation_id="grid", indices=[0,1,2]) |
| Scroll | ui_scroll(automation_id="list", direction="down", amount=5) |
| Drag | ui_drag(from_automation_id="src", to_automation_id="dst") |
| Complex keys | ui_set_focus(automation_id="grid") then ui_send_keys_focused(keys="^{END}") |
| Scoped search | ui_click(automation_id="btn", root_id="panel1") — search within subtree |
| XPath search | ui_click(xpath="//Button[@Name='Save']") — FlaUI backend only |

PREFER ui_invoke over ui_click for buttons — it works even when the element is off-screen or obscured.
PREFER ui_toggle over ui_click for checkboxes — it returns the actual state after toggling.

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
"""

_DEBUG_EXCEPTION = """\
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
"""

_DEBUG_VISUAL = """\
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
"""

_DEBUG_MISTAKES = """\
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
"""

# ═══════════════════════════════════════════════════════════════════════
# Parameterized prompt builders
# ═══════════════════════════════════════════════════════════════════════

# Exception-specific investigation knowledge base
_EXCEPTION_PLAYBOOKS: dict[str, str] = {
    "nullreferenceexception": """\
## NullReferenceException Investigation

This is the #1 .NET exception. Something is null that shouldn't be.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Find the null:
```
get_scopes(frame_id=...)
get_variables(ref=...)    # scan locals for null values
```

### Step 3: Trace the null backwards
Look at the null variable. How was it assigned?
```
# Set breakpoint at the assignment point
add_breakpoint(file="...", line=<assignment_line>)
restart_debug(rebuild=False)
# When hit, inspect what the source returns
```

### Step 4: Common causes
- Database query returned no results (FirstOrDefault → null)
- Dependency injection not registered (service is null)
- JSON deserialization missing property (model field is null)
- UI element not found (FindName returns null)
- Race condition: value set after null check but before use

### Step 5: Verify fix
```
set_variable(ref=..., name="suspect", value="new object()")
continue_execution()
# If it works → confirms the null was the issue
```
""",
    "invalidoperationexception": """\
## InvalidOperationException Investigation

Something was called at the wrong time or in the wrong state.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Read the message carefully — it usually tells you EXACTLY what's wrong:
- "Collection was modified; enumeration operation may not proceed"
- "Sequence contains no elements"
- "Cannot access a disposed object"

Inspect state:
```
get_variables(ref=...)
# Look for: disposed objects, empty collections, wrong phase of lifecycle
```

### Step 3: Common causes by message
- "Collection was modified" → iterating while adding/removing. Use .ToList() first.
- "Sequence contains no elements" → .First() on empty. Use .FirstOrDefault().
- "disposed object" → using a resource after its scope ended. Check using/IDisposable.
- "not on UI thread" → cross-thread UI access. Use Dispatcher.Invoke.
""",
    "taskcanceledexception": """\
## TaskCanceledException / OperationCanceledException Investigation

An async operation was cancelled — usually a timeout or explicit cancellation.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Check cancellation source:
```
get_variables(ref=...)
# Look for: CancellationToken.IsCancellationRequested
# Look for: HttpClient.Timeout value
# Look for: Task.Delay with cancellation
# Look for: HttpClient calls, database queries, CancellationToken usage
```

### Step 3: Common causes
- HttpClient timeout (default 100s) — endpoint too slow or unreachable
- CancellationToken from request pipeline — user navigated away
- Task.WhenAny with timeout task winning — operation took too long
- Disposed HttpClient or DbContext cancelling pending operations
""",
    "objectdisposedexception": """\
## ObjectDisposedException Investigation

Using a resource after it was disposed.

### Step 1: Quick context (one call)
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output.

### Step 2: Deeper investigation (if needed)

Find where it was disposed:
```
add_function_breakpoint(function_name="Dispose")
restart_debug(rebuild=False)
# When Dispose hits, check call stack — who disposed it and when
```

### Step 3: Common causes
- DbContext in async/closure: `using var db = ...; Task.Run(() => db.Query())`
- HttpClient disposed by DI container while request in flight
- Timer callback accessing disposed resources
- WPF binding accessing disposed ViewModel
""",
    "deadlock": """\
## Deadlock Investigation

App stops responding. No exception. UI frozen. No crash.

### Step 1: Pause and inspect all threads
```
pause_execution()
get_threads()
# For each thread:
get_call_stack(thread_id=<each>)
```

### Step 2: Look for the pattern
- Thread A waiting on Thread B (lock, Monitor.Enter, SemaphoreSlim)
- Thread B waiting on Thread A
- OR: Task waiting for UI thread (.Result or .Wait() in WPF)

### Step 3: Classic WPF/Avalonia deadlock
```csharp
// DEADLOCK: .Result blocks UI thread, task needs UI thread to complete
public void OnClick() {
    var result = GetDataAsync().Result;  // BLOCKS UI THREAD
}
```
Fix: `async void OnClick() { var result = await GetDataAsync(); }`

### Step 4: Verify
```
restart_debug()
# Reproduce the scenario
# If app responds now → deadlock was the issue
```
""",
    "crash": """\
## App Crash Investigation

App terminates unexpectedly.

### Step 1: Catch everything
```
configure_exceptions(filters=["all"])
start_debug(program="...", build_project="...")
```

### Step 2: Reproduce the crash
If GUI app: interact with UI to trigger crash path.
If console: let it run — exception will be caught.

### Step 3: When exception hits
```
get_exception_context()
```
This returns exception info + call stack + variables + recent output in ONE call.

### Step 4: Deeper investigation (if needed)
```
get_variables(ref=...)    # specific scope inspection
get_output_tail(lines=50) # last log messages before crash
```

### Step 5: If no exception caught
App may crash in native code (access violation, stack overflow).
```
get_output()  # check for native crash messages
# Look for: "Process terminated with exit code -1073741819" (access violation)
# Look for: "Stack overflow" in output
```
""",
    "performance": """\
## Performance Issue Investigation

App is slow, laggy, or uses too much CPU/memory.

### Step 1: Identify the slow operation
```
start_debug(program="...", build_project="...")
# Set breakpoint BEFORE the slow operation
add_breakpoint(file="...", line=<before_slow_code>)
```

### Step 2: Step through and time
```
# When breakpoint hits:
step_over()  # one line at a time
# Watch: which step_over takes noticeably longer to return?
# That's your bottleneck.
```

### Step 3: Inspect the bottleneck
```
get_variables(ref=...)
evaluate_expression("collection.Count")  # large collection?
evaluate_expression("query.ToQueryString()")  # N+1 query?
```

### Step 4: Common causes
- N+1 database queries (loop calling DB per item)
- Synchronous I/O on UI thread
- Large collection iteration without pagination
- Unnecessary re-rendering in MVVM (property changed spam)
""",
}

# Symptom keywords → playbook mapping
_SYMPTOM_MAPPING: dict[str, str] = {
    "null": "nullreferenceexception",
    "nullreference": "nullreferenceexception",
    "nullreferenceexception": "nullreferenceexception",
    "object reference not set": "nullreferenceexception",
    "invalidoperation": "invalidoperationexception",
    "invalidoperationexception": "invalidoperationexception",
    "collection was modified": "invalidoperationexception",
    "sequence contains no elements": "invalidoperationexception",
    "disposed": "objectdisposedexception",
    "objectdisposed": "objectdisposedexception",
    "objectdisposedexception": "objectdisposedexception",
    "cancel": "taskcanceledexception",
    "timeout": "taskcanceledexception",
    "taskcanceled": "taskcanceledexception",
    "operationcanceled": "taskcanceledexception",
    "deadlock": "deadlock",
    "freeze": "deadlock",
    "hang": "deadlock",
    "not responding": "deadlock",
    "crash": "crash",
    "terminated": "crash",
    "exit code": "crash",
    "access violation": "crash",
    "slow": "performance",
    "performance": "performance",
    "lag": "performance",
    "high cpu": "performance",
    "memory": "performance",
    "argumentnull": "nullreferenceexception",
    "argumentnullexception": "nullreferenceexception",
    "filenotfound": "crash",
    "directorynotfound": "crash",
    "ioexception": "crash",
    "stackoverflow": "crash",
    "stackoverflowexception": "crash",
    "httprequest": "taskcanceledexception",
    "httprequestexception": "taskcanceledexception",
    "network": "taskcanceledexception",
    "connection refused": "taskcanceledexception",
    "json": "invalidoperationexception",
    "jsonexception": "invalidoperationexception",
    "deserialization": "invalidoperationexception",
    "format": "invalidoperationexception",
    "formatexception": "invalidoperationexception",
    "parse error": "invalidoperationexception",
    "sqlexception": "invalidoperationexception",
    "database": "invalidoperationexception",
    "dbupdate": "invalidoperationexception",
}


def _build_investigation_plan(symptom: str, app_type: str) -> str:
    """Build a targeted investigation plan based on symptom description."""
    symptom_lower = symptom.lower().strip()

    # Find matching playbook
    playbook_key = None
    for keyword, key in _SYMPTOM_MAPPING.items():
        if keyword in symptom_lower:
            playbook_key = key
            break

    if playbook_key and playbook_key in _EXCEPTION_PLAYBOOKS:
        playbook = _EXCEPTION_PLAYBOOKS[playbook_key]
        header = f"# Investigation Plan: {symptom}\n\nApp type: {app_type}\n\n"
        if app_type == "gui":
            header += (
                "**GUI app reminder:** App UI is frozen while stopped. "
                "Resume after inspecting. Set breakpoints only after window is visible.\n\n"
            )
        return header + playbook

    # Generic investigation plan for unknown symptoms
    return f"""\
# Investigation Plan: {symptom}

App type: {app_type}

No specific playbook for this symptom. Follow the general approach:

## Step 1: Reproduce
```
start_debug(program="...", build_project="...")
configure_exceptions(filters=["all"])
```
{"Wait for window: ui_get_window_tree()" if app_type == "gui" else ""}

## Step 2: Trigger the issue
{"Interact with UI to reproduce: ui_click, ui_send_keys, etc." if app_type == "gui" else "Let the app run and reproduce the issue."}

## Step 3: When it stops (breakpoint or exception)
```
get_exception_info()      # if exception
get_call_stack()          # where
get_variables(ref=...)    # state
get_output_tail(lines=30) # recent output
```

## Step 4: Narrow down
- If you know the file: add_breakpoint(file="...", line=...)
- If you know the method: add_function_breakpoint(function_name="...")
- If you need to see UI: ui_take_annotated_screenshot()
- If build warnings matter: get_build_diagnostics()

## Step 5: Step through
```
step_over()   # follow the flow
step_into()   # enter suspicious functions
step_out()    # exit when you've seen enough
```
"""


def _build_scenario_plan(problem: str, app_type: str, file_hint: str) -> str:
    """Build a step-by-step debugging plan for a specific problem."""
    steps = [f"# Debug Plan: {problem}\n"]

    if app_type == "gui":
        steps.append("**App type: GUI (WPF/Avalonia)** — breakpoints AFTER window loads.\n")
    else:
        steps.append("**App type: Console** — breakpoints before or after launch.\n")

    steps.append("## Step 1: Start debug session")
    steps.append("```")
    steps.append('start_debug(program="bin/Debug/<framework>/App.dll", build_project="App.csproj", pre_build=True)')
    steps.append("```\n")

    if app_type == "gui":
        steps.append("## Step 2: Wait for window")
        steps.append("```")
        steps.append("ui_get_window_tree()            # confirm loaded")
        steps.append("ui_take_annotated_screenshot()   # see the UI")
        steps.append("```\n")

    steps.append(f"## Step {'3' if app_type == 'gui' else '2'}: Set breakpoints")
    if file_hint:
        steps.append(f"```\nadd_breakpoint(file=\"{file_hint}\", line=<suspected_line>)\n```\n")
    else:
        steps.append("```")
        steps.append("# If you know the method name:")
        steps.append('add_function_breakpoint(function_name="<MethodName>")')
        steps.append("# If you know the file and line:")
        steps.append('add_breakpoint(file="<file.cs>", line=<N>)')
        steps.append("```\n")

    if app_type == "gui":
        steps.append(f"## Step 4: Trigger the code path")
        steps.append("```")
        steps.append('ui_click(automation_id="<trigger_element>")')
        steps.append("# Or: ui_send_keys, ui_double_click, etc.")
        steps.append("```\n")
        next_step = 5
    else:
        steps.append("## Step 3: Run to breakpoint")
        steps.append("```\ncontinue_execution()\n```\n")
        next_step = 4

    steps.append(f"## Step {next_step}: Inspect state")
    steps.append("```")
    steps.append("get_call_stack()           # where are we?")
    steps.append("get_scopes(frame_id=...)   # scope references")
    steps.append("get_variables(ref=...)     # actual values")
    steps.append("get_output_tail(lines=20)  # recent log output")
    steps.append("```\n")

    steps.append(f"## Step {next_step + 1}: Iterate")
    steps.append("```")
    steps.append("step_over()          # follow the flow")
    steps.append("step_into()          # enter suspicious function")
    steps.append("continue_execution() # jump to next breakpoint")
    steps.append("```\n")

    steps.append(f"## Step {next_step + 2}: Clean up")
    steps.append("```")
    steps.append("stop_debug()")
    steps.append("```")

    return "\n".join(steps)

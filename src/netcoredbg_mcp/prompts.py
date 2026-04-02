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

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
  ^                      |                       |
  |                      v                       v
  +---stop_debug---  TERMINATED            (inspect, resume)
```

**IDLE** — No session. Only `start_debug` or `attach_debug`.
**RUNNING** — App executing. Old variable refs are INVALID. Do NOT call get_variables.
**STOPPED** — App FROZEN. UI won't paint. User cannot interact. Inspect then RESUME.
**TERMINATED** — App exited. Read output. Call stop_debug.

## Execution Tools Block Automatically

continue_execution, step_over, step_into, step_out all BLOCK until the program
stops again. You get the result (state, location, source context) in ONE call.
No polling needed. No loops. One call = one answer.

## The Inspect-Resume Cycle

When stopped at a breakpoint:
1. get_call_stack() — where are you? Response includes surrounding source lines.
2. get_scopes(frame_id) — get variable scope references
3. get_variables(reference) — read actual values
4. Decide: step deeper? continue? set more breakpoints?
5. RESUME — always resume. A frozen app = broken user experience.

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

## Process Management

cleanup_processes() — view/kill tracked processes. Never use taskkill.
restart_debug(rebuild=True) — rebuild + relaunch after code changes.

## Valid Actions by State

| State | You CAN do |
|-------|-----------|
| IDLE | start_debug, attach_debug |
| RUNNING | pause_execution, get_output*, get_debug_state, add_breakpoint, stop_debug |
| STOPPED | get_call_stack, get_variables, get_scopes, evaluate_expression, step_*, continue_execution, set_variable, ui_*, stop_debug |
| TERMINATED | get_output*, stop_debug, start_debug |

*get_output, get_output_tail, search_output work in all non-IDLE states.
"""

_DEBUG_GUI = """\
# Debugging WPF / Avalonia / WinForms Apps

GUI apps have a critical difference from console apps: the UI thread.
When the debugger pauses, the ENTIRE UI thread freezes. The window stops
painting, buttons stop responding, animations freeze mid-frame.

## The Golden Rule

**NEVER set breakpoints before the window is visible.**

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

When the app is RUNNING (not stopped at breakpoint), you can interact with it:

| Action | Tool |
|--------|------|
| Click button | ui_click(automation_id="btnSave") |
| Type text | ui_send_keys(keys="hello", automation_id="txtInput") |
| Right-click | ui_right_click(automation_id="dataGrid") |
| Double-click | ui_double_click(automation_id="listItem") |
| Select rows | ui_select_items(automation_id="grid", indices=[0,1,2]) |
| Scroll | ui_scroll(automation_id="list", direction="down", amount=5) |
| Drag | ui_drag(from_automation_id="src", to_automation_id="dst") |
| Complex keys | ui_set_focus(automation_id="grid") then ui_send_keys_focused(keys="^{END}") |

## "App Doesn't Respond" Checklist

1. get_debug_state() — is it STOPPED? If yes, you hit a breakpoint. Resume.
2. Is it RUNNING but UI frozen? Possible deadlock.
   pause_execution() → get_call_stack() for all threads → look for async deadlock.
3. Still unclear? ui_take_screenshot() — see what the user sees.
"""

_DEBUG_EXCEPTION = """\
## Exception Investigation Protocol

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
continue_execution()   # returns immediately (old behavior)
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
"""

# ═══════════════════════════════════════════════════════════════════════
# Parameterized prompt builders
# ═══════════════════════════════════════════════════════════════════════

# Exception-specific investigation knowledge base
_EXCEPTION_PLAYBOOKS: dict[str, str] = {
    "nullreferenceexception": """\
## NullReferenceException Investigation

This is the #1 .NET exception. Something is null that shouldn't be.

### Step 1: Find the null
```
get_exception_info()      # exception message may name the member
get_call_stack()          # find exact line
get_scopes(frame_id=...)
get_variables(ref=...)    # scan locals for null values
```

### Step 2: Trace the null backwards
Look at the null variable. How was it assigned?
```
# Set breakpoint at the assignment point
add_breakpoint(file="...", line=<assignment_line>)
restart_debug(rebuild=False)
# When hit, inspect what the source returns
```

### Step 3: Common causes
- Database query returned no results (FirstOrDefault → null)
- Dependency injection not registered (service is null)
- JSON deserialization missing property (model field is null)
- UI element not found (FindName returns null)
- Race condition: value set after null check but before use

### Step 4: Verify fix
```
set_variable(ref=..., name="suspect", value="new object()")
continue_execution()
# If it works → confirms the null was the issue
```
""",
    "invalidoperationexception": """\
## InvalidOperationException Investigation

Something was called at the wrong time or in the wrong state.

### Step 1: Read the message carefully
```
get_exception_info()
# The message usually tells you EXACTLY what's wrong:
# "Collection was modified; enumeration operation may not proceed"
# "Sequence contains no elements"
# "Cannot access a disposed object"
```

### Step 2: Inspect state
```
get_call_stack()
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

### Step 1: Find what was cancelled
```
get_exception_info()
get_call_stack()
# Look for: HttpClient calls, database queries, CancellationToken usage
```

### Step 2: Check cancellation source
```
get_variables(ref=...)
# Look for: CancellationToken.IsCancellationRequested
# Look for: HttpClient.Timeout value
# Look for: Task.Delay with cancellation
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

### Step 1: Identify the disposed object
```
get_exception_info()   # names the object type
get_call_stack()       # where it was used after disposal
```

### Step 2: Find where it was disposed
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
get_exception_info()      # what crashed
get_call_stack()          # where
get_variables(ref=...)    # state at crash
get_output_tail(lines=50) # last log messages before crash
```

### Step 4: If no exception caught
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
    steps.append('start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj", pre_build=True)')
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

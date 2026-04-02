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

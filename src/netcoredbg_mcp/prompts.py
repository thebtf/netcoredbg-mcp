"""MCP prompts for debug workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register MCP prompts (slash commands) on the server."""

    @mcp.prompt(
        name="debug",
        description="Debug session workflow guide for .NET applications",
    )
    def debug_prompt() -> list[dict]:
        """Start here when debugging .NET applications."""
        return [
            {
                "role": "user",
                "content": """# .NET Debug Session Guide

## CRITICAL RULES FOR AI DEBUGGER

### State Awareness
1. **PAUSED = FROZEN:** When state=stopped, the target program is COMPLETELY FROZEN.
   Its UI won't paint, it won't respond to input, it won't produce output.
   Do NOT wait for the program to "respond" or "finish loading" - it can't.
   Inspect state (get_call_stack, get_variables), then RESUME execution.

2. **RUNNING = REFS INVALID:** When state=running, variable references from the
   previous stop are INVALID. Do NOT call get_variables with old references.
   Wait for the program to stop again (execution tools block automatically).

3. **TERMINATED = DONE:** When state=terminated, the session is over.
   Read output for errors, then call stop_debug.

### Breakpoint Timing
4. **GUI APPS (WPF/WinForms/Avalonia):** NEVER set breakpoints before the window
   is visible. The app will freeze during initialization and the window will
   never appear.

   Correct workflow:
   ```
   start_debug(program="App.dll", pre_build=True, build_project="App.csproj")
   # If state is stopped/entry: continue_execution()
   # Wait for window: ui_get_window_tree()
   # NOW set breakpoints: add_breakpoint(file="ViewModel.cs", line=42)
   # Trigger the action in the UI: ui_click(automation_id="btnSave")
   # Execution tools block until breakpoint hit - inspect state
   ```

5. **CONSOLE APPS:** Breakpoints before launch are fine - they fire on startup code.

### Inspect-Resume Cycle
6. After hitting a breakpoint, ALWAYS follow this sequence:
   a. get_call_stack() - understand where execution stopped
   b. get_scopes(frame_id) - get variable scope references
   c. get_variables(variables_reference) - read local variable values
   d. Decide: step deeper, continue to next breakpoint, or stop?
   e. RESUME - do not leave the app paused indefinitely.

### Output Monitoring
7. Call get_output_tail() after every significant execution phase to catch
   runtime errors, assertion failures, and log messages.
   The user CANNOT see program output - YOU must read and summarize it.
   Never tell the user to "check the console" or "look at output".

### Exception Handling
8. When stopped with reason=exception:
   - Call get_exception_info() BEFORE resuming
   - Read the exception type, message, and stack trace
   - Decide: is this expected (resume) or a bug (investigate deeper)?

### Step Strategy
9. Use step_over for general flow (most common).
   Use step_into when you need to enter a called function.
   Use step_out to exit the current function and return to the caller.
   Prefer step_over unless the bug is inside a function at the current line.

### Function Breakpoints
10. When you know the method name but not the line number:
    Use add_function_breakpoint(function_name="OnButtonClick").
    This breaks when the named function is entered.

### Valid Actions by State

| State | Valid Actions |
|-------|-------------|
| IDLE | start_debug, attach_debug |
| RUNNING | pause_execution, get_output*, get_debug_state, stop_debug, add_breakpoint |
| STOPPED | get_call_stack, get_variables, get_scopes, evaluate_expression, step_*, continue_execution, add/remove_breakpoint, set_variable, stop_debug, ui_* |
| TERMINATED | get_output, stop_debug, start_debug (new session) |

*get_output, get_output_tail, search_output are valid in all non-IDLE states.

## Quick Start Workflows

### Debug a Console App
```
start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj")
add_breakpoint(file="Program.cs", line=15)
continue_execution()  # blocks until breakpoint hit
get_call_stack()
get_variables(scope_reference)
continue_execution()  # resume
stop_debug()
```

### Debug a WPF/Avalonia App
```
start_debug(program="bin/Debug/net8.0/App.dll", build_project="App.csproj")
# App is running - wait for window
ui_get_window_tree()  # verify window is visible
add_breakpoint(file="MainViewModel.cs", line=42)
ui_click(automation_id="btnAction")  # trigger the code path
# Execution stops at breakpoint - inspect
get_call_stack()
get_variables(scope_reference)
continue_execution()  # resume the app
stop_debug()
```

### Investigate an Exception
```
configure_exceptions(filters=["all"])  # break on ALL exceptions
continue_execution()  # run until exception
# Stopped with reason=exception
get_exception_info()
get_call_stack()
get_variables(scope_reference)  # inspect state at exception point
continue_execution()  # or stop_debug() if done
```

### Sending Keys to Complex Controls (DataGrid, TreeView, etc.)
```
ui_set_focus(automation_id="MyDataGrid")  # 1. Focus the element
ui_send_keys_focused(keys="^{END}")       # 2. Send keys without re-search
ui_send_keys_focused(keys="{DOWN}")       # 3. Continue sending keys
```

## Build Warnings
11. Build warnings are HIDDEN by default in start_debug/restart_debug responses.
    If debugging leads nowhere and the app behaves unexpectedly, call
    get_build_diagnostics() - a warning about nullable references, missing assemblies,
    or compatibility issues may explain the behavior.

## Process Management
12. Use cleanup_processes() to view or terminate tracked debug processes.
    Never use manual taskkill - the server tracks what it spawned.

## Common Issues
- Empty stack trace? Use start_debug, not attach_debug
- deps.json conflict? Build uses .dll, not .exe
- E_NOINTERFACE? dbgshim.dll version mismatch with .NET runtime
- App frozen at startup? Breakpoints set too early for GUI apps (see rule 4)
- Build OK but app crashes? Check get_build_diagnostics() for warnings
""",
            }
        ]

    @mcp.prompt(
        name="exception",
        description="Guide for investigating exceptions during debugging",
    )
    def exception_prompt() -> list[dict]:
        """Steps to investigate an exception."""
        return [
            {
                "role": "user",
                "content": "The debugger stopped on an exception.",
            },
            {
                "role": "assistant",
                "content": "I'll investigate the exception. Let me gather the details.",
            },
            {
                "role": "user",
                "content": """## Exception Investigation Steps

Execute these in order:

### 1. Get Exception Details
```
get_debug_state()  # Check exceptionInfo field
```

### 2. Get Stack Trace
```
get_call_stack()   # See where exception occurred
```

### 3. Inspect Local State
```
get_scopes(frame_id)           # Get scopes for the frame
get_variables(scope_reference)  # See local variables
```

### 4. Read Recent Output
```
get_output()  # Check for error messages before exception
```

### 5. Report to User
Summarize:
- Exception type and message
- Where it occurred (file, line, method)
- Likely cause based on local state
- Suggested fix

### 6. Decision
- `continue_execution()` to ignore and continue
- `stop_debug()` to end session and fix code
""",
            },
        ]

[English](README.md) | [Русский](README.ru.md)

# netcoredbg-mcp

[![PyPI](https://img.shields.io/pypi/v/netcoredbg-mcp?style=flat-square)](https://pypi.org/project/netcoredbg-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)](#requirements)
[![MCP](https://img.shields.io/badge/MCP-Server-6f42c1?style=flat-square)](https://modelcontextprotocol.io/)
[![Platform](https://img.shields.io/badge/Platform-Windows-2ea44f?style=flat-square)](#limitations)

Debug .NET applications from an MCP-capable coding agent without leaving the
agent workflow. `netcoredbg-mcp` combines `netcoredbg`, the Debug Adapter
Protocol, and Windows UI Automation so an agent can observe a running app,
stop it deliberately, and inspect the state that explains the behavior.

**Python 3.10+ · Windows GUI automation · 135 tools · 8 prompts · 4 resources · v0.23.13**

The v0.23.13 PATCH replaces PID-based cleanup with live process-owner cleanup.
The v0.23.12 fix for explicit paths outside the default project remains unchanged.

## What it enables

| Need | Use the MCP server to |
|---|---|
| Understand a failure | Launch or attach to a .NET process, set breakpoints, inspect threads, stacks, scopes, variables, modules, output, and exceptions. |
| Drive a desktop app | Find UI elements, read window trees, click, type, select, use the clipboard, and gather bounded WPF, WinForms, or Avalonia evidence. |
| Keep evidence honest | Capture a preview for navigation or opt in to a lossless screenshot artifact with integrity metadata. |
| Verify a repair | Run a bounded runtime-smoke plan with cleanup, output checkpoints, freshness checks, and recorded evidence. |
| Search a project | Find C# symbols and references, read source context, or run a bounded `search_source` query. |

The published Python package is the consumer entry point. The experimental .NET
host and Native Scene Probe are source-only and do not add tools to this wheel.

## Quick start

Install the package, let the setup wizard provision or discover the debugger,
then register the public CLI with your MCP client. The command below is for
Claude Code:

```powershell
pipx install netcoredbg-mcp
netcoredbg-mcp --setup
claude mcp add --scope user netcoredbg -- netcoredbg-mcp --project-from-cwd
```

Restart the MCP client after changing its configuration. From a .NET workspace,
ask the agent:

```text
Set a breakpoint in Program.cs, run the application, and show the local values when it stops.
```

`--project-from-cwd` searches upward from the server's startup directory for a
solution or .NET project. Use `--project` instead when the server must be pinned
to one explicit project root.

## Requirements

- Python 3.10 or later.
- `pipx` (recommended) or `pip` to install the package.
- A .NET SDK/runtime suitable for the application being debugged.
- `netcoredbg`. The setup wizard can download or discover it and scans compatible
  `dbgshim.dll` files.
- An MCP client, such as Claude Code, Cursor, Cline, Roo Code, Windsurf,
  Continue, or Claude Desktop.
- Windows for the GUI automation paths. Debugger functionality remains subject
  to the target runtime and `netcoredbg` capabilities.

## Install and configure

### Recommended installation

`pipx` keeps the command-line server isolated from project environments:

```powershell
pipx install netcoredbg-mcp
netcoredbg-mcp --setup
netcoredbg-mcp --version
```

The setup flow checks for a .NET SDK, provisions or finds `netcoredbg`, scans
`dbgshim` candidates, builds the FlaUI bridge on Windows when required, and
prints a client configuration snippet.

### Package-managed installation

Use `pip` when your environment owns Python packages directly:

```powershell
pip install --upgrade netcoredbg-mcp
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
netcoredbg-mcp --project C:\Work\MyDotNetApp
```

Run `netcoredbg-mcp --setup` after an upgrade if the target runtime changed or
you need a new managed debugger or FlaUI bridge.

### Client configuration

Use `--project-from-cwd` only when the client launches the server from the .NET
workspace or supplies local MCP roots. When no explicit `--project` or operator
environment pin is configured, local MCP roots take precedence. If there is
neither an operator pin nor a usable local root, the server searches its startup
directory for a solution, project, or Git marker and falls back to that startup
directory when no marker exists.

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project-from-cwd"]
    }
  }
}
```

For a client that starts servers from a stable global location, pin the target
project explicitly instead of relying on that server startup directory:

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project", "C:\\Work\\MyDotNetApp"]
    }
  }
}
```

If the debugger is managed outside the setup flow, set its path in the client
process environment rather than committing it to a repository. Use the same
project-selection mode that fits the client; this globally launched example
pins its target explicitly:

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project", "C:\\Work\\MyDotNetApp"],
      "env": {
        "NETCOREDBG_PATH": "C:\\Tools\\netcoredbg\\netcoredbg.exe"
      }
    }
  }
}
```

Keep `.mcp.json`, `.netcoredbg-mcp.launch.json`, credentials, and local project
paths out of source control.

### Run from a source checkout

The installed CLI is the consumer route. Use a source checkout only while
developing the server itself:

```powershell
uv sync --locked --project C:\Work\netcoredbg-mcp
cd C:\Work\MyDotNetApp
uv run --no-sync --project C:\Work\netcoredbg-mcp netcoredbg-mcp --project-from-cwd
```

`--no-sync` prevents a supervised server restart from changing the shared
virtual environment. Synchronize explicitly after changing dependencies or the
lockfile.

## First debugging session

`start_debug` launches the debug session and normally returns with it running.
`continue_execution`, `step_over`, `step_into`, and `step_out` are long-poll
operations: they return when the debuggee stops, exits, terminates, or reaches
their timeout.

For console programs, use this sequence:

1. Add a breakpoint in the code path of interest.
2. Call `start_debug` with the program and, when appropriate, `pre_build=true`.
3. Wait for `state=stopped`.
4. Read `get_call_stack`, `get_scopes`, and `get_variables`.
5. Evaluate or step only while stopped.
6. Continue or terminate the session.

For WPF, Avalonia, and WinForms targets, use the Desktop UI sequence below instead.
It starts the application without breakpoints, waits for the window to load, and
only then adds a breakpoint; a pre-launch breakpoint can make the window appear hung.

A representative launch request is:

```json
{
  "program": "bin/Debug/net8.0/MyApp.dll",
  "build_project": "MyApp.csproj",
  "pre_build": true,
  "stop_at_entry": false
}
```

For .NET 6+ targets, a built `.exe` is accepted when its matching `.dll` and
`.runtimeconfig.json` are present. Use
`inspect_debug_launch_compatibility(program)` before launch when you need to
inspect the selected runtime and shim without starting the process.

## Desktop UI and visual evidence

While a GUI debuggee is `RUNNING`, use UI tools to observe and operate it. Once
the UI thread is stopped at a breakpoint or pause, stack and variable inspection
become available but the window will not respond normally until you continue.

```text
start_debug(...)
ui_get_window_tree() # Wait for the application window to load.
add_breakpoint(file="MainWindow.xaml.cs", line=42)
ui_find_element(automation_id="saveButton")
ui_click(automation_id="saveButton")
# Trigger the breakpoint, then inspect state after it reports STOPPED.
```

### Physical numeric keypad input

On the default Python server's Windows FlaUI path,
`ui_send_keys(keys="{NUMPAD1}", automation_id="myInput")` sends the physical
keypad 1 key, not the text `"1"` or the top-row digit key.
`ui_send_keys_focused(keys="{NUMPADENTER}")` sends keypad Enter, distinct from `{ENTER}`.
`ui_send_keys_batch(keys=["{NUMPAD1}", "{NUMPADADD}", "{NUMPADENTER}"], automation_id="myInput")`
sends them in order;
`ui_key_sequence(keys=["NUMPAD1", "NUMPADENTER"], modifiers=[], automation_id="myInput")`
accepts names without braces.

Supported physical keys: `{NUMPAD0}`–`{NUMPAD9}`, `{NUMPADADD}`,
`{NUMPADSUBTRACT}`, `{NUMPADMULTIPLY}`, `{NUMPADDIVIDE}`, `{NUMPADDECIMAL}`,
`{NUMPADENTER}`, and `{NUMLOCK}` (17 keys). `{NUMLOCK}` presses and releases
the lock key; it does not set a chosen lock state. Physical-key delivery is
scoped to the Windows FlaUI backend, not the alternative UI backend or the
opt-in .NET preview.

### Screenshot modes

`ui_take_screenshot()` returns a WebP navigation preview with
`evidence_grade=preview_only`. It is useful for locating the next UI action,
not for asserting lossless visual evidence.

For an artifact that preserves the original raster and integrity metadata, opt
in explicitly:

```text
ui_take_screenshot(evidence=true)
```

Normally this mode returns `evidence_grade=lossless_raster`, persists a session-scoped
PrintWindow PNG, and includes SHA-256 and geometry provenance. For a strict physical
target, a probable-black PrintWindow raster may make one verified `BitBlt` attempt.
That response is explicitly `method=BitBlt`, `fallback=flash-focus`,
`fallback_reason=probable_black_printwindow`, and
`evidence_grade=typed_bitblt_fallback`; it records the `GetWindowDC` authority,
ROP, target PID, stable geometry/DPI, and foreground activation/restoration proof.
Any malformed, black, unstable, mismatched, or incompletely proven fallback persists
nothing. Any raw-derived crop requires `evidence=true`; preview-only captures do not
provide it.

Without an expected target, persisted evidence reports
`target_comparability.status=UNASSERTED`: it is valid lossless evidence, but
does not prove a resize target. Supply all three physical target fields to
compare the raw raster, not a derivative:

```text
ui_take_screenshot(evidence=true, expected_hwnd=..., expected_physical_width=..., expected_physical_height=...)
```

The response reports `MATCHED` or `MISMATCH`; only `MATCHED` persists raw
evidence. `max_width` affects only the preview and HD derivative, never this
comparison. `ui_resize_window()` reports request-versus-readback
`target_comparability.status` as `MATCHED`, `MISMATCH`, or `UNAVAILABLE`;
`resized=true` confirms request completion, not target equality.

Use `ui_take_annotated_screenshot()` to receive Set-of-Mark labels, then invoke
`ui_click_annotated(element_id=...)`. Use `ui_bring_to_front()` only when the
debuggee should intentionally leave stealth mode.

## Tool map

The published MCP catalog has 135 tools.

| Category | Count | Examples |
|---|---:|---|
| Debug control | 14 | `start_debug`, `attach_debug`, `continue_execution`, `pause_execution`, `terminate_debug` |
| Breakpoints and exceptions | 7 | file/function breakpoints and exception configuration |
| Inspection and DAP coverage | 15 | stacks, scopes, variables, modules, disassembly, source locations |
| Tracepoints | 6 | add, read, clear, and cursor trace evidence |
| Snapshots and object analysis | 5 | create, compare, list, and summarize captured state |
| Memory and output | 6 | memory, debugger output, and build diagnostics |
| Runtime smoke | 21 | hygiene, validation, execution, lifecycle, and cleanup evidence |
| UI automation | 55 | windows, elements, focus, input, screenshots, grids, and monitors |
| Code search | 4 | symbols, references, context, and regex search |
| Edit-and-Continue | 1 | `apply_code_change` |
| Process management | 1 | `cleanup_processes` |

The server also exposes four resources: `debug://state`, `debug://breakpoints`,
`debug://output`, and `debug://threads`.

Eight prompts provide guided workflows: `debug`, `debug-gui`,
`debug-exception`, `debug-visual`, `debug-mistakes`, `investigate`,
`debug-scenario`, and `dap-escape-hatch`.

### Process cleanup boundary

`cleanup_processes(force=True)` invokes the retained cleanup owners for debugger
adapters and the FlaUI bridge created by this server. It does not turn a numeric
PID, a DAP process event, or a saved record into permission to terminate a process.
Old PID files are left untouched and inert; startup no longer sweeps their PIDs.

Stopping or cleaning up an attached debug session detaches from the target rather
than terminating it. An explicit `terminate_debug` request remains a separate
action. Cleaning up the FlaUI bridge does not grant ownership of the UI app.
Cleanup failures remain incomplete results, not successful removal of records.

On Windows, cleanup uses retained process and Job handles. On POSIX, a private
guardian controls its own live process group, including ordinary descendants
that remain in that group. This is not a guarantee for daemonized or group-escaped
descendants. In the force-cleanup response, `terminated` counts only confirmed
forced terminations of owned roots caused by cleanup. It excludes natural or
graceful exits and is not a count of callbacks, all stopped processes, or the
whole-tree population.
On POSIX, `terminated` remains `0` because the cleanup result does not establish
the cause of the root's exit. This excludes unconfirmed causality; it does not
mean that no processes were stopped. The whole-tree count remains unknown.
`tree_terminated` is `null` because no whole-tree count is supplied. `complete`,
`remaining_owners`, and `errors` report completion and unfinished cleanup; a
failure returns an error with the same cleanup data. An empty observation
registry alone does not prove that an operating-system tree drained.

### Code search boundary

`find_code_symbol`, `find_code_references`, and `get_source_context` execute
in the MCP server process. For `search_source`, source-file enumeration and the
synchronous wait remain in that process; per-file source reading/scanning and
regex matching run in a bounded dedicated Python subprocess, with a default
five-second timeout and a maximum of 1,000 results. It honors only the project
root `.gitignore`; nested ignore files are not consulted.

## Runtime-smoke verification

Use runtime-smoke tools when you need a bounded, replayable verification rather
than an ad hoc debugging conversation. Start with
`debug_hygiene_preflight`, create an output checkpoint, run a validated plan,
and close the run with its cleanup contract. `verify_debug_freshness` can prove
that the live process still matches the expected workspace and artifacts.

For long-lived orchestration, use the lifecycle family:
`runtime_smoke_start`, `runtime_smoke_tail_events`,
`runtime_smoke_get_result`, and `runtime_smoke_stop`. See the
[production testing playbook](docs/PRODUCTION-TESTING-PLAYBOOK.md) for the
consumer-mode release gate and the examples in [`docs/examples/`](docs/examples/)
for WPF workflow, WPF DataGrid drag/drop, and diagnostic-plan shapes.

### Input provenance

Runtime-smoke plans can distinguish the runner's own input from operator or
foreign input. For an operator-free product verdict, set both
`input_policy.no_global_input=true` and `run_confidence.no_operator=true`.
The first setting prevents runner-controlled global input; the second requires
input-monitor confidence evidence for the action window.

The resulting `run_confidence` classification is `CLEAN_PROVEN` when the
monitor proves no operator input, `DIRTY_UNPROVEN` when it observes physical or
foreign input or receives malformed/unattributable input evidence, or `UNPROVEN`
when monitor evidence is unavailable or incomplete. Only `CLEAN_PROVEN` permits a
product verdict.

When a plan permits runner-controlled global input, such as `ui.drag`, set
`input_policy.no_global_input=false` and retain `run_confidence.no_operator=true`
when a product verdict needs confidence evidence. Every covered input event must
carry `runner_injected` provenance. A `foreign_injected` or `physical` event
yields `DIRTY_UNPROVEN`; the caller must not treat that run as a product verdict.

## Command-line reference

| Command or option | Purpose |
|---|---|
| `netcoredbg-mcp --version` | Print the installed package version. |
| `netcoredbg-mcp --setup` | Provision or discover debugger prerequisites, then print a client configuration snippet. |
| `netcoredbg-mcp setup --enc` | Install the default prebuilt Edit-and-Continue debugger with `ncdbhook.dll` on Windows x64; a source build is opt-in. |
| `netcoredbg-mcp --project C:\Work\MyApp` | Set the default project root for path resolution and source search. |
| `netcoredbg-mcp --project-from-cwd` | Resolve the project from the startup directory and compatible local MCP roots. |

`--project` and `--project-from-cwd` are mutually exclusive. `--enc` must be
used with `setup` or `--setup`.

## Configuration reference

| Variable | Purpose |
|---|---|
| `NETCOREDBG_PATH` | Explicit path to `netcoredbg`. |
| `NETCOREDBG_PROJECT_ROOT` / `MCP_PROJECT_ROOT` | Authoritative project-root fallback. |
| `FLAUI_BRIDGE_PATH` | Explicit FlaUI bridge executable path. |
| `NETCOREDBG_SCREENSHOT_MAX_WIDTH` / `NETCOREDBG_SCREENSHOT_QUALITY` | Inline preview dimensions and WebP quality. |
| `NETCOREDBG_SESSION_TIMEOUT` | Multi-agent ownership inactivity timeout. |
| `LOG_LEVEL` / `LOG_FILE` | Server diagnostic logging controls. |

An explicit `--project` or project-root environment variable takes precedence
over MCP client roots. Network/UNC client roots are rejected.

Explicit paths to existing DLL/EXE targets, source files, and build projects,
plus output paths, need no separate debug permission, even outside the default
project or worktree. Project-root selection provides context, not a directory
allowlist. This removes MCP-wrapper policy, not a native Samsung `netcoredbg`
operating-system sandbox. File and .NET target validity checks remain, as do
the independent evidence, restore, and source-search guards.

## Architecture

```mermaid
graph TB
    Client[MCP client] --> Server[netcoredbg-mcp stdio server]
    Server --> Tools[Debug, inspection, UI, smoke, and search tools]
    Tools --> Session[Session manager and process registry]
    Session --> DAP[DAP client]
    DAP --> Debugger[netcoredbg]
    Debugger --> App[.NET debuggee]
    Tools --> UI[Windows UI automation bridge]
```

The public console script starts a FastMCP stdio server. Its tool modules share
one session manager, which owns debugger state, default project context,
process cleanup, output, snapshots, and trace evidence. The DAP client talks to
`netcoredbg`; Windows UI operations use the FlaUI bridge when available, with a
pywinauto fallback for supported operations.

## Troubleshooting

### `netcoredbg` is not found

**Symptom:** startup or `start_debug` reports that the debugger cannot be found.

**Cause:** setup did not install a managed debugger and `NETCOREDBG_PATH` is not
set.

**Fix:** run `netcoredbg-mcp --setup`, or set `NETCOREDBG_PATH` to the full
`netcoredbg.exe` path in the MCP client environment.

**Verify:** run `netcoredbg-mcp --setup` again and confirm its output reports a
found or provisioned debugger. Then confirm that the MCP client can list the
server tools.

### A breakpoint remains unverified

**Symptom:** the process does not stop at the requested source line.

**Cause:** common causes include stale build output, a wrong target DLL,
optimized Release binaries, or a line without executable IL.

**Fix:** use `pre_build=true`, debug a Debug build, verify that source and
assembly match, and inspect `list_breakpoints()` for DAP-adjusted locations.

**Verify:** the response reports `verified=true` or gives the adjusted line.

### A GUI appears frozen

**Symptom:** a WPF, WinForms, or Avalonia window stops repainting after a debug
command.

**Cause:** its UI thread is stopped at a breakpoint or pause.

**Fix:** inspect state while stopped, then call `continue_execution()` before
expecting the window to accept UI input.

**Verify:** `get_debug_state()` reports `running` and fresh screenshots update.

## Limitations

- GUI automation is Windows-focused.
- `netcoredbg` and DAP behavior depends on the target runtime and debugger
  support.
- Memory tools require valid adapter-supported memory references.
- Native debugging, browser automation, and non-.NET runtimes are out of scope.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, test expectations,
sensitive-data rules, and pull-request requirements.

## License

MIT. See [LICENSE](LICENSE).

[English](README.md) | [Русский](README.ru.md)

# netcoredbg-mcp

[![PyPI](https://img.shields.io/pypi/v/netcoredbg-mcp?style=flat-square)](https://pypi.org/project/netcoredbg-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)](#requirements)
[![MCP](https://img.shields.io/badge/MCP-Server-6f42c1?style=flat-square)](https://modelcontextprotocol.io/)
[![Platform](https://img.shields.io/badge/Platform-Windows-2ea44f?style=flat-square)](#limitations)

`netcoredbg-mcp` gives AI coding agents a real debugger for .NET applications.
Through the Model Context Protocol, an agent can launch or attach to a process,
set breakpoints, step through code, inspect variables, evaluate expressions, read
debug output, and operate WPF/WinForms windows without opening an IDE.

**89 MCP tools · 8 prompts · 4 resources · 728 collected tests · release target v0.12.0**

## Quick Links

- **Start:** [Quick Start](#quick-start) · [Installation](#installation) · [Client Setup](#client-setup)
- **Use:** [First Debug Session](#first-debug-session) · [GUI App Debugging](#gui-app-debugging) · [Visual Inspection](#visual-inspection)
- **Reference:** [Available Tools](#available-tools) · [Resources](#mcp-resources) · [Prompts](#mcp-prompts) · [Architecture](#architecture-overview)
- **Project:** [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [License](LICENSE)

## What's New in v0.12.0

- **Launch environment profiles** — `start_debug` can load project-local
  `.netcoredbg-mcp.launch.json` profiles, merge inherited variables, and redact
  sensitive values from responses and logs.
- **DAP coverage expansion** — typed wrappers now cover progress, memory,
  loaded sources, disassembly, locations, and previously unhandled DAP events.
- **Memory inspection** — `read_memory` and `write_memory` expose DAP memory
  references when the debug adapter and target support them.
- **Escape hatch prompt** — `dap-escape-hatch` documents lower-level DAP
  commands for advanced cases before a dedicated MCP tool exists.
- **Documentation and sensitive-data cleanup** — tracked docs no longer use
  downstream project names or local private paths as examples.

## Highlights

| Capability | What agents can do |
|---|---|
| Debug control | Launch, attach, restart, continue, pause, terminate, and step through .NET code |
| Breakpoints | File, function, conditional, hit-count, exception, and tracepoint workflows |
| Inspection | Threads, stack frames, scopes, variables, modules, expressions, source, disassembly, memory |
| GUI automation | Window trees, element search, clicks, keystrokes, screenshots, annotations, clipboard, window management |
| Build integration | Pre-launch `dotnet build`, progress notifications, build diagnostics, cleanup of locked debug processes |
| Multi-agent safety | Session ownership through `mcp-mux`, read-only observers, inactivity release |

## Quick Start

```powershell
# 1. Install the MCP server
pipx install netcoredbg-mcp

# 2. Run first-time setup
netcoredbg-mcp --setup

# 3. Register it in Claude Code
claude mcp add netcoredbg -- netcoredbg-mcp --project-from-cwd
```

Then ask your agent:

```text
Set a breakpoint in Program.cs, run the app, and inspect local variables when it stops.
```

## Critical Notes

> [!IMPORTANT]
> For .NET Core debugging, the `dbgshim.dll` next to `netcoredbg.exe` must match
> the target runtime major version. The setup wizard scans installed runtimes and
> prepares compatible dbgshim copies.

> [!IMPORTANT]
> `start_debug` is a long-poll tool. If the debuggee is a GUI app, it may return
> only after the app stops at a breakpoint, exits, or times out. Use screenshots
> and UI tools while the app is running; use variable inspection only when stopped.

> [!CAUTION]
> Do not commit `.mcp.json`, `.netcoredbg-mcp.launch.json`, credentials, server
> inventory, or local downstream project paths. Launch profiles support
> `inherit` so secrets can stay in the MCP server process environment.

## Installation

### Requirements

- Windows for GUI automation and FlaUI/pywinauto workflows.
- Python 3.10 or newer.
- .NET SDK/runtime for the target application.
- `netcoredbg`; use `netcoredbg-mcp --setup` unless you need a custom install.
- An MCP client such as Claude Code, Cursor, Cline, Roo Code, Windsurf, Continue,
  or Claude Desktop.

### Recommended Install

```powershell
pipx install netcoredbg-mcp
netcoredbg-mcp --setup
netcoredbg-mcp --version
```

The setup wizard downloads or discovers `netcoredbg`, scans dbgshim versions,
builds the FlaUI bridge when needed, and prints a ready-to-use MCP configuration
snippet.

### Manual Install

```powershell
pip install netcoredbg-mcp
$env:NETCOREDBG_PATH = "C:\Tools\netcoredbg\netcoredbg.exe"
netcoredbg-mcp --project-from-cwd
```

Use a manual install when you pin a locally managed `netcoredbg` build or when a
corporate environment blocks automatic downloads.

### Upgrading

```powershell
pipx upgrade netcoredbg-mcp
netcoredbg-mcp --setup
```

Run setup after an upgrade when the target .NET runtime changed, when you need a
new FlaUI bridge build, or when MCP client snippets should be regenerated.

## Configuration

### Project Launch Profiles

`start_debug` can read `.netcoredbg-mcp.launch.json` from the resolved project
root and apply profile environment variables to the debuggee process. The build
process environment is not changed.

```json
{
  "defaultProfile": "default",
  "profiles": {
    "default": {
      "env": {
        "DOTNET_ENVIRONMENT": "Development",
        "APP_MODE": "Debug"
      },
      "inherit": ["PATH"]
    }
  }
}
```

Precedence is deterministic:

1. `inherit` copies only explicitly named variables from the MCP server process.
2. Profile `env` values override inherited values.
3. Direct `start_debug(env={...})` values override the profile.

`env` values set to `null` are passed through to DAP as explicit nulls to
request variable removal or unset semantics where the adapter supports it. Tool
responses include only variable names, counts, profile name, source path, and
redacted metadata; they never echo environment values.

The repository `.gitignore` excludes `.netcoredbg-mcp.launch.json` by default.
Commit a profile only when it contains non-secret, shareable values.

### Base Server Configuration

Use `--project-from-cwd` for CLI-based agents that start the server from the
workspace. Use `--project` when the MCP client starts from a stable global
location and you want to constrain all debug paths explicitly.

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project-from-cwd"]
    }
  }
}
```

If setup did not install a managed `netcoredbg`, add `NETCOREDBG_PATH`:

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project-from-cwd"],
      "env": {
        "NETCOREDBG_PATH": "C:\\Tools\\netcoredbg\\netcoredbg.exe"
      }
    }
  }
}
```

### Project-Scoped Config

Use a project-local MCP config when a client supports it. Keep machine-specific
secrets and binary paths outside git.

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project", "C:\\Work\\MyDotNetApp"]
    }
  }
}
```

## Client Setup

### Claude Code

```powershell
claude mcp add netcoredbg -- netcoredbg-mcp --project-from-cwd
```

### Cursor, Cline, Roo Code, Windsurf, Continue, Claude Desktop

Add the same server shape to the client-specific MCP configuration file:

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project-from-cwd"]
    }
  }
}
```

Common configuration locations:

| Client | Typical config path |
|---|---|
| Cursor | `%USERPROFILE%\.cursor\mcp.json` |
| Cline | VS Code extension MCP settings |
| Roo Code | `%USERPROFILE%\.roo\mcp.json` or project `.roo\mcp.json` |
| Windsurf | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |
| Continue | `%USERPROFILE%\.continue\config.json` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |

## First Debug Session

### The Long-Poll Pattern

Execution tools wait for a meaningful debugger event. `start_debug`,
`continue_execution`, `step_over`, `step_into`, and `step_out` return when the
debuggee stops, exits, terminates, or times out.

### Typical Workflow

```text
1. Add a breakpoint in the code path you want to inspect.
2. Start debugging with pre_build=true.
3. Wait for state=stopped.
4. Read call stack, scopes, and variables.
5. Evaluate focused expressions or step to the next line.
6. Continue or terminate the session.
```

### Pre-Build Launch Example

```json
{
  "program": "bin/Debug/net8.0/MyApp.dll",
  "build_project": "MyApp.csproj",
  "pre_build": true,
  "stop_at_entry": false
}
```

For .NET 6+ applications, passing a built `.exe` is accepted when a matching
`.dll` and `.runtimeconfig.json` exist. The server resolves the DLL target to
avoid `deps.json` conflicts.

## GUI App Debugging

### The Rule

Do not use debugger inspection tools while a GUI app is running normally. First
stop at a breakpoint or pause the process; otherwise stack, scopes, and variables
are unavailable by design.

### GUI Workflow

```text
1. Launch the app.
2. Use ui_take_screenshot or ui_get_window_tree to observe the running UI.
3. Use UI tools to click, type, select, or wait for state changes.
4. Set a breakpoint before the code path you need to inspect.
5. Trigger the UI action.
6. When state=stopped, inspect variables and call stack.
```

### Visual Inspection

Screenshots return MCP image content, so vision-capable agents can inspect
layout and state. Annotated screenshots add Set-of-Mark labels for elements that
can be clicked with `ui_click_annotated`.

```text
ui_take_screenshot()
ui_take_annotated_screenshot()
ui_click_annotated(element_id=3)
```

## Available Tools

| Category | Count | Tools |
|---|---:|---|
| Debug control | 12 | `start_debug`, `attach_debug`, `stop_debug`, `restart_debug`, `continue_execution`, `pause_execution`, `step_over`, `get_step_in_targets`, `step_into`, `step_out`, `get_debug_state`, `terminate_debug` |
| Breakpoints and exceptions | 6 | `add_breakpoint`, `remove_breakpoint`, `list_breakpoints`, `clear_breakpoints`, `add_function_breakpoint`, `configure_exceptions` |
| Inspection and DAP coverage | 15 | `get_threads`, `get_call_stack`, `get_scopes`, `get_variables`, `evaluate_expression`, `set_variable`, `get_exception_info`, `get_modules`, `get_progress`, `get_loaded_sources`, `disassemble`, `get_locations`, `quick_evaluate`, `get_exception_context`, `get_stop_context` |
| Tracepoints | 4 | `add_tracepoint`, `remove_tracepoint`, `get_trace_log`, `clear_trace_log` |
| Snapshots and object analysis | 5 | `create_snapshot`, `diff_snapshots`, `list_snapshots`, `analyze_collection`, `summarize_object` |
| Memory | 2 | `read_memory`, `write_memory` |
| Output and build diagnostics | 4 | `get_output`, `search_output`, `get_output_tail`, `get_build_diagnostics` |
| UI automation | 40 | Window tree, element search, focus, keyboard, mouse, screenshots, annotations, selection, clipboard, window management, expand/collapse, value setting, virtualization |
| Process management | 1 | `cleanup_processes` |

## MCP Resources

| URI | Contents |
|---|---|
| `debug://state` | Current debug session state |
| `debug://breakpoints` | Active breakpoints and verification state |
| `debug://output` | Buffered debuggee and build output |
| `debug://threads` | Current thread list |

## MCP Prompts

| Prompt | Use it for |
|---|---|
| `debug` | General debugging workflow guidance |
| `debug-gui` | WPF/WinForms debugging and UI automation |
| `debug-exception` | Exception-first investigation |
| `debug-visual` | Screenshot and Set-of-Mark workflows |
| `debug-mistakes` | Common agent debugging mistakes and recovery |
| `investigate` | Parameterized symptom investigation |
| `debug-scenario` | Scenario-specific debugging plans |
| `dap-escape-hatch` | Advanced DAP commands without first-class MCP wrappers |

## Multi-Agent Safety

When served through `mcp-mux`, mutating debug tools are session-owned. One agent
can control a live debug session while other agents keep read-only observability
through state, output, screenshot, and inspection tools. Ownership auto-releases
after the configured inactivity timeout.

## Architecture Overview

```mermaid
graph TB
    subgraph MCP["MCP Server"]
        MAIN["__main__.py"]
        SERVER["server.py"]
        PROMPTS["prompts.py"]
        TOOLS["tools/*"]
        SESSION["session/*"]
        BUILD["build/*"]
        UI["ui/*"]
        SETUP["setup/*"]
    end

    subgraph DAP["Debug Adapter Protocol"]
        CLIENT["dap/client.py"]
        PROTOCOL["dap/protocol.py"]
        EVENTS["dap/events.py"]
    end

    MAIN --> SERVER
    SERVER --> PROMPTS
    SERVER --> TOOLS
    TOOLS --> SESSION
    TOOLS --> BUILD
    TOOLS --> UI
    SESSION --> CLIENT
    CLIENT --> PROTOCOL
    CLIENT --> EVENTS
    CLIENT <-->|stdio JSON-RPC| NETCOREDBG["netcoredbg"]
    NETCOREDBG --> APP[".NET debuggee"]
    SETUP --> NETCOREDBG
```

### How It Works

1. `__main__.py` parses CLI flags, configures project-root policy, and starts the
   FastMCP stdio server.
2. `server.py` registers tools, prompts, resources, progress notifications, and
   session ownership checks.
3. Tool modules keep debugger control, breakpoints, inspection, memory, output,
   process cleanup, and UI automation separate.
4. `SessionManager` owns debugger state, path validation, event handling,
   snapshots, tracepoints, output buffers, and process cleanup.
5. `DAPClient` speaks JSON-RPC over stdio to `netcoredbg`.
6. UI automation uses a FlaUI bridge on Windows, with pywinauto fallback where
   supported.

## Command Line Options

```text
netcoredbg-mcp --help
netcoredbg-mcp --version
netcoredbg-mcp --setup
netcoredbg-mcp --project C:\Work\MyApp
netcoredbg-mcp --project-from-cwd
```

| Option | Purpose |
|---|---|
| `--version` | Print the package version |
| `--setup` | Run first-time setup and exit |
| `--project PATH` | Constrain debug operations to a specific project root |
| `--project-from-cwd` | Detect the project root from the process working directory and MCP roots |

`--project` and `--project-from-cwd` are mutually exclusive.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `NETCOREDBG_PATH` | auto-discovered after setup | Explicit path to `netcoredbg` |
| `NETCOREDBG_PROJECT_ROOT` | unset | Project root fallback |
| `MCP_PROJECT_ROOT` | unset | Generic MCP project root fallback |
| `NETCOREDBG_ALLOWED_PATHS` | empty | Additional comma-separated allowed path prefixes |
| `NETCOREDBG_SCREENSHOT_MAX_WIDTH` | `1568` | Max inline screenshot width |
| `NETCOREDBG_SCREENSHOT_QUALITY` | `80` | Screenshot compression quality |
| `NETCOREDBG_MAX_TRACE_ENTRIES` | `1000` | Tracepoint log capacity |
| `NETCOREDBG_EVALUATE_TIMEOUT` | `0.5` | Tracepoint expression timeout in seconds |
| `NETCOREDBG_RATE_LIMIT_INTERVAL` | `0.1` | Tracepoint hit rate-limit interval |
| `NETCOREDBG_MAX_SNAPSHOTS` | `20` | Snapshot capacity |
| `NETCOREDBG_MAX_VARS_PER_SNAPSHOT` | `200` | Variables captured per snapshot |
| `NETCOREDBG_MAX_OUTPUT_BYTES` | `10000000` | Total output buffer cap |
| `NETCOREDBG_MAX_OUTPUT_ENTRY` | `100000` | Single output entry cap |
| `NETCOREDBG_SESSION_TIMEOUT` | `60.0` | Multi-agent ownership inactivity timeout |
| `NETCOREDBG_STACKTRACE_DELAY_MS` | `0` | Diagnostic delay before stackTrace requests |
| `FLAUI_BRIDGE_PATH` | auto-discovered | Explicit FlaUI bridge executable path |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `LOG_FILE` | unset | Optional diagnostic log file |

## Troubleshooting

### `netcoredbg` is not found

**Symptom:** startup or `start_debug` reports that `netcoredbg` cannot be found.

**Cause:** setup has not installed a managed debugger and `NETCOREDBG_PATH` is
not set.

**Fix:** run `netcoredbg-mcp --setup`, or set `NETCOREDBG_PATH` to the explicit
`netcoredbg.exe` path.

**Verify:** `netcoredbg-mcp --version` succeeds and your MCP client can list the
server tools.

### Breakpoints do not bind

**Symptom:** a breakpoint stays unverified or the process never stops where
expected.

**Cause:** stale build output, wrong target DLL, optimized Release binaries, or a
line without executable IL.

**Fix:** run with `pre_build=True`, debug the `Debug` configuration, confirm the
source file matches the built assembly, and inspect `list_breakpoints()` for
DAP-adjusted lines.

**Verify:** the breakpoint response reports `verified=true` or includes the DAP
line adjustment.

### GUI appears frozen

**Symptom:** a WPF or WinForms window stops repainting after a debug command.

**Cause:** the debugger stopped the UI thread at a breakpoint or pause.

**Fix:** inspect variables while stopped, then call `continue_execution()` before
expecting the GUI to respond to clicks or keystrokes.

**Verify:** `get_debug_state()` reports `running` and screenshots update again.

### Path is rejected in a worktree

**Symptom:** launch or build fails with a path validation error.

**Cause:** the project root was resolved to a different checkout, or the worktree
path is outside the allowed root set.

**Fix:** use `--project-from-cwd` from the active worktree, or add the worktree
prefix to `NETCOREDBG_ALLOWED_PATHS`.

**Verify:** `start_debug` accepts build and program paths under the worktree.

## Limitations

- GUI automation is Windows-focused.
- `netcoredbg` and DAP capabilities vary by runtime and target application.
- Memory reads and writes require debug adapter support and valid memory
  references.
- Native debugging, browser automation, and non-.NET runtimes are out of scope.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, PR expectations, and
sensitive-data rules.

## License

MIT. See [LICENSE](LICENSE).

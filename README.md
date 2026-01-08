# netcoredbg-mcp

MCP (Model Context Protocol) server for debugging C#/.NET applications using [netcoredbg](https://github.com/Samsung/netcoredbg).

**Enables AI agents like Claude to debug .NET applications autonomously** — set breakpoints, inspect variables, step through code, and analyze runtime state without requiring VS Code or any IDE.

## Features

- **Standalone debugging** — No VS Code required, works directly from terminal/Claude Code
- **Full DAP support** — Complete Debug Adapter Protocol implementation via netcoredbg
- **Smart program resolution** — Automatically handles .NET 6+ .exe/.dll conflicts
- **Pre-build integration** — Build before debug with `pre_build: true`
- **Version compatibility checks** — Auto-detects dbgshim.dll version mismatches
- **Breakpoint management** — Add, remove, list, clear breakpoints with conditions
- **Execution control** — Start, stop, continue, step over/into/out, pause
- **State inspection** — Variables, call stack, threads, expression evaluation
- **Exception handling** — Get exception info when stopped on exception

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP Server (Python)                          │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  MCP Tools   │  │  DAP Client  │  │   Session Manager      │ │
│  │  (20 tools)  │←→│  (protocol)  │←→│   (state + validation) │ │
│  └──────────────┘  └──────┬───────┘  └────────────────────────┘ │
│                           │                                      │
│  ┌──────────────┐         │          ┌────────────────────────┐ │
│  │ Version      │         │          │   Build Manager        │ │
│  │ Checker      │         │          │   (pre_build support)  │ │
│  └──────────────┘         │          └────────────────────────┘ │
└───────────────────────────┼─────────────────────────────────────┘
                            │ stdio (JSON-RPC)
                     ┌──────▼──────┐
                     │ netcoredbg  │
                     │ (DAP Server)│
                     └─────────────┘
```

## ⚠️ Critical: dbgshim.dll Version Compatibility

**The `dbgshim.dll` in your netcoredbg folder MUST match the major version of the .NET runtime you're debugging.**

This is an **undocumented Microsoft requirement** discovered through extensive debugging. Using a mismatched version causes:
- `E_NOINTERFACE (0x80004002)` errors
- Empty call stacks
- Failed variable inspection

| Target Runtime | Required dbgshim.dll Location |
|----------------|------------------------------|
| .NET 6.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.x\dbgshim.dll` |
| .NET 7.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\7.0.x\dbgshim.dll` |
| .NET 8.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\8.0.x\dbgshim.dll` |
| .NET 9.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\9.0.x\dbgshim.dll` |

```powershell
# Example: Setup for .NET 6 debugging
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll" "D:\Bin\netcoredbg\"
```

**Auto-detection:** This MCP server automatically detects mismatches and warns you during `start_debug`.

## Installation

```bash
# Clone the repository
git clone https://github.com/thebtf/netcoredbg-mcp.git
cd netcoredbg-mcp

# Install with uv (recommended)
uv sync

# Or install with pip
pip install -e .
```

### Download netcoredbg

Download from [Samsung/netcoredbg releases](https://github.com/Samsung/netcoredbg/releases) and extract to a folder (e.g., `D:\Bin\netcoredbg`).

## Configuration

### Claude Code (Recommended)

```bash
# Add to Claude Code with automatic project detection
claude mcp add --scope user netcoredbg -- \
  uv --directory "/path/to/netcoredbg-mcp" run netcoredbg-mcp --project-from-cwd
```

Set `NETCOREDBG_PATH` in your shell profile:

```powershell
# PowerShell profile (~\Documents\PowerShell\Microsoft.PowerShell_profile.ps1)
$env:NETCOREDBG_PATH = "D:\Bin\netcoredbg\netcoredbg.exe"
```

### JSON Configuration (Claude Desktop, Cursor, etc.)

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/netcoredbg-mcp",
        "run",
        "netcoredbg-mcp",
        "--project-from-cwd"
      ],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

### Project-Scoped Configuration (`.mcp.json`)

Add to your .NET project root for automatic loading:

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": ["--directory", "/path/to/netcoredbg-mcp", "run", "netcoredbg-mcp"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe",
        "NETCOREDBG_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

## Available Tools

### Debug Control

| Tool | Description |
|------|-------------|
| `start_debug` | **Recommended.** Launch program with full debug support. Supports `pre_build` option. |
| `attach_debug` | Attach to running process (⚠️ limited — see below) |
| `stop_debug` | Stop the debug session |
| `continue_execution` | Continue program execution |
| `pause_execution` | Pause program execution |
| `step_over` | Step over to next line |
| `step_into` | Step into function call |
| `step_out` | Step out of current function |
| `get_debug_state` | Get current session state |

### start_debug Parameters

```python
start_debug(
    program="/path/to/App.exe",      # or App.dll — auto-resolved for .NET 6+
    cwd="/path/to/working/dir",      # optional working directory
    args=["--arg1", "value"],        # optional command line args
    env={"KEY": "value"},            # optional environment variables
    stop_at_entry=False,             # stop at program entry point
    pre_build=True,                  # build before launching
    build_project="/path/to/App.csproj",  # required if pre_build=True
    build_configuration="Debug"      # Debug or Release
)
```

### Smart .exe → .dll Resolution

For .NET 6+ applications (WPF, WinForms, Console), the SDK creates:
- `App.exe` — Native host launcher
- `App.dll` — Actual managed code

Debugging `.exe` causes a "deps.json conflict" error. This MCP server **automatically resolves `.exe` to `.dll`** when:
1. A matching `.dll` exists in the same directory
2. A `.runtimeconfig.json` file exists (indicates .NET 6+ SDK-style project)

You can pass either `App.exe` or `App.dll` — the correct target is selected automatically.

### ⚠️ Attach Mode Limitations

`attach_debug` has **significant limitations** due to an upstream netcoredbg restriction:

- **`justMyCode` is NOT supported in attach mode** — this is a netcoredbg limitation
- Stack traces may be **incomplete or empty**
- Variable inspection may not work reliably

**Always prefer `start_debug`** which has full functionality.

### Breakpoints

| Tool | Description |
|------|-------------|
| `add_breakpoint` | Add breakpoint with optional condition and hit count |
| `remove_breakpoint` | Remove a breakpoint by ID |
| `list_breakpoints` | List all active breakpoints |
| `clear_breakpoints` | Clear all breakpoints (optionally by file) |

### Inspection

| Tool | Description |
|------|-------------|
| `get_threads` | Get all threads with their states |
| `get_call_stack` | Get call stack for a thread |
| `get_scopes` | Get variable scopes (locals, arguments, etc.) for a frame |
| `get_variables` | Get variables in a scope with types and values |
| `evaluate_expression` | Evaluate an expression in the current context |
| `get_exception_info` | Get exception details when stopped on exception |
| `get_output` | Get debug console output |

## MCP Resources

| Resource URI | Description |
|--------------|-------------|
| `debug://state` | Current session state (idle/running/stopped) |
| `debug://breakpoints` | All active breakpoints |
| `debug://output` | Debug console output buffer |
| `debug://threads` | Current threads |

## Command Line Options

| Option | Description |
|--------|-------------|
| `--project PATH` | Explicit project root path. All debug operations constrained to this path. |
| `--project-from-cwd` | Auto-detect project from CWD by searching for `.sln`, `.csproj`, or `.git`. |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NETCOREDBG_PATH` | **Required.** Path to netcoredbg executable |
| `NETCOREDBG_PROJECT_ROOT` | Project root path (alternative to `--project`) |
| `MCP_PROJECT_ROOT` | Fallback project root |
| `LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `NETCOREDBG_STACKTRACE_DELAY_MS` | Diagnostic delay before stackTrace requests |

## Troubleshooting

### Empty call stack / E_NOINTERFACE (0x80004002)

**Symptom:** `get_call_stack` returns empty array or error containing `0x80004002`.

**Cause:** `dbgshim.dll` version mismatch between netcoredbg and target runtime.

**Solution:**
1. Check the warning from `start_debug` — it tells you the exact versions
2. Copy the correct `dbgshim.dll`:

```powershell
# Find your .NET runtime versions
dir "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\"

# Copy matching version (e.g., for .NET 6 app)
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll" "D:\Bin\netcoredbg\"
```

### deps.json conflict error

**Symptom:** Launch fails with "assembly has already been found but with a different file extension".

**Cause:** You're debugging `.exe` instead of `.dll` for a .NET 6+ app.

**Solution:** This should be auto-resolved. If not, explicitly pass the `.dll` path:
```
program: "App.dll"  # instead of "App.exe"
```

### Program not found with pre_build

**Symptom:** `start_debug` with `pre_build: true` fails saying program doesn't exist.

**Cause:** Old version of netcoredbg-mcp that validated path before building.

**Solution:** Update to latest version. The fix defers path validation until after build completes.

### Breakpoints not hitting

**Symptom:** Breakpoints are set but never triggered.

**Possible causes:**
1. **Wrong configuration:** Debug build required (not Release)
2. **Source mismatch:** Binary doesn't match source files
3. **Optimized code:** JIT optimization can affect line mappings

**Solution:** Use `pre_build: true` to ensure fresh Debug build before debugging.

### Attach mode: empty stack traces

**Symptom:** After attaching to running process, `get_call_stack` returns empty.

**Cause:** netcoredbg doesn't support `justMyCode` in attach mode (upstream limitation).

**Solution:** Use `start_debug` instead. If you must attach, expect limited functionality.

## Requirements

- Python 3.10+
- [netcoredbg](https://github.com/Samsung/netcoredbg/releases)
- .NET SDK (for the apps you're debugging)

## How It Works

1. **MCP Layer:** Exposes debugging tools via Model Context Protocol
2. **Session Manager:** Manages debug session state, validates paths, handles events
3. **DAP Client:** Communicates with netcoredbg via Debug Adapter Protocol (JSON-RPC over stdio)
4. **Build Manager:** Optionally builds project before debugging (pre_build feature)
5. **Version Checker:** Validates dbgshim.dll compatibility with target runtime

## Known Limitations

1. **Single session:** Only one debug session at a time (by design for simplicity)
2. **Attach mode:** Limited functionality due to netcoredbg upstream limitation
3. **dbgshim version:** Must manually match version to target runtime
4. **Windows focus:** Primary development/testing on Windows (Linux/macOS may work)

## License

MIT

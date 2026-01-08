# netcoredbg-mcp

MCP (Model Context Protocol) server for debugging C#/.NET applications using [netcoredbg](https://github.com/Samsung/netcoredbg).

## Features

- **Standalone debugging** - No VS Code required
- **Full DAP support** - Uses Debug Adapter Protocol via netcoredbg
- **Breakpoint management** - Add, remove, list, clear breakpoints with conditions
- **Execution control** - Start, stop, continue, step over/into/out, pause
- **State inspection** - Variables, call stack, threads, expression evaluation
- **Exception handling** - Get exception info when stopped on exception

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    MCP Server (Python)                   │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ MCP Tools   │  │ DAP Client  │  │ Session Manager │ │
│  │ (20 tools)  │←→│ (protocol)  │←→│ (state)         │ │
│  └─────────────┘  └──────┬──────┘  └─────────────────┘ │
└──────────────────────────┼──────────────────────────────┘
                           │ stdio
                    ┌──────▼──────┐
                    │ netcoredbg  │
                    │ (DAP Server)│
                    └─────────────┘
```

## Installation

```bash
# Clone the repository
git clone https://github.com/thebtf/netcoredbg-mcp.git
cd netcoredbg-mcp

# Install with pip
pip install -e .
```

## Configuration

### Claude Code (Recommended)

The easiest way to add netcoredbg-mcp to Claude Code:

```bash
# Install globally with automatic project detection
claude mcp add --scope user netcoredbg -- \
  uv --directory "D:\Dev\netcoredbg-mcp" run netcoredbg-mcp --project-from-cwd
```

Set `NETCOREDBG_PATH` environment variable or add to your shell profile:

```powershell
# PowerShell profile (~\Documents\PowerShell\Microsoft.PowerShell_profile.ps1)
$env:NETCOREDBG_PATH = "C:\path\to\netcoredbg\netcoredbg.exe"
```

### JSON Configuration (Claude Desktop, Cursor, etc.)

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": [
        "--directory",
        "D:\\Dev\\netcoredbg-mcp",
        "run",
        "netcoredbg-mcp",
        "--project-from-cwd"
      ],
      "env": {
        "NETCOREDBG_PATH": "C:\\path\\to\\netcoredbg\\netcoredbg.exe"
      }
    }
  }
}
```

### Installed Package

If installed via pip (`pip install netcoredbg-mcp`):

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "args": ["--project-from-cwd"],
      "env": {
        "NETCOREDBG_PATH": "C:\\path\\to\\netcoredbg\\netcoredbg.exe"
      }
    }
  }
}
```

### Project-Scoped Configuration (`.mcp.json`)

For Claude Code 0.2.50+, you can add a `.mcp.json` file to your project root. This configuration is automatically loaded when Claude Code opens the project:

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": [
        "--directory",
        "D:\\Dev\\netcoredbg-mcp",
        "run",
        "netcoredbg-mcp"
      ],
      "env": {
        "NETCOREDBG_PATH": "C:\\path\\to\\netcoredbg\\netcoredbg.exe",
        "NETCOREDBG_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

See `.mcp.json.example` for a template.

### Project Detection (`--project-from-cwd`)

The `--project-from-cwd` flag automatically detects your .NET project by searching upward from the current directory for:
1. `.sln` files (solution - preferred for multi-project setups)
2. `.csproj`/`.vbproj`/`.fsproj` files (project files)
3. `.git` directory (git root as fallback)

This allows you to debug any .NET project from the directory where Claude Code is running.

## Available Tools

### Debug Control
| Tool | Description |
|------|-------------|
| `start_debug` | **Recommended.** Start debugging a .NET program with full feature support |
| `attach_debug` | Attach to a running .NET process (⚠️ limited functionality) |
| `stop_debug` | Stop the debug session |
| `continue_execution` | Continue program execution |
| `pause_execution` | Pause program execution |
| `step_over` | Step over to next line |
| `step_into` | Step into function call |
| `step_out` | Step out of current function |
| `get_debug_state` | Get current session state |

#### ⚠️ Attach Mode Limitations

`attach_debug` has **significant limitations** due to an upstream netcoredbg restriction:

- **`justMyCode` is NOT supported in attach mode** — this is a netcoredbg limitation, not fixable by this MCP server
- Stack traces may be **incomplete or empty**
- Variable inspection may not work reliably

**Always prefer `start_debug`** which has full functionality. Use `attach_debug` only when you must debug an already-running process that cannot be restarted (e.g., production service, container).

### Breakpoints
| Tool | Description |
|------|-------------|
| `add_breakpoint` | Add breakpoint with optional condition |
| `remove_breakpoint` | Remove a breakpoint |
| `list_breakpoints` | List all breakpoints |
| `clear_breakpoints` | Clear breakpoints |

### Inspection
| Tool | Description |
|------|-------------|
| `get_threads` | Get all threads |
| `get_call_stack` | Get call stack for thread |
| `get_scopes` | Get variable scopes for frame |
| `get_variables` | Get variables in scope |
| `evaluate_expression` | Evaluate expression |
| `get_exception_info` | Get exception details |
| `get_output` | Get debug output |

## MCP Resources

| Resource URI | Description |
|--------------|-------------|
| `debug://state` | Current session state |
| `debug://breakpoints` | All active breakpoints |
| `debug://output` | Debug console output |
| `debug://threads` | Current threads |

## Command Line Options

| Option | Description |
|--------|-------------|
| `--project PATH` | Project root path for debugging. All debug operations will be constrained to this path. |
| `--project-from-cwd` | Auto-detect project from current working directory. Searches upward for `.sln`, `.csproj`/`.vbproj`/`.fsproj`, or `.git` markers. Cannot be used with `--project`. |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NETCOREDBG_PATH` | Path to netcoredbg executable |
| `NETCOREDBG_PROJECT_ROOT` | Project root path for debugging (alternative to `--project`) |
| `MCP_PROJECT_ROOT` | Fallback project root (if `NETCOREDBG_PROJECT_ROOT` not set) |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Requirements

- Python 3.10+
- netcoredbg (included in `bin/` or provide via `NETCOREDBG_PATH`)

## ⚠️ Critical: dbgshim.dll Version Compatibility

**The `dbgshim.dll` in your netcoredbg folder MUST match the major version of the .NET runtime you're debugging.**

This is an **undocumented Microsoft requirement**. Using a mismatched version causes `E_NOINTERFACE (0x80004002)` errors and empty call stacks.

| Target Runtime | Required dbgshim.dll |
|----------------|---------------------|
| .NET 6.x | From `dotnet/shared/Microsoft.NETCore.App/6.0.x/` |
| .NET 7.x | From `dotnet/shared/Microsoft.NETCore.App/7.0.x/` |
| .NET 8.x | From `dotnet/shared/Microsoft.NETCore.App/8.0.x/` |
| .NET 9.x | From `dotnet/shared/Microsoft.NETCore.App/9.0.x/` |

**Auto-detection:** This MCP server automatically detects version mismatches and warns you during `start_debug`. If you see a warning, copy the correct `dbgshim.dll` before debugging.

```powershell
# Example: Copy dbgshim for .NET 6 debugging
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll" "path\to\netcoredbg\"
```

## Troubleshooting

### Empty call stack / E_NOINTERFACE (0x80004002) error

**Symptom:** `get_call_stack` returns empty array or error containing `0x80004002`.

**Cause:** `dbgshim.dll` version mismatch. The `dbgshim.dll` in your netcoredbg folder must match the major version of the .NET runtime you're debugging.

**Solution:** Copy `dbgshim.dll` from the matching .NET SDK:

```powershell
# For .NET 6 apps:
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.x\dbgshim.dll" "path\to\netcoredbg\"

# For .NET 8 apps:
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\8.0.x\dbgshim.dll" "path\to\netcoredbg\"
```

Replace `6.0.x` or `8.0.x` with your actual installed version (e.g., `6.0.36`).

**Note:** This is an undocumented requirement. Microsoft only documents that `mscordbi.dll` must match the runtime version, but `dbgshim.dll` also has version-specific behavior for `ICorDebugThread3::CreateStackWalk`.

### Diagnostic environment variable

Set `NETCOREDBG_STACKTRACE_DELAY_MS` to add a delay before stackTrace requests (useful for diagnosing timing issues):

```json
"env": {
  "NETCOREDBG_STACKTRACE_DELAY_MS": "300"
}
```

## License

MIT

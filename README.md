# netcoredbg-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](#requirements)
[![MCP](https://img.shields.io/badge/MCP-Server-6f42c1)](https://modelcontextprotocol.io/)
[![Platform](https://img.shields.io/badge/Platform-Windows-2ea44f)](#limitations)

MCP (Model Context Protocol) server for debugging C#/.NET applications using [netcoredbg](https://github.com/Samsung/netcoredbg).

**Debug .NET apps from AI agents** — set breakpoints, step through code, inspect variables, and evaluate expressions without requiring VS Code or any IDE.

## Quick Links

- **Get Started:** [Install](#installation) · [Configure](#configuration) · [First Debug Session](#first-debug-session)
- **Reference:** [Tools](#available-tools) · [Troubleshooting](#troubleshooting) · [Architecture](#architecture)

---

## Highlights

| Feature | Description |
|---------|-------------|
| 🚀 **Standalone** | No IDE required — works directly with AI agents |
| 🔧 **Full DAP** | Complete Debug Adapter Protocol via netcoredbg |
| 🏗️ **Pre-build** | Build before debug with `pre_build: true` |
| 🎯 **Smart Resolution** | Auto-resolves `.exe` → `.dll` for .NET 6+ |
| ⚠️ **Version Check** | Detects dbgshim.dll mismatches automatically |

---

## Critical Notes

> [!WARNING]
> **dbgshim.dll Version Compatibility**
>
> The `dbgshim.dll` in your netcoredbg folder **MUST match the major version** of the .NET runtime you're debugging.
> This is an undocumented Microsoft requirement. Mismatch causes:
> - `E_NOINTERFACE (0x80004002)` errors
> - Empty call stacks
> - Failed variable inspection

| Target Runtime | Required dbgshim.dll Source |
|----------------|----------------------------|
| .NET 6.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.x\dbgshim.dll` |
| .NET 7.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\7.0.x\dbgshim.dll` |
| .NET 8.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\8.0.x\dbgshim.dll` |
| .NET 9.x | `C:\Program Files\dotnet\shared\Microsoft.NETCore.App\9.0.x\dbgshim.dll` |

```powershell
# Example: Setup for .NET 6 debugging
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll" "D:\Bin\netcoredbg\"
```

> [!TIP]
> This MCP server automatically detects mismatches and warns you during `start_debug`.

> [!IMPORTANT]
> **Prefer `start_debug` over `attach_debug`**
>
> `attach_debug` has significant upstream limitations in netcoredbg — stack traces and variable inspection may be incomplete or empty.

---

## Installation

### Requirements

- Python 3.10+
- [netcoredbg](https://github.com/Samsung/netcoredbg/releases)
- .NET SDK (for the apps you're debugging)

### Install the MCP Server

```bash
# Clone the repository
git clone https://github.com/thebtf/netcoredbg-mcp.git
cd netcoredbg-mcp

# Install with uv (recommended)
uv sync

# Or install with pip
pip install -e .
```

### Install netcoredbg

Download from [Samsung/netcoredbg releases](https://github.com/Samsung/netcoredbg/releases) and extract:
- **Windows:** `D:\Bin\netcoredbg\`
- **macOS/Linux:** `/opt/netcoredbg/`

---

## Configuration

### Environment Variable

Set `NETCOREDBG_PATH` in your shell profile:

```powershell
# PowerShell profile (~\Documents\PowerShell\Microsoft.PowerShell_profile.ps1)
$env:NETCOREDBG_PATH = "D:\Bin\netcoredbg\netcoredbg.exe"
```

```bash
# Bash/Zsh (~/.bashrc or ~/.zshrc)
export NETCOREDBG_PATH="/opt/netcoredbg/netcoredbg"
```

> [!IMPORTANT]
> **Use `uv run --project` NOT `uv --directory`**
>
> The `--directory` flag changes the working directory, which breaks `--project-from-cwd` detection.

### Base Server Configuration

All clients use this same server definition:

```jsonc
{
  "netcoredbg": {
    "command": "uv",
    "args": [
      "run",
      "--project", "/path/to/netcoredbg-mcp",
      "netcoredbg-mcp",
      "--project-from-cwd"
    ],
    "env": {
      "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
    }
  }
}
```

---

## Client Setup

<details>
<summary><b>Claude Code (CLI)</b></summary>

```bash
claude mcp add --scope user netcoredbg -- \
  uv run --project "/path/to/netcoredbg-mcp" netcoredbg-mcp --project-from-cwd
```

**Verify installation:**
```bash
claude mcp list
```

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Add to your Claude Desktop configuration file:

| OS | Config Location |
|----|-----------------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp", "--project-from-cwd"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Cursor</b></summary>

Add to Cursor's MCP configuration:

| OS | Config Location |
|----|-----------------|
| macOS | `~/.cursor/mcp.json` |
| Windows | `%USERPROFILE%\.cursor\mcp.json` |

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp", "--project-from-cwd"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Windsurf</b></summary>

Add to Windsurf's MCP configuration:

| OS | Config Location |
|----|-----------------|
| macOS | `~/.codeium/windsurf/mcp_config.json` |
| Windows | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp", "--project-from-cwd"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Zed</b></summary>

Add to Zed's settings file (`~/.config/zed/settings.json`):

```jsonc
{
  "context_servers": {
    "netcoredbg": {
      "command": {
        "path": "uv",
        "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp", "--project-from-cwd"],
        "env": {
          "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
        }
      }
    }
  }
}
```

</details>

<details>
<summary><b>VS Code + Continue</b></summary>

Add to Continue's configuration (`~/.continue/config.json`):

```jsonc
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "stdio",
          "command": "uv",
          "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp", "--project-from-cwd"],
          "env": {
            "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
          }
        }
      }
    ]
  }
}
```

</details>

<details>
<summary><b>Project-Scoped Config (.mcp.json)</b></summary>

Add to your .NET project root for automatic loading when opening the project:

```jsonc
{
  "mcpServers": {
    "netcoredbg": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/netcoredbg-mcp", "netcoredbg-mcp"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe",
        "NETCOREDBG_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```

> [!NOTE]
> With project-scoped config, use `NETCOREDBG_PROJECT_ROOT` instead of `--project-from-cwd`.

</details>

---

## First Debug Session

### Typical Workflow

```
1. start_debug     → Launch program under debugger
2. add_breakpoint  → Set breakpoints in source files
3. continue        → Run until breakpoint hit
4. get_call_stack  → Inspect where you stopped
5. get_variables   → Examine local variables
6. step_over       → Step through code
7. stop_debug      → End session
```

### Example: start_debug with Pre-build

```python
start_debug(
    program="/path/to/MyApp.exe",      # Auto-resolves to .dll for .NET 6+
    pre_build=True,                     # Build before launching
    build_project="/path/to/MyApp.csproj",
    build_configuration="Debug",
    stop_at_entry=False
)
```

### Smart .exe → .dll Resolution

For .NET 6+ applications (WPF, WinForms, Console), the SDK creates:
- `App.exe` — Native host launcher
- `App.dll` — Actual managed code

Debugging `.exe` causes a "deps.json conflict" error. This MCP server **automatically resolves `.exe` to `.dll`** when a matching `.dll` and `.runtimeconfig.json` exist.

---

## Available Tools

### Debug Control

| Tool | Description |
|------|-------------|
| `start_debug` | **Recommended.** Launch program with full debug support. Supports `pre_build`. |
| `attach_debug` | Attach to running process ⚠️ Limited functionality |
| `stop_debug` | Stop the debug session |
| `continue_execution` | Continue program execution |
| `pause_execution` | Pause program execution |
| `step_over` | Step over to next line |
| `step_into` | Step into function call |
| `step_out` | Step out of current function |
| `get_debug_state` | Get current session state |

<details>
<summary><b>start_debug Parameters</b></summary>

| Parameter | Type | Description |
|-----------|------|-------------|
| `program` | string | Path to .exe or .dll (auto-resolved) |
| `cwd` | string? | Working directory |
| `args` | list? | Command line arguments |
| `env` | dict? | Environment variables |
| `stop_at_entry` | bool | Stop at program entry point |
| `pre_build` | bool | Build before launching |
| `build_project` | string? | Path to .csproj (required if pre_build) |
| `build_configuration` | string | "Debug" or "Release" |

</details>

### Breakpoints

| Tool | Description |
|------|-------------|
| `add_breakpoint` | Add breakpoint with optional condition and hit count |
| `remove_breakpoint` | Remove a breakpoint by file and line |
| `list_breakpoints` | List all active breakpoints |
| `clear_breakpoints` | Clear all breakpoints (optionally by file) |

### Inspection

| Tool | Description |
|------|-------------|
| `get_threads` | Get all threads with their states |
| `get_call_stack` | Get call stack for a thread |
| `get_scopes` | Get variable scopes for a stack frame |
| `get_variables` | Get variables in a scope |
| `evaluate_expression` | Evaluate expression in current context |
| `get_exception_info` | Get exception details when stopped on exception |
| `get_output` | Get debug console output |

### MCP Resources

| Resource URI | Description |
|--------------|-------------|
| `debug://state` | Current session state |
| `debug://breakpoints` | All active breakpoints |
| `debug://output` | Debug console output buffer |

---

## Troubleshooting

<details>
<summary><b>Empty call stack / E_NOINTERFACE (0x80004002)</b></summary>

**Symptom:** `get_call_stack` returns empty array or error containing `0x80004002`.

**Cause:** `dbgshim.dll` version mismatch between netcoredbg and target runtime.

**Solution:**
1. Check the warning from `start_debug` — it shows exact versions
2. Copy the correct `dbgshim.dll`:

```powershell
# Find your .NET runtime versions
dir "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\"

# Copy matching version (e.g., for .NET 6 app)
copy "C:\Program Files\dotnet\shared\Microsoft.NETCore.App\6.0.36\dbgshim.dll" "D:\Bin\netcoredbg\"
```

</details>

<details>
<summary><b>deps.json conflict error</b></summary>

**Symptom:** Launch fails with "assembly has already been found but with a different file extension".

**Cause:** Debugging `.exe` instead of `.dll` for a .NET 6+ app.

**Solution:** Should be auto-resolved. If not, explicitly pass the `.dll` path:
```
program: "App.dll"  # instead of "App.exe"
```

</details>

<details>
<summary><b>Program not found with pre_build</b></summary>

**Symptom:** `start_debug` with `pre_build: true` fails saying program doesn't exist.

**Cause:** Old version that validated path before building.

**Solution:** Update to latest version. Path validation is now deferred until after build.

</details>

<details>
<summary><b>Breakpoints not hitting</b></summary>

**Symptom:** Breakpoints are set but never triggered.

**Possible causes:**
1. Wrong configuration (Release instead of Debug)
2. Source mismatch (binary doesn't match source)
3. JIT optimization affecting line mappings

**Solution:** Use `pre_build: true` to ensure fresh Debug build.

</details>

<details>
<summary><b>Attach mode: empty stack traces</b></summary>

**Symptom:** After attaching to running process, `get_call_stack` returns empty.

**Cause:** netcoredbg doesn't support `justMyCode` in attach mode (upstream limitation).

**Solution:** Use `start_debug` instead. If you must attach, expect limited functionality.

</details>

---

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

### How It Works

1. **MCP Layer** — Exposes debugging tools via Model Context Protocol
2. **Session Manager** — Manages debug session state, validates paths, handles events
3. **DAP Client** — Communicates with netcoredbg via Debug Adapter Protocol (JSON-RPC over stdio)
4. **Build Manager** — Optionally builds project before debugging (`pre_build` feature)
5. **Version Checker** — Validates dbgshim.dll compatibility with target runtime

---

## Command Line Options

| Option | Description |
|--------|-------------|
| `--project PATH` | Explicit project root path |
| `--project-from-cwd` | Auto-detect project from CWD |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NETCOREDBG_PATH` | **Required.** Path to netcoredbg executable |
| `NETCOREDBG_PROJECT_ROOT` | Project root path (alternative to `--project`) |
| `LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `LOG_FILE` | Path to log file for diagnostics |

---

## Limitations

- **Single session** — Only one debug session at a time (by design)
- **Attach mode** — Limited functionality due to netcoredbg upstream limitation
- **dbgshim version** — Must manually match version to target runtime
- **Windows focus** — Primary development/testing on Windows (Linux/macOS may work)

---

## License

MIT

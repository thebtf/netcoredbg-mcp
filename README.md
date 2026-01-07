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
│  │ (18 tools)  │←→│ (protocol)  │←→│ (state)         │ │
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

Add to your MCP client configuration (e.g., Claude Desktop, Cursor):

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "python",
      "args": ["-m", "netcoredbg_mcp"],
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

Or if installed as a package:

```json
{
  "mcpServers": {
    "netcoredbg": {
      "command": "netcoredbg-mcp",
      "env": {
        "NETCOREDBG_PATH": "/path/to/netcoredbg/netcoredbg.exe"
      }
    }
  }
}
```

## Available Tools

### Debug Control
| Tool | Description |
|------|-------------|
| `start_debug` | Start debugging a .NET program |
| `attach_debug` | Attach to a running .NET process |
| `stop_debug` | Stop the debug session |
| `continue_execution` | Continue program execution |
| `pause_execution` | Pause program execution |
| `step_over` | Step over to next line |
| `step_into` | Step into function call |
| `step_out` | Step out of current function |
| `get_debug_state` | Get current session state |

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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NETCOREDBG_PATH` | Path to netcoredbg executable |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Requirements

- Python 3.10+
- netcoredbg (included in `bin/` or provide via `NETCOREDBG_PATH`)

## License

MIT

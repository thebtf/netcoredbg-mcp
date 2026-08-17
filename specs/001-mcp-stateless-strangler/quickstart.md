# Quickstart — Milestone M1 Acceptance Journeys

This is an execution contract for a future internal candidate, not a public
release claim. T-008 owns a runnable RED suite, its test project, controlled
executable DAP adapter fixture, and reflection/process driver. T-009 owns only
the lifecycle build/test receipt below; T-005 owns materializing the actual
candidate launch, C# v2.1.0 client, environment, cleanup, and modern source-tree
candidate test receipt. T-001 owns retained-Python and rollback blocks.
T-007 MUST refuse consumer evidence if a required final receipt is absent,
stale, or traceable to no accepted receipt.

## 1. T-009 lifecycle and T-005 modern candidate receipts

### 1.1 T-009 lifecycle build/test receipt

T-009 creates only the owned BCL lifecycle project. Independent T-009 acceptance
hardens incomplete discriminators, proves the corrected cases RED against frozen
production, then the current 49-case candidate test suite goes GREEN. Run from the
repository root with .NET 8:

```powershell
dotnet build host/NetCoreDbg.Mcp.Stateless/NetCoreDbg.Mcp.Stateless.csproj -c Debug --nologo
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj --nologo
```

The test command builds the controlled adapter as test infrastructure. This
receipt uses no `NETCOREDBG_PATH`, MCP SDK/client, candidate launch command, or
network. It records the current 49-case denominator and actual GREEN result. It
proves test execution only: no installed or public consumer proof exists here.

**Receipt:** On 2026-08-18 the exact commands above succeeded: build completed
with 0 warnings and 0 errors; the exact test command completed in 1 m 58 s with
**49 passed, 0 failed, and 0 skipped**. This remains source-tree candidate test
evidence only, not an MCP client, public entrypoint, or consumer proof.

### 1.2 T-005 modern candidate command block — source-tree test receipt

Run from the repository root with .NET 8:

```powershell
dotnet build host/NetCoreDbg.Mcp.Stateless/NetCoreDbg.Mcp.Stateless.csproj -c Debug --nologo
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj --filter 'FullyQualifiedName~ModernMcp' --nologo
```

The build produces the internal candidate at
`host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0/NetCoreDbg.Mcp.Stateless.exe`.
The test command launches that built artifact through the official
`ModelContextProtocol` v2.1.0 stdio transport/client and uses the controlled DAP
adapter as `NETCOREDBG_PATH`; it requires no installed debugger or network.

The 33-case denominator executes the seven-step journey:

1. `server/discover` is a literal first request and returns tools capability,
   positive `ttlMs`, and public cache scope.
2. Fresh candidate processes accept `tools/list` and valid
   `tools/call(start_debug)` as literal first requests.
3. The ordered three-tool catalog/runtime schemas, all six discriminated
   `structuredContent` variants, and complete start envelope are exact;
   ordinary call results carry no cache fields.
4. Raw official transport evidence proves form MRTR input-required, no
   `requestState`, distinct-ID retry with repeated arguments and
   `inputResponses`, plus the no-form complete application error.
5. Empty/extra arguments and unusable handles produce their exact public error
   classes before prohibited native launch/state/stop actions.
6. Explicit opaque tokens survive independent/interleaved requests within one
   live host; concurrent stop has one winner and one native cleanup; stopped,
   unavailable, prior-process, and later-use tokens are uniformly not-found.
7. The official stdio client observes bounded candidate completion, while MCP
   stdout remains parseable protocol frames throughout every process exchange.

**Receipt:** On 2026-08-18 the exact commands above succeeded: build completed
with 0 warnings and 0 errors; the exact filtered test command completed in 1 m
31 s with **33 passed, 0 failed, and 0 skipped**, including executable parity for
all **6/6** application-schema variants. This is source-tree candidate test
evidence only; it does not select a public entrypoint, install a package, or
establish consumer proof.

## 2. Retained Python command block — owned by T-001

### Receipt: retained Python consumer — `PRODUCT_WORKS`

Run from the repository root in PowerShell. The first command constructs the controlled
program. The block then creates a disposable MCP stdio consumer driver and isolated provider
environment; it does not install globally or publish a package. Set `NETCOREDBG_PATH` to an
already-installed debugger; do not invoke `--setup`, which can download or change managed
debugger state.
Before the block, set `NETCOREDBG_PATH` to the existing local `netcoredbg.exe` by the
operator's normal environment convention; do not hardcode a workstation path in this receipt.

```powershell
dotnet build tests/fixtures/SmokeTestApp -c Debug --nologo
$env:UV_PROJECT_ENVIRONMENT = '.agent/tmp/t001-retained-python'
$env:T001_REPO_ROOT = (Get-Location).Path
New-Item -ItemType Directory -Force .agent/tmp | Out-Null
@'
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

repo = Path(os.environ["T001_REPO_ROOT"])
fixture = repo / "tests" / "fixtures" / "SmokeTestApp"
program = fixture / "bin" / "Debug" / "net8.0-windows" / "SmokeTestApp.dll"


async def main() -> None:
    if not program.is_file():
        raise RuntimeError(f"missing controlled fixture program: {program}")
    env = get_default_environment()
    env["NETCOREDBG_PATH"] = os.environ["NETCOREDBG_PATH"]
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    params = StdioServerParameters(
        command="netcoredbg-mcp",
        args=["--project-from-cwd"],
        env=env,
        cwd=str(fixture),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            started = await session.call_tool(
                "start_debug",
                {"program": str(program), "pre_build": False, "stop_at_entry": True},
            )
            if started.isError:
                raise AssertionError(f"start_debug error: {started}")
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                state = await session.read_resource("debug://state")
                state_payload = json.loads(state.contents[0].text)
                if state_payload.get("stopReason") is not None:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"debuggee did not stop at entry: {state_payload}")
                await asyncio.sleep(0.1)
            stopped = await session.call_tool("stop_debug", {})
            result = {
                "product_works": bool(
                    initialized.capabilities.tools is not None
                    and len(tools.tools) == 135
                    and not started.isError
                    and state_payload.get("stopReason") is not None
                    and not stopped.isError
                ),
                "denominator": "5/5",
                "tool_count": len(tools.tools),
                "stopped_at_entry": state_payload.get("stopReason") is not None,
            }
            print(json.dumps(result, sort_keys=True))
            if not result["product_works"]:
                raise SystemExit(1)


asyncio.run(main())
'@ | Set-Content -NoNewline .agent/tmp/t001-retained-python-consumer.py
uv sync --locked --project .
uv run --no-sync --project . python .agent/tmp/t001-retained-python-consumer.py
```

The disposable consumer driver launches the retained public console script
`netcoredbg-mcp --project-from-cwd` from the isolated environment with the controlled
`tests/fixtures/SmokeTestApp/bin/Debug/net8.0-windows/SmokeTestApp.dll` program. It uses
an MCP stdio client to initialize, list tools, start the fixture stopped at entry, observe
the stopped state, and stop it: **5/5** checks. On this receipt it reported
`{"product_works": true, "tool_count": 135, "stopped_at_entry": true}`.

Environment and cleanup: `NETCOREDBG_PATH` must name an existing executable, and `dotnet`
and `uv` must be on `PATH`. Section 3 removes the generated driver and environment. This
proves the retained installed console-script consumer path and controlled debugger lifecycle
only; it makes no claim about the proposed modern candidate or its MCP conformance.

## 3. Rollback command block — owned by T-001

### Receipt: candidate removal and retained-Python replay — `PRODUCT_WORKS`

T-001 performs no candidate selection or launch, so there is no candidate process or
artifact to remove in this receipt. Rollback is therefore the exact retained-Python replay
below, with no package publication, console-script replacement, persisted-data migration, or
client-configuration cutover to reverse. With the same environment, fixture build, and
generated driver from section 2, execute this exact replay and cleanup:

```powershell
uv run --no-sync --project . python .agent/tmp/t001-retained-python-consumer.py
Remove-Item -Recurse -Force .agent/tmp/t001-retained-python
Remove-Item -Force .agent/tmp/t001-retained-python-consumer.py
Remove-Item Env:T001_REPO_ROOT, Env:UV_PROJECT_ENVIRONMENT
```

The replay is the rollback proof: it re-runs the unchanged public `netcoredbg-mcp`
console-script journey against the same `SmokeTestApp.dll` denominator (**5/5**) and must
again emit `PRODUCT_WORKS` (`product_works: true`, 135 tools, entry stop observed). The
cleanup removes task scratch only; it does not remove the built controlled fixture or alter
the debugger installation. The 2026-08-13 replay yielded `PRODUCT_WORKS` with the same
5/5 denominator before cleanup.

## 4. T-007 closure and M1 merge decision

On 2026-08-13, after the fresh independent T-006 **PASS**, T-007 ran the
materialized candidate command and received **28/28** passed with **6/6**
application-schema variants. The complete integrated candidate project then
passed **39/39** tests with 0 failed and 0 skipped in 2 m 5 s. The unchanged
installed public Python consumer
then reached **PRODUCT_WORKS (5/5)** with 135 tools and an entry stop. With the
internal candidate left unselected, the exact retained-Python rollback replay
again reached **PRODUCT_WORKS (5/5)** with the same 135-tool and entry-stop
observations.

Final cleanup measured 0 candidate processes, 0 controlled-adapter processes,
0 installed-debugger processes, no retained disposable environment or consumer
driver, and 0 owned modern scratch directories. **Decision: M1 is merge-ready.**
This decision authorizes no publication, package change, public-entrypoint
selection, legacy-relay cutover, or release; those remain a separately
authorized later slice.

## Failure interpretation

- Unsupported version is official `-32022` data `requested`/`supported`, not an
  application tool result.
- Input-required is official MRTR behavior, not a server-initiated request.
- Invalid advertised arguments are complete `CallToolResult` application errors
  with `structuredContent.kind: invalid_tool_arguments` and `isError: true`.
- A missing/short/malformed handle is uniform `DEBUG_SESSION_NOT_FOUND`, not an
  invalid-argument or existence-oracle result.
- A green test suite without materialized commands, both consumer receipts, and
  rollback replay is insufficient for M1 acceptance.

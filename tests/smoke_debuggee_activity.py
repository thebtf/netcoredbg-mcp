"""Controlled SmokeTestApp proof for the bounded debuggee activity tool."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.debug import register_debug_tools

SMOKE_DLL = (
    Path(__file__).parent
    / "fixtures"
    / "SmokeTestApp"
    / "bin"
    / "Debug"
    / "net8.0-windows"
    / "SmokeTestApp.dll"
)


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, annotations: Any = None):
        del annotations

        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return decorator


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def main() -> None:
    if not os.environ.get("NETCOREDBG_PATH"):
        raise RuntimeError("NETCOREDBG_PATH is required")
    if not SMOKE_DLL.is_file():
        raise RuntimeError(f"Build SmokeTestApp first: {SMOKE_DLL}")

    manager = SessionManager()
    registry = ToolRegistry()
    register_debug_tools(
        registry,
        manager,
        ownership=SimpleNamespace(release=lambda *_args, **_kwargs: None),
        notify_state_changed=_noop,
        check_session_access=lambda _ctx: None,
        execute_and_wait=_noop,
        resolve_project_root=_noop,
        resolve_project_root_readonly=_noop,
    )

    try:
        await manager.launch(program=str(SMOKE_DLL), args=["longrun"])
        if manager.state.state != DebugState.RUNNING:
            raise AssertionError(f"expected RUNNING, got {manager.state.state.value}")

        result = await registry.tools["debuggee_activity"](
            SimpleNamespace(),
            window_ms=1000,
        )
        if "error" in result:
            raise AssertionError(result["error"])

        data = result["data"]
        assert data["deltas"]["outputEvents"] >= 1, data
        assert data["observedActivity"] is True, data
        assert "outputEvents" in data["activitySignals"], data
        assert data["windowMs"] == 1000, data
        assert 900 <= data["elapsedMs"] <= 2500, data
        assert data["end"]["state"] in {"running", "terminated"}, data
        assert data["instructionsExecuted"]["available"] is False, data
        print(json.dumps(data, sort_keys=True))
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())

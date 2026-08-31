"""Installed-wheel MCP ClientSession proof for the WPF popup submenu."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

POLL_DEADLINE_SECONDS = 15.0
POLL_CALL_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.25
REQUIRED_TOOLS = frozenset(
    {
        "start_debug",
        "cleanup_processes",
        "ui_find_element",
        "ui_get_window_tree",
        "ui_key_sequence",
        "ui_invoke",
        "ui_text",
    }
)


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _payload(result: CallToolResult) -> dict[str, Any]:
    if result.isError:
        raise AssertionError(f"tools/call reported isError: {result}")
    if not result.content:
        raise AssertionError("tools/call returned no content blocks")
    first = result.content[0]
    if not isinstance(first, TextContent):
        raise AssertionError(f"tools/call returned non-text content: {first!r}")
    payload = json.loads(first.text)
    if not isinstance(payload, dict):
        raise AssertionError(f"tools/call payload was not an object: {payload!r}")
    return payload


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    if "error" in payload:
        raise AssertionError(f"tool response error: {payload}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AssertionError(f"tool response has no object data: {payload}")
    return data


def _tree_contains_automation_id(value: object, automation_id: str) -> bool:
    if isinstance(value, dict):
        return value.get("automationId") == automation_id or any(
            _tree_contains_automation_id(child, automation_id) for child in value.values()
        )
    if isinstance(value, list):
        return any(_tree_contains_automation_id(child, automation_id) for child in value)
    return False


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _payload(await session.call_tool(name, arguments))


async def _poll_discovery(
    session: ClientSession,
    *,
    name: str,
    arguments: dict[str, Any],
    matches: Callable[[dict[str, Any]], bool],
) -> tuple[dict[str, Any], dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + POLL_DEADLINE_SECONDS
    attempts = 0
    last_response: dict[str, Any] | None = None
    terminal_event = "deadline_elapsed_without_match"

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            terminal_event = "deadline_elapsed_after_response"
            break

        attempts += 1
        try:
            last_response = await asyncio.wait_for(
                _call(session, name, arguments),
                timeout=min(POLL_CALL_TIMEOUT_SECONDS, remaining),
            )
        except asyncio.TimeoutError:
            terminal_event = "attempt_timeout_no_response"
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
            if deadline - loop.time() <= 0:
                break
            continue

        data = last_response.get("data")
        if isinstance(data, dict) and matches(data):
            return data, {
                "operation": name,
                "attempts": attempts,
                "deadline_seconds": POLL_DEADLINE_SECONDS,
            }

        remaining = deadline - loop.time()
        if remaining <= 0:
            terminal_event = "deadline_elapsed_after_response"
            break
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    deadline_evidence = {
        "operation": name,
        "arguments": arguments,
        "attempts": attempts,
        "deadline_seconds": POLL_DEADLINE_SECONDS,
        "terminal_event": terminal_event,
        "last_received_response": last_response,
    }
    raise AssertionError(f"discovery deadline: {json.dumps(deadline_evidence, sort_keys=True)}")


def _server_environment() -> dict[str, str]:
    """Pass the exact installed bridge and debugger through MCP's child environment."""
    return {
        "FLAUI_BRIDGE_PATH": _environment("FLAUI_BRIDGE_PATH"),
        "NETCOREDBG_PATH": _environment("NETCOREDBG_PATH"),
    }


async def main() -> None:
    consumer_cli = _environment("NETCOREDBG_MCP_CONSUMER_CLI")
    wpf_root = _environment("NETCOREDBG_MCP_WPF_ROOT")
    evidence: dict[str, Any] = {"installed_cli": os.path.basename(consumer_cli)}

    params = StdioServerParameters(
        command=consumer_cli,
        args=["--project-from-cwd"],
        env=_server_environment(),
        cwd=wpf_root,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            try:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                missing = sorted(REQUIRED_TOOLS - names)
                assert not missing, f"installed server missing required tools: {missing}"
                evidence["tool_count"] = len(names)

                launch = _data(
                    await _call(
                        session,
                        "start_debug",
                        {
                            "program": "bin/Debug/net8.0-windows/WpfSmokeApp.dll",
                            "pre_build": False,
                        },
                    )
                )
                assert launch.get("success") is True, launch
                evidence["start_debug"] = {"success": launch["success"]}

                parent, parent_poll = await _poll_discovery(
                    session,
                    name="ui_find_element",
                    arguments={"automation_id": "submenuParent", "control_type": "MenuItem"},
                    matches=lambda data: data.get("automationId") == "submenuParent",
                )
                evidence["parent_automation_id"] = parent["automationId"]

                pre_expansion_tree = _data(
                    await _call(
                        session,
                        "ui_get_window_tree",
                        {"max_depth": 6, "max_children": 100},
                    )
                )
                assert not _tree_contains_automation_id(pre_expansion_tree, "submenuChild"), (
                    pre_expansion_tree
                )
                evidence["pre_expansion"] = {"popup_tree_child_present": False}

                native_enter = _data(
                    await _call(
                        session,
                        "ui_key_sequence",
                        {
                            "modifiers": [],
                            "keys": ["enter"],
                            "automation_id": "submenuParent",
                            "control_type": "MenuItem",
                        },
                    )
                )
                assert native_enter.get("status") == "PASS", native_enter
                assert native_enter.get("sent_count") == 1, native_enter
                focus_receipt = native_enter.get("focused")
                assert isinstance(focus_receipt, dict), native_enter
                assert focus_receipt.get("foreground_verified") is True, native_enter
                assert focus_receipt.get("target_focus_verified") is True, native_enter
                evidence["native_parent_enter"] = {
                    "status": native_enter["status"],
                    "sent_count": native_enter["sent_count"],
                    "focus_receipt": focus_receipt,
                }

                child, child_poll = await _poll_discovery(
                    session,
                    name="ui_find_element",
                    arguments={"automation_id": "submenuChild", "control_type": "MenuItem"},
                    matches=lambda data: data.get("automationId") == "submenuChild",
                )
                evidence["post_expansion"] = {"popup_child_discovered": True}
                evidence["popup_child_automation_id"] = child["automationId"]

                invocation = _data(
                    await _call(
                        session,
                        "ui_invoke",
                        {"automation_id": "submenuChild", "control_type": "MenuItem"},
                    )
                )
                assert invocation.get("invoked") is True, invocation
                assert invocation.get("method") == "InvokePattern", invocation
                evidence["child_invocation"] = {
                    "invoked": invocation["invoked"],
                    "method": invocation["method"],
                }

                output = _data(
                    await _call(
                        session,
                        "ui_text",
                        {"action": "read", "automation_id": "txtOutput"},
                    )
                )
                assert output.get("text") == "WpfWorkflow Submenu child invoked", output
                evidence["observable_result"] = output["text"]
                evidence["polling"] = [parent_poll, child_poll]
            except AssertionError as error:
                if str(error).startswith("discovery deadline: "):
                    evidence["discovery_deadline"] = json.loads(
                        str(error).removeprefix("discovery deadline: ")
                    )
                raise
            finally:
                try:
                    cleanup = _data(await _call(session, "cleanup_processes", {"force": True}))
                    evidence["cleanup"] = {"terminated": cleanup.get("terminated")}
                finally:
                    print(f"WPF installed submenu evidence: {json.dumps(evidence, sort_keys=True)}")


if __name__ == "__main__":
    asyncio.run(main())

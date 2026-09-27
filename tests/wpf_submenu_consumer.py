"""Installed-wheel MCP ClientSession proof for the WPF popup submenu."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import time
from collections.abc import Callable
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

POLL_DEADLINE_SECONDS = 15.0
POLL_CALL_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.25
BRIDGE_READINESS_TIMEOUT_SECONDS = 45.0
REQUIRED_TOOLS = frozenset(
    {
        "start_debug",
        "cleanup_processes",
        "ui_find_element",
        "ui_get_window_tree",
        "ui_set_focus",
        "ui_send_keys",
        "ui_send_keys_focused",
        "ui_send_keys_batch",
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


def _num_lock_enabled() -> bool:
    """Read the foreground input queue's lock state, not this client's stale queue."""
    user32 = ctypes.windll.user32
    foreground = user32.GetForegroundWindow()
    thread = user32.GetWindowThreadProcessId(foreground, None)
    current = ctypes.windll.kernel32.GetCurrentThreadId()
    if (
        not foreground
        or not thread
        or (thread != current and not user32.AttachThreadInput(current, thread, True))
    ):
        raise RuntimeError("cannot read foreground NumLock state")
    try:
        return bool(user32.GetKeyState(0x90) & 1)
    finally:
        if thread != current:
            user32.AttachThreadInput(current, thread, False)


def _restore_num_lock(initial: bool) -> bool:
    """Restore independently of the MCP server, even when its input call fails."""
    user32 = ctypes.windll.user32
    if _num_lock_enabled() != initial:
        user32.keybd_event(0x90, 0x45, 0x01, 0)
        user32.keybd_event(0x90, 0x45, 0x03, 0)
        time.sleep(0.05)
    restored = _num_lock_enabled() == initial
    if not restored:
        raise AssertionError("NumLock state was not restored")
    return restored


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
            num_lock_initial: bool | None = None
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
                try:
                    ready_tree = _data(
                        await asyncio.wait_for(
                            _call(
                                session, "ui_get_window_tree", {"max_depth": 1, "max_children": 5}
                            ),
                            timeout=BRIDGE_READINESS_TIMEOUT_SECONDS,
                        )
                    )
                except asyncio.TimeoutError as error:
                    raise AssertionError(
                        f"bridge readiness deadline: ui_get_window_tree did not respond "
                        f"within {BRIDGE_READINESS_TIMEOUT_SECONDS}s"
                    ) from error
                assert ready_tree.get("count", 0) > 0, f"bridge readiness failed: {ready_tree}"
                evidence["bridge_readiness"] = {"window_count": ready_tree["count"]}

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

                key_status_selector = {"automation_id": "keyEventStatus", "control_type": "Text"}
                target_selector = {"automation_id": "txtOutput", "control_type": "Edit"}
                key_cases = (
                    (
                        "selector_keypad",
                        "ui_send_keys",
                        {"keys": "{NUMPAD1}", **target_selector},
                        ((0x4F, False, None),),
                    ),
                    (
                        "selector_ordinary",
                        "ui_send_keys",
                        {"keys": "{ENTER}", **target_selector},
                        ((0x1C, False, 0x0D),),
                    ),
                    (
                        "focused_add",
                        "ui_send_keys_focused",
                        {"keys": "{NUMPADADD}"},
                        ((0x4E, False, None),),
                    ),
                    (
                        "focused_ordinary",
                        "ui_send_keys_focused",
                        {"keys": "{ENTER}"},
                        ((0x1C, False, 0x0D),),
                    ),
                    (
                        "batch_order",
                        "ui_send_keys_batch",
                        {
                            "keys": ["{NUMPADSUBTRACT}", "{NUMPADENTER}", "{ENTER}"],
                            "automation_id": "txtOutput",
                        },
                        ((0x4A, False, None), (0x1C, True, None), (0x1C, False, 0x0D)),
                    ),
                    (
                        "sequence_order",
                        "ui_key_sequence",
                        {
                            "modifiers": [],
                            "keys": [
                                "NUMPAD1",
                                "NUMPADADD",
                                "NUMPADSUBTRACT",
                                "NUMPADENTER",
                                "1",
                                "ENTER",
                            ],
                            **target_selector,
                        },
                        (
                            (0x4F, False, None),
                            (0x4E, False, None),
                            (0x4A, False, None),
                            (0x1C, True, None),
                            (0x02, False, 0x31),
                            (0x1C, False, 0x0D),
                        ),
                    ),
                )
                # Each eight-key call fits within the fixture's 32-event observer window.
                keypad_tokens = (
                    *(
                        (f"NUMPAD{digit}", scan, False, None)
                        for digit, scan in enumerate(
                            (0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49)
                        )
                    ),
                    ("NUMPADADD", 0x4E, False, None),
                    ("NUMPADSUBTRACT", 0x4A, False, None),
                    ("NUMPADMULTIPLY", 0x37, False, None),
                    ("NUMPADDIVIDE", 0x35, True, None),
                    ("NUMPADDECIMAL", 0x53, False, None),
                    ("NUMPADENTER", 0x1C, True, None),
                )
                for start in (0, 8):
                    chunk = keypad_tokens[start : start + 8]
                    key_cases += (
                        (
                            f"vocabulary_{start // 8 + 1}",
                            "ui_send_keys_batch",
                            {"keys": [f"{{{token}}}" for token, *_ in chunk], **target_selector},
                            tuple((scan, extended, vk) for _, scan, extended, vk in chunk),
                        ),
                    )
                key_cases += (
                    (
                        "numlock",
                        "ui_send_keys",
                        {"keys": "{NUMLOCK}", **target_selector},
                        ((0x45, False, 0x90),),
                    ),
                )
                observed_keys = {}
                for case, tool, arguments, physical_keys in key_cases:
                    if case.startswith("focused_"):
                        focus = _data(await _call(session, "ui_set_focus", target_selector))
                        assert focus.get("focused") is True, focus
                    before = _data(await _call(session, "ui_find_element", key_status_selector))
                    previous_events = json.loads(before.get("name") or "[]")
                    if case == "numlock":
                        num_lock_initial = _num_lock_enabled()
                    delivery = _data(await _call(session, tool, arguments))
                    if tool == "ui_key_sequence":
                        assert delivery.get("status") == "PASS", delivery
                        assert delivery.get("sent_count") == len(physical_keys), delivery
                    expected = tuple(
                        (scan, extended, vk, down)
                        for scan, extended, vk in physical_keys
                        for down in (True, False)
                    )

                    def matches(data: dict[str, Any]) -> bool:
                        events = json.loads(data.get("name") or "[]")
                        if len(events) != min(32, len(previous_events) + len(expected)):
                            return False
                        if events == previous_events:
                            return False
                        return all(
                            event["scan"] == scan
                            and event["extended"] is extended
                            and (vk is None or event["vk"] == vk)
                            and event["down"] is down
                            for event, (scan, extended, vk, down) in zip(
                                events[-len(expected) :], expected, strict=True
                            )
                        )

                    observed, _ = await _poll_discovery(
                        session,
                        name="ui_find_element",
                        arguments=key_status_selector,
                        matches=matches,
                    )
                    observed_keys[case] = json.loads(observed["name"])[-len(expected) :]
                evidence["keypad_events"] = observed_keys
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
                    if num_lock_initial is not None:
                        evidence["numlock_restored"] = _restore_num_lock(num_lock_initial)
                finally:
                    try:
                        cleanup = _data(await _call(session, "cleanup_processes", {"force": True}))
                        evidence["cleanup"] = {"terminated": cleanup.get("terminated")}
                    finally:
                        print(
                            "WPF installed submenu evidence:", json.dumps(evidence, sort_keys=True)
                        )


if __name__ == "__main__":
    asyncio.run(main())

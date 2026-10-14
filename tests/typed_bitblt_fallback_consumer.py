"""Built-wheel MCP proof for exact-HWND compositor screenshot recovery."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import io
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

POLL_DEADLINE_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.2
CALIBRATION_RGB = (18, 192, 222)
STRICT_FIELDS = frozenset({"expected_hwnd", "expected_physical_width", "expected_physical_height"})
EXPECTED_PROTOCOL_VERSION = "2025-11-25"
EXPECTED_TOOL_COUNT = 135


def _environment(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"missing required environment variable: {name}"
    return value


def _select_cases(
    cases: tuple[tuple[Any, ...], ...],
) -> tuple[tuple[Any, ...], ...]:
    selection = os.environ.get("NETCOREDBG_COMPOSITOR_CASES")
    if selection is None:
        return cases

    requested = tuple(name.strip() for name in selection.split(","))
    assert requested and all(requested), (
        "NETCOREDBG_COMPOSITOR_CASES must select at least one named case"
    )
    assert len(set(requested)) == len(requested), (
        "NETCOREDBG_COMPOSITOR_CASES must not select a case more than once"
    )
    known = {case[0] for case in cases}
    unknown = set(requested) - known
    assert not unknown, (
        f"NETCOREDBG_COMPOSITOR_CASES contains unknown case names: {', '.join(sorted(unknown))}"
    )
    return tuple(case for case in cases if case[0] in requested)


def _payload(result: CallToolResult) -> dict[str, Any]:
    assert result.isError is False, f"tools/call isError must be explicit false: {result}"
    assert result.structuredContent is None, (
        f"tools/call must use text content without structuredContent: {result}"
    )
    texts = [content for content in result.content if isinstance(content, TextContent)]
    assert texts, f"tool call returned no text content: {result}"
    payload = json.loads(texts[-1].text)
    assert isinstance(payload, dict), f"tool call returned non-object JSON: {payload!r}"
    return payload


def _assert_spatial_images(
    result: CallToolResult,
    metadata: dict[str, Any],
    samples: tuple[tuple[int, int, tuple[int, int, int]], ...],
) -> dict[str, str]:
    images = [content for content in result.content if isinstance(content, ImageContent)]
    assert len(images) == 1 and images[0].mimeType == "image/webp", result
    inline_bytes = base64.b64decode(images[0].data, validate=True)
    raw_bytes = Path(str(metadata["raw_path"])).read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == metadata["raw_sha256"], metadata
    with Image.open(io.BytesIO(raw_bytes)) as raw, Image.open(io.BytesIO(inline_bytes)) as inline:
        assert raw.format == "PNG" and inline.format == "WEBP", metadata
        assert raw.size == (metadata["raw_width"], metadata["raw_height"]), metadata
        raw.load()
        inline.load()
        raw_rgb, inline_rgb = raw.convert("RGB"), inline.convert("RGB")
        for x, y, expected_rgb in samples:
            assert 0 <= x < raw.width and 0 <= y < raw.height, (x, y, raw.size)
            assert raw_rgb.getpixel((x, y)) == expected_rgb, (x, y, metadata)
            inline_xy = (x * inline.width // raw.width, y * inline.height // raw.height)
            actual = inline_rgb.getpixel(inline_xy)
            assert isinstance(actual, tuple), (inline.mode, actual)
            assert all(abs(a - b) <= 12 for a, b in zip(actual, expected_rgb)), (
                inline_xy,
                actual,
                expected_rgb,
                metadata,
            )
    return {
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "inline_sha256": hashlib.sha256(inline_bytes).hexdigest(),
    }


def _calibration_samples(
    hwnd: int, color: tuple[int, int, int] = CALIBRATION_RGB
) -> tuple[tuple[int, int, tuple[int, int, int]], ...]:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    client_to_screen = user32.ClientToScreen
    client_to_screen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    client_to_screen.restype = wintypes.BOOL
    get_window_rect = user32.GetWindowRect
    get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    get_window_rect.restype = wintypes.BOOL
    origin, bounds = wintypes.POINT(0, 0), wintypes.RECT()
    assert client_to_screen(hwnd, ctypes.byref(origin)), ctypes.get_last_error()
    assert get_window_rect(hwnd, ctypes.byref(bounds)), ctypes.get_last_error()
    offset_x, offset_y = origin.x - bounds.left, origin.y - bounds.top
    return (
        (offset_x + 40, offset_y + 40, color),
        (offset_x + 120, offset_y + 120, (255, 255, 255)),
    )


def _set_calibration_state(hwnd: int, process_id: int, state: int) -> None:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    owner = wintypes.DWORD()
    get_owner = user32.GetWindowThreadProcessId
    get_owner.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_owner.restype = wintypes.DWORD
    assert get_owner(hwnd, ctypes.byref(owner)) and owner.value == process_id, (hwnd, owner.value)
    send = user32.SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    assert send(hwnd, 0x802A, state, 0, 0x0002, 2_000, ctypes.byref(result)), (
        ctypes.get_last_error()
    )
    assert result.value == 1, "owned compositor-marker fixture did not acknowledge repaint"


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _payload(await session.call_tool(name, arguments))


async def _stop_then_force_cleanup(session: ClientSession, launched: bool) -> dict[str, Any]:
    try:
        if launched:
            await _call(session, "stop_debug", {})
    finally:
        cleanup = _data(await _call(session, "cleanup_processes", {"force": True}))
    return cleanup


async def _prove_post_cleanup_liveness(
    cleanup: dict[str, Any], processes: list[dict[str, int | str]]
) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    while True:
        liveness: list[dict[str, int | str | bool]] = []
        for process in processes:
            role = process["role"]
            pid = process["pid"]
            assert isinstance(role, str), process
            assert type(pid) is int and pid > 0, process
            liveness.append({"role": role, "pid": pid, "alive": psutil.pid_exists(pid)})
        alive = [process for process in liveness if process["alive"]]
        if not alive:
            return {
                "terminated": cleanup.get("terminated"),
                "post_cleanup_liveness": liveness,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"post-cleanup liveness remains true: {alive}")
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in payload, payload
    data = payload.get("data")
    assert isinstance(data, dict), payload
    return data


def _enable_per_monitor_dpi_coordinates() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_context = user32.SetThreadDpiAwarenessContext
    set_context.argtypes = [ctypes.c_void_p]
    set_context.restype = ctypes.c_void_p
    previous = set_context(ctypes.c_void_p(-4))
    assert previous, f"SetThreadDpiAwarenessContext failed: {ctypes.get_last_error()}"


def _physical_window_for_process(
    process_id: int, title: str | None = None, *, visible: bool = True
) -> tuple[int, int, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [enum_proc, ctypes.c_void_p]
    enum_windows.restype = ctypes.c_bool
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    get_pid.restype = ctypes.c_ulong
    get_rect = user32.GetWindowRect
    get_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
    get_rect.restype = ctypes.c_bool
    is_visible = user32.IsWindowVisible
    is_visible.argtypes = [ctypes.c_void_p]
    is_visible.restype = ctypes.c_bool
    get_text_length = user32.GetWindowTextLengthW
    get_text_length.argtypes = [ctypes.c_void_p]
    get_text_length.restype = ctypes.c_int
    get_text = user32.GetWindowTextW
    get_text.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    get_text.restype = ctypes.c_int

    candidates: list[tuple[int, int, int, int]] = []

    @enum_proc
    def collect(hwnd: int, _lparam: int) -> bool:
        owner = ctypes.c_ulong()
        get_pid(hwnd, ctypes.byref(owner))
        if owner.value != process_id or (visible and not is_visible(hwnd)):
            return True
        if title is not None:
            length = get_text_length(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            get_text(hwnd, buffer, len(buffer))
            if buffer.value != title:
                return True
        rect = Rect()
        if not get_rect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width > 0 and height > 0:
            candidates.append((width * height, int(hwnd), width, height))
        return True

    assert enum_windows(collect, None), f"EnumWindows failed: {ctypes.get_last_error()}"
    assert candidates, f"no visible top-level HWND for PID {process_id}"
    _, hwnd, width, height = max(candidates)
    return hwnd, width, height


def _foreground_hwnd() -> int:
    hwnd = int(ctypes.WinDLL("user32", use_last_error=True).GetForegroundWindow())
    assert hwnd != 0, f"GetForegroundWindow failed: {ctypes.get_last_error()}"
    return hwnd


async def _wait_for_ui(session: ClientSession) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + POLL_DEADLINE_SECONDS
    last: dict[str, Any] | None = None
    while True:
        last = await _call(session, "ui_get_window_tree", {"max_depth": 1, "max_children": 5})
        data = last.get("data")
        if isinstance(data, dict) and data.get("count", 0) > 0:
            return data
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"fixture discovery deadline: {last}")
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _bridge_processes(processes: Iterable[object]) -> list[dict[str, Any]]:
    return [
        process
        for process in processes
        if isinstance(process, dict)
        and process.get("role") == "flaui_bridge"
        and process.get("alive") is True
    ]


def _assert_no_persistence(case_root: Path, response: dict[str, Any]) -> None:
    assert not {"raw_path", "crop_path", "hd_path"}.intersection(response), response
    assert list(case_root.glob("mcp-netcoredbg-*")) == [], list(case_root.iterdir())


def _trace(trace_path: Path) -> list[str]:
    if not trace_path.exists():
        return []
    return [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line]


def _assert_bridge_artifact_identity(
    identity: object,
    expected_path: Path,
    expected_sha256: str,
    *,
    response: dict[str, Any],
) -> dict[str, Any]:
    assert isinstance(identity, dict), response
    actual_path = Path(str(identity.get("path"))).resolve()
    assert actual_path == expected_path.resolve(), (actual_path, expected_path, response)
    assert identity.get("sha256") == expected_sha256, response
    return {"path": str(actual_path), "sha256": expected_sha256}


def _assert_host_identity(
    trace_path: Path,
    *,
    source_identity: str,
    host_library: Path,
    host_library_sha256: str,
    bridge_library: Path,
    bridge_library_sha256: str,
) -> dict[str, Any]:
    identity_path = Path(f"{trace_path}.identity.json")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert isinstance(identity, dict), identity
    assert identity.get("source_identity") == source_identity, identity
    return {
        "source_identity": source_identity,
        "host_assembly": _assert_bridge_artifact_identity(
            identity.get("host_assembly"),
            host_library,
            host_library_sha256,
            response=identity,
        ),
        "bridge_assembly": _assert_bridge_artifact_identity(
            identity.get("bridge_assembly"),
            bridge_library,
            bridge_library_sha256,
            response=identity,
        ),
    }


async def _run_case(
    *,
    name: str,
    consumer_cli: str,
    fixture_root: Path,
    bridge_path: Path,
    bridge_sha256: str,
    bridge_identity_path: Path,
    bridge_identity_sha256: str,
    host_library: Path,
    host_library_sha256: str,
    source_identity: str,
    case_root: Path,
    trace_path: Path | None,
    fixture_env: dict[str, str],
    expected: str,
) -> dict[str, Any]:
    server_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NETCOREDBG_TEST_", "NETCOREDBG_COMPOSITOR_"))
    }
    server_env.update(
        {
            "FLAUI_BRIDGE_PATH": str(bridge_path),
            "NETCOREDBG_COMPOSITOR_SOURCE_SHA256": source_identity,
            "TEMP": str(case_root),
            "TMP": str(case_root),
        }
    )
    if trace_path is not None:
        server_env["NETCOREDBG_COMPOSITOR_TRACE_FILE"] = str(trace_path)
    server_env.update(fixture_env)
    params = StdioServerParameters(
        command=consumer_cli,
        args=["--project-from-cwd"],
        cwd=str(fixture_root),
        env=server_env,
    )
    evidence: dict[str, Any] = {"case": name}
    launched = False
    raw_paths: list[Path] = []
    spawned_processes: list[dict[str, int | str]] = []

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            try:
                initialized = await session.initialize()
                assert initialized.protocolVersion == EXPECTED_PROTOCOL_VERSION, initialized
                assert initialized.serverInfo.name == "netcoredbg-mcp", initialized
                assert initialized.capabilities.tools is not None, initialized
                assert initialized.capabilities.tools.listChanged is not True, initialized

                tools = await session.list_tools()
                assert len(tools.tools) == EXPECTED_TOOL_COUNT, tools
                screenshot_tool = next(
                    (tool for tool in tools.tools if tool.name == "ui_take_screenshot"), None
                )
                assert screenshot_tool is not None, "installed server omitted ui_take_screenshot"
                assert screenshot_tool.inputSchema.get("type") == "object", (
                    screenshot_tool.inputSchema
                )
                properties = screenshot_tool.inputSchema.get("properties", {})
                assert STRICT_FIELDS.issubset(properties), screenshot_tool.inputSchema
                assert all(
                    {entry.get("type") for entry in properties[field].get("anyOf", [])}
                    == {"integer", "null"}
                    for field in STRICT_FIELDS
                ), screenshot_tool.inputSchema

                launch = _data(
                    await _call(
                        session,
                        "start_debug",
                        {
                            "program": "bin/Debug/net8.0-windows/WpfSmokeApp.dll",
                            "cwd": str(fixture_root),
                            "env": fixture_env,
                            "pre_build": False,
                            "stealth_mode": True,
                        },
                    )
                )
                assert launch.get("success") is True, launch
                launched = True
                status = _data(await _call(session, "cleanup_processes", {"force": False}))
                debuggees = [
                    process
                    for process in status.get("processes", [])
                    if isinstance(process, dict)
                    and process.get("role") == "debuggee"
                    and process.get("alive") is True
                ]
                assert len(debuggees) == 1, status
                process_id = debuggees[0].get("pid")
                assert type(process_id) is int and process_id > 0, status
                spawned_processes = [{"role": "debuggee", "pid": process_id}]
                await _wait_for_ui(session)
                calibration_title = "WPF Smoke Capture Calibration"
                if fixture_env.get("NETCOREDBG_TEST_CAPTURE_CALIBRATION"):
                    switched = _data(
                        await _call(session, "ui_switch_window", {"name": calibration_title})
                    )
                    assert switched.get("switched") is True, switched
                hwnd, width, height = _physical_window_for_process(
                    process_id,
                    calibration_title
                    if fixture_env.get("NETCOREDBG_TEST_CAPTURE_CALIBRATION")
                    else None,
                )
                if expected in {"ordinary_fallback", "ordinary_final_black"}:
                    activation = _data(await _call(session, "ui_bring_to_front", {}))
                    assert activation.get("activated") is True, activation
                    assert activation.get("stealth_mode") is False, activation
                foreground_before = _foreground_hwnd()
                expected_hwnd: int | None = hwnd
                if expected == "target_mismatch":
                    expected_hwnd, _, _ = _physical_window_for_process(
                        process_id, "WPF Smoke Test", visible=False
                    )
                    assert expected_hwnd != hwnd, (expected_hwnd, hwnd)
                expected_width = width - 1 if expected == "dimension_mismatch" else width
                expected_height = height
                screenshot_arguments: dict[str, Any] = {
                    "evidence": True,
                    "format": "png",
                }
                if expected not in {"ordinary_fallback", "ordinary_final_black"}:
                    screenshot_arguments.update(
                        {
                            "expected_hwnd": expected_hwnd,
                            "expected_physical_width": expected_width,
                            "expected_physical_height": expected_height,
                        }
                    )
                screenshot_result = await session.call_tool(
                    "ui_take_screenshot", screenshot_arguments
                )
                response = _payload(screenshot_result)
                images = [
                    item for item in screenshot_result.content if isinstance(item, ImageContent)
                ]

                if expected in {"control", "fallback", "ordinary_fallback"}:
                    _assert_bridge_artifact_identity(
                        response.get("bridge_assembly"),
                        bridge_identity_path,
                        bridge_identity_sha256,
                        response=response,
                    )
                    metadata = response
                    if expected == "control":
                        assert metadata.get("evidence_grade") == "lossless_raster", metadata
                        assert metadata.get("method") == "PrintWindow", metadata
                    else:
                        assert metadata.get("evidence_grade") == "compositor_window_raster", (
                            metadata
                        )
                        assert metadata.get("method") == "WindowsGraphicsCapture", metadata
                        assert (
                            metadata.get("source_api") == "GraphicsCaptureItem.CreateForWindow"
                        ), metadata
                        assert metadata.get("capture_authority") == "hwnd_compositor_frame", (
                            metadata
                        )
                        assert metadata.get("alternate_attempts") == 1, metadata
                        compositor = metadata.get("compositor", {})
                        assert type(compositor.get("frame_time_ticks")) is int, metadata
                        assert compositor["frame_time_ticks"] > 0, metadata
                        assert compositor.get("item_size") == {"width": width, "height": height}, (
                            metadata
                        )
                        assert compositor.get("frame_content_size") == compositor["item_size"], (
                            metadata
                        )
                        assert compositor.get("policy") == {"status": "ALLOWED", "affinity": 0}, (
                            metadata
                        )
                        assert compositor.get("window_closed") is False, metadata
                    assert (
                        metadata.get("hwnd") == hwnd and metadata.get("process_id") == process_id
                    ), metadata
                    assert metadata.get("capture_stability", {}).get("before") == metadata.get(
                        "capture_stability", {}
                    ).get("after"), metadata
                    assert metadata.get("foreground") == {"mutation_attempted": False}, metadata
                    assert _foreground_hwnd() == foreground_before, metadata
                    assert metadata.get("target_comparability", {}).get("status") == (
                        "UNASSERTED" if expected == "ordinary_fallback" else "MATCHED"
                    ), metadata
                    raw_paths.append(Path(str(metadata["raw_path"])))
                    first_images = _assert_spatial_images(
                        screenshot_result, metadata, _calibration_samples(hwnd)
                    )
                    _set_calibration_state(hwnd, process_id, 1)
                    second_result = await session.call_tool(
                        "ui_take_screenshot", screenshot_arguments
                    )
                    second = _payload(second_result)
                    assert second.get("hwnd") == hwnd and second.get("process_id") == process_id, (
                        second
                    )
                    assert second.get("evidence_grade") == metadata["evidence_grade"], second
                    assert second.get("foreground") == {"mutation_attempted": False}, second
                    assert second.get("target_comparability") == metadata["target_comparability"], (
                        second
                    )
                    raw_paths.append(Path(str(second["raw_path"])))
                    second_images = _assert_spatial_images(
                        second_result, second, _calibration_samples(hwnd, (222, 18, 192))
                    )
                    assert second_images["raw_sha256"] != first_images["raw_sha256"], second
                    assert second_images["inline_sha256"] != first_images["inline_sha256"], second
                    if expected != "control":
                        assert (
                            second["compositor"]["frame_time_ticks"]
                            > metadata["compositor"]["frame_time_ticks"]
                        ), second
                    assert _foreground_hwnd() == foreground_before, second
                    evidence["images"] = [first_images, second_images]
                elif expected == "dimension_mismatch":
                    assert len(images) == 1, screenshot_result
                    assert response.get("evidence_grade") == "preview_only", response
                    assert response.get("target_comparability", {}).get("status") == "MISMATCH", (
                        response
                    )
                    assert (
                        response.get("target_comparability", {}).get("code")
                        == "PHYSICAL_CAPTURE_MISMATCH"
                    ), response
                    assert response.get("target_comparability", {}).get("mismatch_fields") == [
                        "width"
                    ], response
                    assert response.get("physical_target", {}).get("status") == "mismatch", response
                    _assert_no_persistence(case_root, response)
                elif expected in {"final_black", "ordinary_final_black"}:
                    assert not images, screenshot_result
                    assert response.get("classification") == "PROBABLE_BLACK_FRAME", response
                    _assert_no_persistence(case_root, response)
                    if expected == "ordinary_final_black":
                        diagnostics = response.get("data", {}).get("capture_diagnostics", {})
                        assert diagnostics.get("producer") == "flaui_bridge", response
                        assert diagnostics.get("capture_method") == "WindowsGraphicsCapture", (
                            response
                        )
                        assert (
                            diagnostics.get("fallback_analysis", {}).get("probable_black") is True
                        ), response
                else:
                    assert not images, screenshot_result
                    assert response.get("code") == "PHYSICAL_CAPTURE_PROVENANCE_UNAVAILABLE", (
                        response
                    )
                    _assert_no_persistence(case_root, response)

                status = _data(await _call(session, "cleanup_processes", {"force": False}))
                bridges = _bridge_processes(status.get("processes", []))
                assert len(bridges) == 1, status
                bridge_pid = bridges[0].get("pid")
                assert type(bridge_pid) is int and bridge_pid > 0, status
                spawned_processes.append({"role": "flaui_bridge", "pid": bridge_pid})
                actual_bridge = Path(psutil.Process(bridge_pid).exe()).resolve()
                assert actual_bridge == bridge_path.resolve(), (actual_bridge, bridge_path)
                assert hashlib.sha256(actual_bridge.read_bytes()).hexdigest() == bridge_sha256
                evidence.update(
                    {
                        "target": {
                            "pid": process_id,
                            "hwnd": hwnd,
                            "width": width,
                            "height": height,
                            "expected_hwnd": expected_hwnd,
                            "expected_width": expected_width,
                            "expected_height": expected_height,
                        },
                        "bridge_sha256": bridge_sha256,
                        "response_kind": expected,
                    }
                )
                if trace_path is not None:
                    evidence["host_identity"] = _assert_host_identity(
                        trace_path,
                        source_identity=source_identity,
                        host_library=host_library,
                        host_library_sha256=host_library_sha256,
                        bridge_library=bridge_identity_path,
                        bridge_library_sha256=bridge_identity_sha256,
                    )
            finally:
                cleanup = await _stop_then_force_cleanup(session, launched)
                evidence["cleanup"] = await _prove_post_cleanup_liveness(
                    cleanup, spawned_processes if launched else []
                )

    for raw_path in raw_paths:
        assert not raw_path.exists(), f"session cleanup retained raw evidence: {raw_path}"
    if trace_path is not None:
        evidence["trace"] = _trace(trace_path)
    return evidence


async def main() -> None:
    _enable_per_monitor_dpi_coordinates()
    consumer_cli = _environment("NETCOREDBG_MCP_CONSUMER_CLI")
    fixture_root = Path(_environment("NETCOREDBG_MCP_WPF_ROOT"))
    production_bridge = Path(_environment("NETCOREDBG_COMPOSITOR_PRODUCTION_BRIDGE_PATH"))
    production_hash = _environment("NETCOREDBG_COMPOSITOR_PRODUCTION_BRIDGE_SHA256")
    production_identity = Path(_environment("NETCOREDBG_COMPOSITOR_PRODUCTION_IDENTITY_PATH"))
    production_identity_hash = _environment("NETCOREDBG_COMPOSITOR_PRODUCTION_IDENTITY_SHA256")
    test_bridge = Path(_environment("NETCOREDBG_COMPOSITOR_TEST_BRIDGE_PATH"))
    test_hash = _environment("NETCOREDBG_COMPOSITOR_TEST_BRIDGE_SHA256")
    host_managed_library = Path(_environment("NETCOREDBG_COMPOSITOR_TEST_HOST_LIBRARY_PATH"))
    host_managed_library_hash = _environment("NETCOREDBG_COMPOSITOR_TEST_HOST_LIBRARY_SHA256")
    host_bridge_library = Path(_environment("NETCOREDBG_COMPOSITOR_TEST_LIBRARY_PATH"))
    host_bridge_library_hash = _environment("NETCOREDBG_COMPOSITOR_TEST_LIBRARY_SHA256")
    source_identity = _environment("NETCOREDBG_COMPOSITOR_SOURCE_SHA256")
    root = Path(_environment("NETCOREDBG_COMPOSITOR_TEMP_ROOT"))
    assert hashlib.sha256(production_bridge.read_bytes()).hexdigest() == production_hash
    assert hashlib.sha256(production_identity.read_bytes()).hexdigest() == production_identity_hash
    assert hashlib.sha256(test_bridge.read_bytes()).hexdigest() == test_hash
    assert (
        hashlib.sha256(host_managed_library.read_bytes()).hexdigest() == host_managed_library_hash
    )
    assert hashlib.sha256(host_bridge_library.read_bytes()).hexdigest() == host_bridge_library_hash
    root.mkdir(parents=True, exist_ok=True)

    cases = (
        (
            "control",
            production_bridge,
            production_hash,
            production_identity,
            production_identity_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker"},
            "control",
            None,
        ),
        (
            "fallback",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker"},
            "fallback",
            root / "fallback.trace",
        ),
        (
            "primary_malformed",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {
                "NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker",
                "NETCOREDBG_COMPOSITOR_PRIMARY_SHAPE": "short",
            },
            "reject",
            root / "primary-malformed.trace",
        ),
        (
            "target_mismatch",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker"},
            "target_mismatch",
            root / "target-mismatch.trace",
        ),
        (
            "dimension_mismatch",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker"},
            "dimension_mismatch",
            root / "dimension-mismatch.trace",
        ),
        (
            "foreign_target",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {
                "NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker",
                "NETCOREDBG_COMPOSITOR_SNAPSHOT_PROCESS": "foreign",
            },
            "foreign_reject",
            root / "foreign-target.trace",
        ),
        (
            "target_change",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {
                "NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker",
                "NETCOREDBG_COMPOSITOR_CAPTURE_MODE": "target-change",
            },
            "target_change",
            root / "target-change.trace",
        ),
        (
            "final_black",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "black"},
            "final_black",
            root / "final-black.trace",
        ),
        (
            "ordinary_fallback",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker"},
            "ordinary_fallback",
            root / "ordinary-fallback.trace",
        ),
        (
            "ordinary_final_black",
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {"NETCOREDBG_TEST_CAPTURE_CALIBRATION": "black"},
            "ordinary_final_black",
            root / "ordinary-final-black.trace",
        ),
    )
    cases += tuple(
        (
            mode,
            test_bridge,
            test_hash,
            host_bridge_library,
            host_bridge_library_hash,
            {
                "NETCOREDBG_TEST_CAPTURE_CALIBRATION": "compositor-marker",
                "NETCOREDBG_COMPOSITOR_CAPTURE_MODE": mode,
            },
            mode,
            root / f"{mode}.trace",
        )
        for mode in (
            "process-change",
            "resize",
            "closed",
            "timeout",
            "policy-unknown",
            "protected",
            "close-at-acceptance",
        )
    )
    cases = _select_cases(cases)
    evidence = []
    try:
        for (
            name,
            bridge,
            bridge_hash,
            bridge_identity_path,
            bridge_identity_sha256,
            fixture_env,
            expected,
            trace_path,
        ) in cases:
            case_root = root / name
            case_root.mkdir()
            result = await _run_case(
                name=name,
                consumer_cli=consumer_cli,
                fixture_root=fixture_root,
                bridge_path=bridge,
                bridge_sha256=bridge_hash,
                bridge_identity_path=bridge_identity_path,
                bridge_identity_sha256=bridge_identity_sha256,
                host_library=host_managed_library,
                host_library_sha256=host_managed_library_hash,
                source_identity=source_identity,
                case_root=case_root,
                trace_path=trace_path,
                fixture_env=fixture_env,
                expected=expected,
            )
            if trace_path is None:
                assert "trace" not in result, result
            elif expected in {"fallback", "ordinary_fallback"}:
                assert result["trace"] == ["primary", "alternate"] * 2, result
            elif expected in {
                "final_black",
                "ordinary_final_black",
                "dimension_mismatch",
                "target_change",
                "process-change",
                "resize",
                "closed",
                "timeout",
            }:
                assert result["trace"] == ["primary", "alternate"], result
            elif expected == "close-at-acceptance":
                assert result["trace"][:2] == ["primary", "alternate"], result
                assert "acceptance-close-observed" in result["trace"], result
                assert "acceptance-close-not-observed" not in result["trace"], result
            elif expected == "reject":
                assert result["trace"] == ["primary"], result
            else:
                assert result["trace"] == [], result
            evidence.append(result)
    finally:
        print(f"Compositor installed consumer evidence: {json.dumps(evidence, sort_keys=True)}")


if __name__ == "__main__":
    asyncio.run(main())

"""Screenshot capture and annotation tests."""

from __future__ import annotations

import ctypes
import io
from types import SimpleNamespace

import pytest

Image = pytest.importorskip("PIL.Image")


def _bgra(r: int, g: int, b: int, a: int = 255) -> bytes:
    return bytes((b, g, r, a))


def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_analyze_screenshot_frame_distinguishes_near_black_from_uniform_nonblack() -> None:
    from netcoredbg_mcp.ui.screenshot import analyze_screenshot_frame

    assert analyze_screenshot_frame(_png((0, 0, 0)))["probable_black"] is True
    assert analyze_screenshot_frame(_png((3, 3, 3)))["probable_black"] is True
    assert analyze_screenshot_frame(_png((20, 20, 20)))["probable_black"] is False
    assert analyze_screenshot_frame(_png((255, 255, 255)))["probable_black"] is False


@pytest.mark.asyncio
async def test_ui_take_screenshot_rejects_probable_black_without_foreground_mutation(
    capturing_mcp,
    monkeypatch,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    black_png = _png((0, 0, 0))
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (black_png, 64, 64),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id=None,
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)

    response = await capturing_mcp.tools["ui_take_screenshot"](
        SimpleNamespace(),
        format="png",
    )

    assert isinstance(response, dict)
    assert response["classification"] == "PROBABLE_BLACK_FRAME"
    assert response["data"]["frame_analysis"]["probable_black"] is True
    assert response["data"]["foreground_mutation_attempted"] is False
    assert "ui_bring_to_front" in response["data"]["next_step"]


@pytest.mark.parametrize(
    "bridge_result",
    [
        pytest.param({"fallback": "flash-focus"}, id="fallback"),
        pytest.param({"method": "flash-focus"}, id="method"),
        pytest.param({"flash_ms": 80}, id="flash-duration"),
    ],
)
@pytest.mark.asyncio
async def test_ui_take_screenshot_preserves_bridge_foreground_mutation_on_invalid_response(
    capturing_mcp,
    monkeypatch,
    bridge_result,
) -> None:
    from unittest.mock import AsyncMock, patch

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    black_png = _png((0, 0, 0))
    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = SimpleNamespace(
        call=AsyncMock(return_value=bridge_result),
        is_running=True,
    )
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (black_png, 64, 64),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id=None,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
        response = await capturing_mcp.tools["ui_take_screenshot"](
            SimpleNamespace(),
            format="png",
        )

    assert response.get("classification") == "PROBABLE_BLACK_FRAME", response
    assert response["data"]["foreground_mutation_attempted"] is True
    backend._client.call.assert_awaited_once_with("screenshot", {})


@pytest.mark.asyncio
async def test_ui_take_annotated_screenshot_rejects_black_before_annotation(
    capturing_mcp,
    monkeypatch,
) -> None:
    from unittest.mock import patch

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    black_png = _png((0, 0, 0))
    backend = PywinautoBackend.__new__(PywinautoBackend)
    backend._ui = SimpleNamespace(process_id=42, _app=object())
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (black_png, 64, 64),
    )

    def unexpected_element_collection(*_args, **_kwargs):
        raise AssertionError("black frame must be rejected before annotation")

    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.collect_visible_elements",
        unexpected_element_collection,
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id=None,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
        response = await capturing_mcp.tools["ui_take_annotated_screenshot"](SimpleNamespace())

    assert isinstance(response, dict)
    assert response["classification"] == "PROBABLE_BLACK_FRAME"
    assert response["data"]["foreground_mutation_attempted"] is False


@pytest.mark.asyncio
async def test_black_annotated_capture_invalidates_cached_click_targets(
    capturing_mcp,
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock, patch

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    captures = iter(
        [
            (_png((255, 255, 255), (120, 80)), 120, 80),
            (_png((0, 0, 0), (120, 80)), 120, 80),
        ]
    )
    backend = PywinautoBackend.__new__(PywinautoBackend)
    backend._ui = SimpleNamespace(process_id=42, _app=object())
    backend.click_at = AsyncMock()

    def fake_get_window_rect(_hwnd, rect_ptr):
        rect = rect_ptr._obj
        rect.left = 100
        rect.top = 200
        rect.right = 220
        rect.bottom = 280
        return True

    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 555)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: next(captures),
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.collect_visible_elements",
        lambda _app, _max_depth, _interactive_only: [
            {
                "id": 7,
                "name": "Save",
                "type": "Button",
                "automationId": "saveButton",
                "bounds": {"x": 110, "y": 220, "width": 40, "height": 20},
            }
        ],
    )
    monkeypatch.setattr(
        "ctypes.windll",
        SimpleNamespace(user32=SimpleNamespace(GetWindowRect=fake_get_window_rect)),
        raising=False,
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id=None,
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
        first = await capturing_mcp.tools["ui_take_annotated_screenshot"](SimpleNamespace())
        rejected = await capturing_mcp.tools["ui_take_annotated_screenshot"](SimpleNamespace())
        click = await capturing_mcp.tools["ui_click_annotated"](
            SimpleNamespace(),
            element_id=7,
            generation=1,
        )

    assert isinstance(first, list)
    assert rejected["classification"] == "PROBABLE_BLACK_FRAME"
    assert click["error"].startswith("No annotation data")
    backend.click_at.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_bring_to_front_supports_pywinauto_fallback(
    capturing_mcp,
    monkeypatch,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.pywinauto_backend import PywinautoBackend

    backend = PywinautoBackend.__new__(PywinautoBackend)
    backend._ui = SimpleNamespace(process_id=42)
    show_calls: list[tuple[int, int]] = []
    restore_calls: list[int] = []
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.backend.create_backend",
        lambda *_args, **_kwargs: backend,
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid",
        lambda _pid: 123,
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.foreground.restore_foreground_window",
        lambda hwnd: restore_calls.append(hwnd) is None or True,
    )
    monkeypatch.setattr(
        ctypes.windll,
        "user32",
        SimpleNamespace(
            ShowWindow=lambda hwnd, command: show_calls.append((hwnd, command)) is None or True
        ),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=True,
        session_id=None,
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)

    response = await capturing_mcp.tools["ui_bring_to_front"](SimpleNamespace())

    assert "error" not in response
    assert response["data"]["activated"] is True
    assert response["data"]["hwnd"] == 123
    assert response["data"]["stealth_mode"] is False
    assert session.stealth_mode is False
    assert show_calls == [(123, 9)]
    assert restore_calls == [123]


def test_capture_window_decodes_top_down_dib_without_vertical_flip(monkeypatch) -> None:
    from netcoredbg_mcp.ui import screenshot

    width = 2
    height = 2
    top_down_bgra = b"".join(
        [
            _bgra(255, 0, 0),  # top-left: red
            _bgra(0, 255, 0),  # top-right: green
            _bgra(0, 0, 255),  # bottom-left: blue
            _bgra(255, 255, 255),  # bottom-right: white
        ]
    )

    class FakeUser32:
        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.left = 0
            rect.top = 0
            rect.right = width
            rect.bottom = height
            return True

        def GetDC(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 100

        def PrintWindow(self, _hwnd, _hdc, _flags):  # noqa: N802 - Win32 API shape
            return True

        def ReleaseDC(self, _hwnd, _hdc):  # noqa: N802 - Win32 API shape
            return 1

    class FakeGdi32:
        def CreateCompatibleDC(self, _wdc):  # noqa: N802 - Win32 API shape
            return 200

        def CreateCompatibleBitmap(self, _wdc, _width, _height):  # noqa: N802 - Win32 API shape
            return 300

        def SelectObject(self, _cdc, _bitmap):  # noqa: N802 - Win32 API shape
            return 400

        def BitBlt(self, *_args):  # noqa: N802 - Win32 API shape
            return True

        def GetDIBits(self, _cdc, _bitmap, _start, _lines, buffer, _bmi, _usage):  # noqa: N802 - Win32 API shape
            ctypes.memmove(buffer, top_down_bgra, len(top_down_bgra))
            return height

        def DeleteObject(self, _bitmap):  # noqa: N802 - Win32 API shape
            return True

        def DeleteDC(self, _cdc):  # noqa: N802 - Win32 API shape
            return True

    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32(), gdi32=FakeGdi32()),
        raising=False,
    )

    png_bytes, captured_width, captured_height = screenshot.capture_window(123)

    image = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    assert (captured_width, captured_height) == (width, height)
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((1, 0)) == (0, 255, 0, 255)
    assert image.getpixel((0, 1)) == (0, 0, 255, 255)
    assert image.getpixel((1, 1)) == (255, 255, 255, 255)

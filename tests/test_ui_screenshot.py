"""Screenshot capture and annotation tests."""

from __future__ import annotations

import ctypes
import io
import json
from types import SimpleNamespace

import pytest

Image = pytest.importorskip("PIL.Image")


@pytest.fixture(autouse=True)
def _default_legacy_backend(monkeypatch):
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.backend.create_backend",
        lambda **_kwargs: SimpleNamespace(process_id=42),
    )


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


@pytest.mark.asyncio
async def test_ui_take_screenshot_rejects_black_evidence_before_persistence(
    capturing_mcp,
    monkeypatch,
    tmp_path,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    black_png = _png((0, 0, 0))
    saved_names: list[str] = []

    def save_screenshot(_sid: str, data: bytes, name: str):
        saved_names.append(name)
        path = tmp_path / name
        path.write_bytes(data)
        return path

    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
        lambda _hwnd: (
            black_png,
            64,
            64,
            {
                "method": "PrintWindow",
                "hwnd": 123,
                "client_rect": {"left": 0, "top": 0, "right": 64, "bottom": 64},
                "dpi": 96,
                "dpi_scale": 1.0,
                "physical_width": 64,
                "physical_height": 64,
                "logical_width": 64.0,
                "logical_height": 64.0,
            },
        ),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="evidence-session",
        temp_manager=SimpleNamespace(save_screenshot=save_screenshot),
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)

    response = await capturing_mcp.tools["ui_take_screenshot"](
        SimpleNamespace(), evidence=True, format="png"
    )

    assert response["classification"] == "PROBABLE_BLACK_FRAME"
    assert saved_names == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_ui_take_screenshot_does_not_persist_evidence_when_capture_is_unstable(
    capturing_mcp,
    monkeypatch,
    tmp_path,
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    saved_names: list[str] = []

    def save_screenshot(_sid: str, data: bytes, name: str):
        saved_names.append(name)
        path = tmp_path / name
        path.write_bytes(data)
        return path

    def unstable_capture(_hwnd: int):
        raise RuntimeError(
            "Evidence capture invalidated because window changed during rasterization"
        )

    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.capture_window_evidence", unstable_capture)
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="evidence-session",
        temp_manager=SimpleNamespace(save_screenshot=save_screenshot),
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)

    response = await capturing_mcp.tools["ui_take_screenshot"](
        SimpleNamespace(), evidence=True, format="png"
    )

    assert (
        response["error"]
        == "Evidence capture invalidated because window changed during rasterization"
    )
    assert saved_names == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bridge_result", [{}, {"method": "BitBlt"}, {"base64": "invalid!"}])
@pytest.mark.asyncio
async def test_ui_take_screenshot_invalid_bridge_does_not_capture_another_desktop(
    capturing_mcp, monkeypatch, bridge_result
) -> None:
    from unittest.mock import AsyncMock

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = SimpleNamespace(call=AsyncMock(return_value=bridge_result), is_running=True)
    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", lambda **_kwargs: backend)
    other_png = _png((220, 40, 90))
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 888)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (other_png, 64, 64),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id=None,
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
    response = await capturing_mcp.tools["ui_take_screenshot"](SimpleNamespace(), format="png")
    assert isinstance(response, dict) and "error" in response


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


@pytest.mark.parametrize(
    ("client_rects", "dpis", "error", "expected_dpi_reads"),
    [
        pytest.param(
            [
                {"left": 0, "top": 0, "right": 2, "bottom": 2},
                {"left": 0, "top": 0, "right": 3, "bottom": 2},
            ],
            [96, 96],
            "changed during rasterization",
            2,
            id="client-resize",
        ),
        pytest.param(
            [
                {"left": 0, "top": 0, "right": 2, "bottom": 2},
                {"left": 0, "top": 0, "right": 2, "bottom": 2},
            ],
            [96, 144],
            "changed during rasterization",
            2,
            id="dpi-change",
        ),
        pytest.param(
            [
                {"left": 0, "top": 0, "right": 2, "bottom": 2},
                RuntimeError("Evidence capture requires actual client geometry"),
            ],
            [96],
            "actual client geometry",
            1,
            id="invalid-post-snapshot",
        ),
    ],
)
def test_capture_window_evidence_rejects_an_unstable_direct_capture_snapshot(
    monkeypatch,
    client_rects,
    dpis,
    error: str,
    expected_dpi_reads: int,
) -> None:
    from netcoredbg_mcp.ui import screenshot

    rect_values = iter(client_rects)
    dpi_values = iter(dpis)
    rect_reads = 0
    dpi_reads = 0

    def get_client_rect(_hwnd: int) -> dict[str, int]:
        nonlocal rect_reads
        rect_reads += 1
        value = next(rect_values)
        if isinstance(value, Exception):
            raise value
        return value

    def get_window_dpi(_hwnd: int) -> int:
        nonlocal dpi_reads
        dpi_reads += 1
        return next(dpi_values)

    class FakeUser32:
        def GetDC(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 100

        def PrintWindow(self, _hwnd, _hdc, _flags):  # noqa: N802 - Win32 API shape
            return True

        def ReleaseDC(self, _hwnd, _hdc):  # noqa: N802 - Win32 API shape
            return 1

    class FakeGdi32:
        def __init__(self) -> None:
            self.bitmap_sizes: list[tuple[int, int]] = []

        def CreateCompatibleDC(self, _wdc):  # noqa: N802 - Win32 API shape
            return 200

        def CreateCompatibleBitmap(self, _wdc, width, height):  # noqa: N802 - Win32 API shape
            self.bitmap_sizes.append((width, height))
            return 300

        def SelectObject(self, _cdc, _bitmap):  # noqa: N802 - Win32 API shape
            return 400

        def GetDIBits(self, _cdc, _bitmap, _start, height, _buffer, _bmi, _usage):  # noqa: N802
            return height

        def DeleteObject(self, _bitmap):  # noqa: N802 - Win32 API shape
            return True

        def DeleteDC(self, _cdc):  # noqa: N802 - Win32 API shape
            return True

    fake_gdi32 = FakeGdi32()
    monkeypatch.setattr(screenshot, "_get_client_rect", get_client_rect)
    monkeypatch.setattr(screenshot, "_get_window_dpi", get_window_dpi)
    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32(), gdi32=fake_gdi32),
        raising=False,
    )

    with pytest.raises(RuntimeError, match=error):
        screenshot.capture_window_evidence(123)

    assert fake_gdi32.bitmap_sizes == [(2, 2)]
    assert rect_reads == 2
    assert dpi_reads == expected_dpi_reads


@pytest.mark.parametrize("dpi_case", ["absent", "raises"])
def test_capture_window_evidence_uses_client_pixels_and_legacy_capture_ignores_dpi(
    monkeypatch,
    dpi_case: str,
) -> None:
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
        def __init__(self) -> None:
            self.printwindow_flags: list[int] = []

        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.left = 0
            rect.top = 0
            rect.right = width
            rect.bottom = height
            return True

        def GetDC(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 100

        def PrintWindow(self, _hwnd, _hdc, flags):  # noqa: N802 - Win32 API shape
            self.printwindow_flags.append(flags)
            return True

        def GetDpiForWindow(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 96

        def ReleaseDC(self, _hwnd, _hdc):  # noqa: N802 - Win32 API shape
            return 1

    class FakeGdi32:
        def __init__(self) -> None:
            self.bitmap_sizes: list[tuple[int, int]] = []

        def CreateCompatibleDC(self, _wdc):  # noqa: N802 - Win32 API shape
            return 200

        def CreateCompatibleBitmap(self, _wdc, bitmap_width, bitmap_height):  # noqa: N802
            self.bitmap_sizes.append((bitmap_width, bitmap_height))
            return 300

        def SelectObject(self, _cdc, _bitmap):  # noqa: N802 - Win32 API shape
            return 400

        def BitBlt(self, *_args):  # noqa: N802 - Win32 API shape
            return True

        def GetDIBits(self, _cdc, _bitmap, _start, _lines, buffer, _bmi, _usage):  # noqa: N802
            ctypes.memmove(buffer, top_down_bgra, len(top_down_bgra))
            return height

        def DeleteObject(self, _bitmap):  # noqa: N802 - Win32 API shape
            return True

        def DeleteDC(self, _cdc):  # noqa: N802 - Win32 API shape
            return True

    fake_user32 = FakeUser32()
    fake_gdi32 = FakeGdi32()
    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=fake_user32, gdi32=fake_gdi32),
        raising=False,
    )

    png_bytes, captured_width, captured_height, metadata = screenshot.capture_window_evidence(123)

    image = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    assert (captured_width, captured_height) == (width, height)
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((1, 0)) == (0, 255, 0, 255)
    assert image.getpixel((0, 1)) == (0, 0, 255, 255)
    assert image.getpixel((1, 1)) == (255, 255, 255, 255)
    assert metadata == {
        "method": "PrintWindow",
        "hwnd": 123,
        "client_rect": {"left": 0, "top": 0, "right": 2, "bottom": 2},
        "dpi": 96,
        "dpi_scale": 1.0,
        "physical_width": 2,
        "physical_height": 2,
        "logical_width": 2.0,
        "logical_height": 2.0,
    }
    assert fake_user32.printwindow_flags == [0x00000003]
    assert fake_gdi32.bitmap_sizes == [(width, height)]

    if dpi_case == "absent":
        monkeypatch.delattr(FakeUser32, "GetDpiForWindow")
    else:

        def unavailable_dpi(_hwnd):  # noqa: N802 - Win32 API shape
            raise OSError("GetDpiForWindow unavailable")

        monkeypatch.setattr(fake_user32, "GetDpiForWindow", unavailable_dpi)

    legacy_capture = screenshot.capture_window(123)
    assert len(legacy_capture) == 3
    assert legacy_capture[1:] == (width, height)
    assert fake_user32.printwindow_flags == [0x00000003, 0x00000002]
    assert fake_gdi32.bitmap_sizes == [(width, height), (width, height)]


def test_build_capture_metadata_uses_actual_client_rect_and_window_dpi_scaling(monkeypatch) -> None:
    from netcoredbg_mcp.ui import screenshot

    class FakeUser32:
        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.left = 0
            rect.top = 0
            rect.right = 180
            rect.bottom = 90
            return True

        def GetDpiForWindow(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 144

    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32()),
        raising=False,
    )

    metadata = screenshot.build_capture_metadata(123, 300, 150, "PrintWindow")

    assert metadata["dpi"] == 144
    assert metadata["dpi_scale"] == 1.5
    assert metadata["client_rect"] == {"left": 0, "top": 0, "right": 180, "bottom": 90}
    assert metadata["physical_width"] == 300
    assert metadata["physical_height"] == 150
    assert metadata["logical_width"] == 200.0
    assert metadata["logical_height"] == 100.0


@pytest.mark.parametrize(
    "dpi_case",
    [
        pytest.param("unavailable", id="unavailable"),
        pytest.param("zero", id="zero"),
        pytest.param("raises", id="raises"),
    ],
)
def test_build_capture_metadata_fails_closed_without_a_positive_window_dpi(
    monkeypatch,
    dpi_case: str,
) -> None:
    from netcoredbg_mcp.ui import screenshot

    def get_client_rect(_hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
        rect = rect_ptr._obj
        rect.left = 0
        rect.top = 0
        rect.right = 180
        rect.bottom = 90
        return True

    user32 = SimpleNamespace(GetClientRect=get_client_rect)
    if dpi_case == "zero":
        user32.GetDpiForWindow = lambda _hwnd: 0
    elif dpi_case == "raises":

        def get_dpi_for_window(_hwnd):  # noqa: N802 - Win32 API shape
            raise OSError("GetDpiForWindow unavailable")

        user32.GetDpiForWindow = get_dpi_for_window

    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=user32),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="actual window DPI"):
        screenshot.build_capture_metadata(123, 300, 150, "PrintWindow")


@pytest.mark.parametrize(
    ("result", "right", "bottom"),
    [(False, 10, 10), (True, 0, 10), (True, 10, 0)],
)
def test_build_capture_metadata_rejects_unavailable_or_invalid_client_geometry(
    monkeypatch,
    result: bool,
    right: int,
    bottom: int,
) -> None:
    from netcoredbg_mcp.ui import screenshot

    class FakeUser32:
        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.right = right
            rect.bottom = bottom
            return result

    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32()),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="actual client geometry"):
        screenshot.build_capture_metadata(123, 300, 150, "PrintWindow")


def test_capture_window_evidence_rejects_printwindow_failure_without_bitblt(monkeypatch) -> None:
    from netcoredbg_mcp.ui import screenshot

    class FakeUser32:
        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.right = 1
            rect.bottom = 1
            return True

        def GetDC(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 100

        def PrintWindow(self, _hwnd, _hdc, _flags):  # noqa: N802 - Win32 API shape
            return False

        def GetDpiForWindow(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 96

        def ReleaseDC(self, _hwnd, _hdc):  # noqa: N802 - Win32 API shape
            return 1

    class FakeGdi32:
        def __init__(self) -> None:
            self.bitblt_calls: list[tuple[int, ...]] = []

        def CreateCompatibleDC(self, _wdc):  # noqa: N802 - Win32 API shape
            return 200

        def CreateCompatibleBitmap(self, _wdc, _width, _height):  # noqa: N802 - Win32 API shape
            return 300

        def SelectObject(self, _cdc, _bitmap):  # noqa: N802 - Win32 API shape
            return 400

        def BitBlt(self, *args):  # noqa: N802 - Win32 API shape
            self.bitblt_calls.append(args)
            return True

        def GetDIBits(self, _cdc, _bitmap, _start, _lines, buffer, _bmi, _usage):  # noqa: N802
            ctypes.memmove(buffer, _bgra(255, 0, 0), 4)
            return 1

        def DeleteObject(self, _bitmap):  # noqa: N802 - Win32 API shape
            return True

        def DeleteDC(self, _cdc):  # noqa: N802 - Win32 API shape
            return True

    fake_gdi32 = FakeGdi32()
    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32(), gdi32=fake_gdi32),
        raising=False,
    )

    _png_bytes, width, height = screenshot.capture_window(123)

    assert (width, height) == (1, 1)
    assert fake_gdi32.bitblt_calls == [(200, 0, 0, 1, 1, 100, 0, 0, 0x00CC0020)]
    fake_gdi32.bitblt_calls.clear()

    with pytest.raises(RuntimeError, match="Evidence capture requires a PrintWindow raster"):
        screenshot.capture_window_evidence(123)

    assert fake_gdi32.bitblt_calls == []


@pytest.mark.parametrize(
    (
        "capture_name",
        "printwindow_success",
        "bitblt_success",
        "scan_lines",
        "error",
        "expect_getdibits",
    ),
    [
        pytest.param(
            "capture_window",
            False,
            False,
            2,
            "BitBlt fallback failed",
            False,
            id="legacy-bitblt-failure",
        ),
        pytest.param(
            "capture_window",
            True,
            True,
            0,
            "GetDIBits returned 0 scan lines; expected 2",
            True,
            id="legacy-getdibits-zero",
        ),
        pytest.param(
            "capture_window_evidence",
            True,
            True,
            0,
            "GetDIBits returned 0 scan lines; expected 2",
            True,
            id="evidence-getdibits-zero",
        ),
        pytest.param(
            "capture_window",
            True,
            True,
            1,
            "GetDIBits returned 1 scan lines; expected 2",
            True,
            id="legacy-getdibits-short",
        ),
        pytest.param(
            "capture_window_evidence",
            True,
            True,
            1,
            "GetDIBits returned 1 scan lines; expected 2",
            True,
            id="evidence-getdibits-short",
        ),
    ],
)
def test_capture_window_rejects_incomplete_gdi_raster(
    monkeypatch,
    capture_name: str,
    printwindow_success: bool,
    bitblt_success: bool,
    scan_lines: int,
    error: str,
    expect_getdibits: bool,
) -> None:
    from netcoredbg_mcp.ui import screenshot

    class FakeUser32:
        def GetClientRect(self, _hwnd, rect_ptr):  # noqa: N802 - Win32 API shape
            rect = rect_ptr._obj
            rect.right = 1
            rect.bottom = 2
            return True

        def GetDpiForWindow(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 96

        def GetDC(self, _hwnd):  # noqa: N802 - Win32 API shape
            return 100

        def PrintWindow(self, _hwnd, _hdc, _flags):  # noqa: N802 - Win32 API shape
            return printwindow_success

        def ReleaseDC(self, _hwnd, _hdc):  # noqa: N802 - Win32 API shape
            return 1

    class FakeGdi32:
        def __init__(self) -> None:
            self.bitblt_calls = 0
            self.getdibits_calls = 0

        def CreateCompatibleDC(self, _wdc):  # noqa: N802 - Win32 API shape
            return 200

        def CreateCompatibleBitmap(self, _wdc, _width, _height):  # noqa: N802 - Win32 API shape
            return 300

        def SelectObject(self, _cdc, _bitmap):  # noqa: N802 - Win32 API shape
            return 400

        def BitBlt(self, *_args):  # noqa: N802 - Win32 API shape
            self.bitblt_calls += 1
            return bitblt_success

        def GetDIBits(self, _cdc, _bitmap, _start, _lines, buffer, _bmi, _usage):  # noqa: N802 - Win32 API shape
            self.getdibits_calls += 1
            ctypes.memmove(buffer, _bgra(255, 0, 0) * 2, 8)
            return scan_lines

        def DeleteObject(self, _bitmap):  # noqa: N802 - Win32 API shape
            return True

        def DeleteDC(self, _cdc):  # noqa: N802 - Win32 API shape
            return True

    fake_gdi32 = FakeGdi32()
    monkeypatch.setattr(
        screenshot.ctypes,
        "windll",
        SimpleNamespace(user32=FakeUser32(), gdi32=fake_gdi32),
        raising=False,
    )
    if expect_getdibits:
        monkeypatch.setattr(
            Image,
            "frombuffer",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("incomplete GDI raster must not reach PNG creation")
            ),
        )

    capture = getattr(screenshot, capture_name)
    with pytest.raises(RuntimeError, match=error):
        capture(123)

    assert fake_gdi32.bitblt_calls == int(not printwindow_success)
    assert fake_gdi32.getdibits_calls == int(expect_getdibits)


def test_crop_png_preserves_selected_pixels_and_dimensions() -> None:
    from netcoredbg_mcp.ui.screenshot import crop_png

    source = Image.new("RGB", (3, 2))
    source.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (255, 0, 255),
            (0, 255, 255),
        ]
    )
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")

    cropped_bytes, width, height = crop_png(buffer.getvalue(), 1, 0, 2, 2)

    cropped = Image.open(io.BytesIO(cropped_bytes))
    assert (width, height) == (2, 2)
    assert cropped.size == (2, 2)
    assert [cropped.getpixel((x, y)) for y in range(2) for x in range(2)] == [
        (0, 255, 0),
        (0, 0, 255),
        (255, 0, 255),
        (0, 255, 255),
    ]


@pytest.mark.parametrize(
    "rectangle",
    [(-1, 0, 1, 1), (0, 0, 0, 1), (0, 0, 1, 0), (2, 0, 2, 1), (0, 1, 1, 2)],
)
def test_crop_png_rejects_invalid_rectangles(rectangle: tuple[int, int, int, int]) -> None:
    from netcoredbg_mcp.ui.screenshot import crop_png

    png = _png((255, 0, 0), (2, 2))
    with pytest.raises(
        ValueError, match=r"^Crop rectangle must be positive and within image bounds$"
    ):
        crop_png(png, *rectangle)


@pytest.mark.asyncio
async def test_non_stealth_resize_mismatch_then_default_evidence_is_unasserted(
    capturing_mcp,
    monkeypatch,
    tmp_path,
) -> None:
    """The public non-stealth route must not invent strict target comparability."""
    from unittest.mock import AsyncMock, patch

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    png = _png((255, 255, 255), (300, 150))
    saved_names: list[str] = []

    def save_screenshot_bundle(
        _sid: str,
        raw: bytes,
        raw_name: str,
        _crop: bytes | None,
        _crop_name: str | None,
    ) -> tuple[object, None]:
        saved_names.append(raw_name)
        path = tmp_path / raw_name
        path.write_bytes(raw)
        return path, None

    def save_screenshot(_sid: str, data: bytes, name: str):
        saved_names.append(name)
        path = tmp_path / name
        path.write_bytes(data)
        return path

    backend = SimpleNamespace(
        process_id=42,
        resize_window=AsyncMock(
            return_value={
                "resized": True,
                "target_comparability": {"status": "MISMATCH"},
            }
        ),
        get_window_tree=AsyncMock(return_value={"windows": [{"name": "WpfSmokeApp"}], "count": 1}),
    )
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 123)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
        lambda _hwnd: (
            png,
            300,
            150,
            {
                "method": "PrintWindow",
                "hwnd": 123,
                "client_rect": {"left": 0, "top": 0, "right": 300, "bottom": 150},
                "dpi": 144,
                "dpi_scale": 1.5,
                "physical_width": 300,
                "physical_height": 150,
                "logical_width": 200.0,
                "logical_height": 100.0,
            },
        ),
    )
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="evidence-session",
        temp_manager=SimpleNamespace(
            save_screenshot_bundle=save_screenshot_bundle,
            save_screenshot=save_screenshot,
        ),
    )

    with patch("netcoredbg_mcp.ui.backend.create_backend", return_value=backend):
        register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
        resize = await capturing_mcp.tools["ui_resize_window"](
            SimpleNamespace(), width=200, height=100
        )
        tree = await capturing_mcp.tools["ui_get_window_tree"]()
        screenshot = await capturing_mcp.tools["ui_take_screenshot"](
            SimpleNamespace(), evidence=True
        )

    assert resize["data"]["target_comparability"]["status"] == "MISMATCH"
    assert tree["data"]["count"] == 1
    metadata = json.loads(screenshot[-1].text)
    assert metadata["target_comparability"] == {"status": "UNASSERTED"}
    assert "physical_target" not in metadata
    assert saved_names


def _compositor_result(png: bytes, width: int = 64, height: int = 64) -> dict:
    import base64
    import copy

    client = {
        "left": 0,
        "top": 0,
        "right": width,
        "bottom": height,
        "unit": "physical_px",
        "coordinate_space": "client",
        "source_api": "GetClientRect",
    }
    bounds = {
        "left": 100,
        "top": 50,
        "right": 100 + width,
        "bottom": 50 + height,
        "unit": "physical_px",
        "coordinate_space": "screen",
        "source_api": "GetWindowRect",
    }
    snapshot = {
        "hwnd": 777,
        "process_id": 42,
        "process_creation_ticks": 123456,
        "session_id": 1,
        "client_rect": client,
        "window_bounds": bounds,
        "dpi": 144,
    }
    return {
        "base64": base64.b64encode(png).decode("ascii"),
        "width": width,
        "height": height,
        "method": "WindowsGraphicsCapture",
        "evidence_grade": "compositor_window_raster",
        "source_api": "GraphicsCaptureItem.CreateForWindow",
        "capture_authority": "hwnd_compositor_frame",
        "fallback_reason": "probable_black_printwindow",
        "alternate_attempts": 1,
        "printwindow_classification": "probable_black_discarded",
        "printwindow_variance": 0.0,
        "printwindow_analysis": {"classification": "PROBABLE_BLACK_FRAME", "variance": 0.0},
        "hwnd": 777,
        "process_id": 42,
        "process_creation_ticks": 123456,
        "connected_creation_ticks": 123456,
        "capture_policy": {"status": "ALLOWED", "affinity": 0},
        "session_id": 1,
        "client_rect": client,
        "window_bounds": bounds,
        "dpi": 144,
        "capture_stability": {"before": copy.deepcopy(snapshot), "after": copy.deepcopy(snapshot)},
        "foreground": {"mutation_attempted": False},
        "bridge_assembly": {"path": "C:/bridge/FlaUIBridge.dll", "sha256": "0" * 64},
        "compositor": {
            "frame_time_ticks": 123,
            "item_size": {"width": width, "height": height},
            "frame_content_size": {"width": width, "height": height},
            "policy": {"status": "ALLOWED", "affinity": 0},
            "process_identity": {
                "connected_creation_ticks": 123456,
                "before_creation_ticks": 123456,
                "after_creation_ticks": 123456,
                "process_id": 42,
                "session_id": 1,
            },
            "window_closed": False,
        },
    }


def _compositor_tool(capturing_mcp, monkeypatch, tmp_path, result):
    from unittest.mock import AsyncMock, Mock

    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    backend = FlaUIBackend.__new__(FlaUIBackend)
    backend._process_id = 42
    backend._client = SimpleNamespace(call=AsyncMock(return_value=result), is_running=True)
    saved = []

    def save(_sid, data, name):
        saved.append(name)
        path = tmp_path / name
        path.write_bytes(data)
        return path

    def bundle(sid, raw, raw_name, crop, crop_name):
        return save(sid, raw, raw_name), save(sid, crop, crop_name) if crop is not None else None

    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="compositor",
        temp_manager=SimpleNamespace(save_screenshot=save, save_screenshot_bundle=bundle),
        wait_for_pending_stealth_foreground_restore=AsyncMock(return_value=False),
        cancel_pending_stealth_foreground_restore=AsyncMock(),
    )
    factory = Mock(return_value=backend)
    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", factory)
    from netcoredbg_mcp.ui.screenshot import build_capture_metadata

    other_png = _png((220, 40, 90))
    other_metadata = build_capture_metadata(
        888,
        64,
        64,
        "PrintWindow",
        client_rect={
            "left": 0,
            "top": 0,
            "right": 64,
            "bottom": 64,
            "unit": "physical_px",
            "coordinate_space": "client",
            "source_api": "GetClientRect",
        },
        dpi=144,
    )
    other_metadata.update(
        process_id=42,
        window_bounds={
            "left": 400,
            "top": 300,
            "right": 464,
            "bottom": 364,
            "unit": "physical_px",
            "coordinate_space": "screen",
            "source_api": "GetWindowRect",
        },
    )
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 888)
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window",
        lambda _hwnd: (other_png, 64, 64),
    )
    monkeypatch.setattr(
        "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
        lambda _hwnd: (other_png, 64, 64, other_metadata),
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
    return capturing_mcp.tools["ui_take_screenshot"], backend, session, saved, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [False, True])
async def test_compositor_capture_uses_one_source_for_raw_crop_and_inline(
    capturing_mcp, monkeypatch, tmp_path, evidence
) -> None:
    import base64
    from pathlib import Path

    image = Image.new("RGB", (64, 64), "white")
    for x in range(8, 24):
        for y in range(8, 24):
            image.putpixel((x, y), (18, 192, 222))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()
    tool, _, _, saved, _ = _compositor_tool(
        capturing_mcp, monkeypatch, tmp_path, _compositor_result(png)
    )
    arguments = {"evidence": evidence, "format": "png"}
    if evidence:
        arguments.update(crop_x=8, crop_y=8, crop_width=16, crop_height=16)
    content = await tool(SimpleNamespace(), **arguments)
    assert isinstance(content, list), content
    metadata = json.loads(content[-1].text)
    with Image.open(io.BytesIO(base64.b64decode(content[0].data, validate=True))) as inline:
        marker = inline.convert("RGB").getpixel((16, 16))
        assert all(abs(actual - expected) <= 8 for actual, expected in zip(marker, (18, 192, 222)))
        assert min(inline.convert("RGB").getpixel((48, 48))) >= 245
    assert metadata["method"] == "WindowsGraphicsCapture"
    assert metadata["hwnd"] == 777 and metadata["process_id"] == 42
    assert metadata["evidence_grade"] == (
        "compositor_window_raster" if evidence else "preview_only"
    )
    assert metadata["foreground"] == {"mutation_attempted": False}
    assert "hd_path" in metadata
    if evidence:
        assert Path(metadata["raw_path"]).read_bytes() == png
        with Image.open(metadata["crop_path"]) as crop:
            assert crop.size == (16, 16) and crop.getpixel((8, 8)) == (18, 192, 222)
        assert metadata["capture_metadata"]["compositor"] == metadata["compositor"]
    else:
        assert not {"raw_path", "crop_path", "raw_sha256"}.intersection(metadata)
        assert len(saved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("method",), "BitBlt"),
        (("source_api",), "CreateForMonitor"),
        (("capture_authority",), "foreground_window_gdi_raster"),
        (("evidence_grade",), "lossless_raster"),
        (("hwnd",), 778),
        (("process_id",), 43),
        (("process_creation_ticks",), 0),
        (("connected_creation_ticks",), 123457),
        (("capture_policy", "status"), "UNKNOWN"),
        (("capture_policy", "affinity"), False),
        (("session_id",), True),
        (("capture_stability", "after", "hwnd"), 778),
        (("capture_stability", "after", "process_creation_ticks"), 123457),
        (("capture_stability", "after", "session_id"), 2),
        (("capture_stability", "after", "dpi"), 96),
        (("capture_stability", "after", "client_rect", "right"), 63),
        (("capture_stability", "after", "client_rect", "left"), False),
        (("capture_stability", "after", "window_bounds", "right"), 163),
        (("compositor", "frame_time_ticks"), 0),
        (("compositor", "frame_time_ticks"), True),
        (("compositor", "item_size", "width"), 63),
        (("compositor", "frame_content_size", "height"), 63),
        (("compositor", "window_closed"), True),
        (("compositor", "policy", "status"), "UNKNOWN"),
        (("compositor", "policy", "affinity"), 17),
        (("compositor", "policy", "affinity"), False),
        (("compositor", "process_identity", "connected_creation_ticks"), 123457),
        (("compositor", "process_identity", "after_creation_ticks"), 123457),
        (("compositor", "process_identity", "process_id"), 43),
        (("compositor", "process_identity", "session_id"), 2),
        (("foreground", "mutation_attempted"), True),
        (("foreground", "mutation_attempted"), 0),
        (("printwindow_variance",), float("nan")),
    ],
)
async def test_compositor_untrusted_provenance_refuses_image_and_persistence(
    capturing_mcp, monkeypatch, tmp_path, path, value
) -> None:
    result = _compositor_result(_png((255, 255, 255)))
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    tool, _, _, saved, _ = _compositor_tool(capturing_mcp, monkeypatch, tmp_path, result)
    response = await tool(
        SimpleNamespace(),
        evidence=True,
        expected_hwnd=777,
        expected_physical_width=64,
        expected_physical_height=64,
    )
    assert (
        isinstance(response, dict) and response["code"] == "PHYSICAL_CAPTURE_PROVENANCE_UNAVAILABLE"
    )
    assert saved == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_compositor_caller_size_mismatch_keeps_preview_but_publishes_no_evidence(
    capturing_mcp, monkeypatch, tmp_path
) -> None:
    tool, _, _, saved, _ = _compositor_tool(
        capturing_mcp, monkeypatch, tmp_path, _compositor_result(_png((255, 255, 255)))
    )
    content = await tool(
        SimpleNamespace(),
        evidence=True,
        expected_hwnd=777,
        expected_physical_width=63,
        expected_physical_height=64,
    )
    assert isinstance(content, list), content
    metadata = json.loads(content[-1].text)
    assert metadata["target_comparability"]["status"] == "MISMATCH"
    assert metadata["evidence_grade"] == "preview_only"
    assert not {"raw_path", "crop_path", "hd_path"}.intersection(metadata)
    assert saved == [] and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255)])
async def test_compositor_black_refusal_does_not_classify_legitimate_white_as_invalid(
    capturing_mcp, monkeypatch, tmp_path, color
) -> None:
    tool, _, _, saved, _ = _compositor_tool(
        capturing_mcp, monkeypatch, tmp_path, _compositor_result(_png(color))
    )
    response = await tool(SimpleNamespace(), evidence=True)
    if color == (0, 0, 0):
        assert response["classification"] == "PROBABLE_BLACK_FRAME" and saved == []
    else:
        assert isinstance(response, list), response


@pytest.mark.asyncio
async def test_missing_bridge_preserves_working_ordinary_capture_and_refuses_strict_target(
    capturing_mcp, monkeypatch, tmp_path
) -> None:
    from netcoredbg_mcp.session.manager import DebugState
    from netcoredbg_mcp.tools.ui import register_ui_tools

    png = _png((255, 255, 255))
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", lambda _pid: 777)
    monkeypatch.setattr("netcoredbg_mcp.ui.screenshot.capture_window", lambda _hwnd: (png, 64, 64))
    session = SimpleNamespace(
        process_registry=None,
        state=SimpleNamespace(state=DebugState.RUNNING, process_id=42),
        stealth_mode=False,
        session_id="legacy",
        temp_manager=SimpleNamespace(
            save_screenshot=lambda _sid, data, name: (tmp_path / name).write_bytes(data)
            and tmp_path / name,
        ),
    )
    register_ui_tools(capturing_mcp, session, check_session_access=lambda _ctx: None)
    ordinary = await capturing_mcp.tools["ui_take_screenshot"](SimpleNamespace())
    strict = await capturing_mcp.tools["ui_take_screenshot"](
        SimpleNamespace(),
        evidence=True,
        expected_hwnd=777,
        expected_physical_width=64,
        expected_physical_height=64,
    )
    assert isinstance(ordinary, list), ordinary
    assert strict["code"] == "PHYSICAL_CAPTURE_PROVENANCE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("fabricated_black", [False, True])
async def test_compositor_native_primary_failure_has_no_fabricated_black_provenance(
    capturing_mcp, monkeypatch, tmp_path, fabricated_black
) -> None:
    result = _compositor_result(_png((255, 255, 255)))
    result["fallback_reason"] = "printwindow_failed"
    if not fabricated_black:
        for key in ("printwindow_classification", "printwindow_variance", "printwindow_analysis"):
            del result[key]
    tool, _, _, saved, _ = _compositor_tool(capturing_mcp, monkeypatch, tmp_path, result)
    response = await tool(SimpleNamespace(), evidence=True)
    if fabricated_black:
        assert isinstance(response, dict) and "error" in response and saved == []
    else:
        assert isinstance(response, list), response
        assert json.loads(response[-1].text)["fallback_reason"] == "printwindow_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["invalid!", "bm90IGEgcG5n"])
async def test_compositor_invalid_png_never_publishes_evidence(
    capturing_mcp, monkeypatch, tmp_path, payload
) -> None:
    result = _compositor_result(_png((255, 255, 255)))
    result["base64"] = payload
    tool, _, _, saved, _ = _compositor_tool(capturing_mcp, monkeypatch, tmp_path, result)
    response = await tool(SimpleNamespace(), evidence=True)
    assert response["error"] == "Bridge screenshot raw PNG is invalid" and saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PrintWindow", "WindowsGraphicsCapture"])
async def test_bridge_session_target_changed_in_flight_refuses_publish(
    capturing_mcp, monkeypatch, tmp_path, method
) -> None:
    result = _compositor_result(_png((255, 255, 255)))
    if method == "PrintWindow":
        for key in (
            "compositor",
            "source_api",
            "capture_authority",
            "fallback_reason",
            "printwindow_classification",
            "printwindow_variance",
            "printwindow_analysis",
        ):
            del result[key]
        result.update(method="PrintWindow", flags=2, evidence_grade="lossless_raster")
    tool, backend, session, saved, _ = _compositor_tool(
        capturing_mcp, monkeypatch, tmp_path, result
    )

    async def change_target(*_args):
        session.state.process_id = 43
        return result

    backend._client.call.side_effect = change_target
    response = await tool(SimpleNamespace(), evidence=True)
    assert isinstance(response, dict) and "error" in response and saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PrintWindow", "WindowsGraphicsCapture"])
async def test_bridge_printwindow_and_compositor_refuse_reused_process_identity(
    capturing_mcp, monkeypatch, tmp_path, method
) -> None:
    result = _compositor_result(_png((255, 255, 255)))
    if method == "PrintWindow":
        for key in (
            "compositor",
            "source_api",
            "capture_authority",
            "fallback_reason",
            "printwindow_classification",
            "printwindow_variance",
            "printwindow_analysis",
        ):
            del result[key]
        result.update(method="PrintWindow", flags=2, evidence_grade="lossless_raster")
    result["capture_stability"]["after"]["process_creation_ticks"] = 123457
    tool, _, _, saved, _ = _compositor_tool(capturing_mcp, monkeypatch, tmp_path, result)
    response = await tool(SimpleNamespace(), evidence=True)
    assert isinstance(response, dict) and "error" in response and saved == []


@pytest.mark.asyncio
async def test_screenshot_keeps_selected_window_without_unconditional_reconnect(
    capturing_mcp, monkeypatch, tmp_path
) -> None:
    import base64
    from pathlib import Path
    from unittest.mock import AsyncMock

    selected_png = _png((24, 160, 80))
    other_png = _png((220, 40, 90))
    selected = {"hwnd": 888}
    tool, backend, _, _, _ = _compositor_tool(
        capturing_mcp, monkeypatch, tmp_path, _compositor_result(selected_png)
    )

    async def switch_window(*, name=None, automation_id=None):
        selected["hwnd"] = 777 if name == "Selected fixture" else 888
        return {"switched": True, "hwnd": selected["hwnd"]}

    async def connect_backend(ui, process_id, **_kwargs):
        ui._process_id = process_id
        selected["hwnd"] = 888

    async def capture_selected(_method, _request):
        result = _compositor_result(selected_png if selected["hwnd"] == 777 else other_png)
        result["hwnd"] = selected["hwnd"]
        for snapshot in result["capture_stability"].values():
            snapshot["hwnd"] = selected["hwnd"]
        return result

    backend.switch_window = AsyncMock(side_effect=switch_window)
    backend._client.call = AsyncMock(side_effect=capture_selected)
    monkeypatch.setattr("netcoredbg_mcp.ui.backend.connect_backend", connect_backend)
    switched = await capturing_mcp.tools["ui_switch_window"](
        SimpleNamespace(), name="Selected fixture"
    )
    response = await tool(SimpleNamespace(), evidence=True)
    assert switched["data"]["switched"] is True
    assert isinstance(response, list), response
    metadata = json.loads(response[-1].text)
    assert metadata["hwnd"] == 777 and metadata["process_id"] == 42
    assert Path(metadata["raw_path"]).read_bytes() == selected_png
    with Image.open(io.BytesIO(base64.b64decode(response[0].data, validate=True))) as inline:
        pixel = inline.convert("RGB").getpixel((16, 16))
        assert all(abs(actual - expected) <= 8 for actual, expected in zip(pixel, (24, 160, 80)))

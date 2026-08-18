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


def test_capture_window_evidence_reports_bitblt_fallback(monkeypatch) -> None:
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

        def GetDIBits(self, _cdc, _bitmap, _start, _lines, buffer, _bmi, _usage):  # noqa: N802 - Win32 API shape
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

    _png_bytes, width, height, metadata = screenshot.capture_window_evidence(123)

    assert (width, height) == (1, 1)
    assert fake_gdi32.bitblt_calls == [(200, 0, 0, 1, 1, 100, 0, 0, 0x00CC0020)]
    assert metadata["method"] == "BitBlt"


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
            "capture_window_evidence",
            False,
            False,
            2,
            "BitBlt fallback failed",
            False,
            id="evidence-bitblt-failure",
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

    with pytest.raises(RuntimeError, match=error):
        getattr(screenshot, capture_name)(123)

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

    with pytest.raises(
        ValueError, match=r"^Crop rectangle must be positive and within image bounds$"
    ):
        crop_png(_png((255, 0, 0), (2, 2)), *rectangle)

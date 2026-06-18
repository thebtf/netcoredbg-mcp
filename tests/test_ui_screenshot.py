"""Screenshot capture and annotation tests."""

from __future__ import annotations

import ctypes
import io
from types import SimpleNamespace

from PIL import Image


def _bgra(r: int, g: int, b: int, a: int = 255) -> bytes:
    return bytes((b, g, r, a))


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

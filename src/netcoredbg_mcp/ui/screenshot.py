"""Window screenshot capture and annotation for AI agent visual access.

Provides:
- Window-specific screenshot via Win32 PrintWindow (no foreground required)
- Set-of-Mark (SoM) annotation overlay with numbered element boxes
- Element collection from UIA tree with bounding rectangles
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

# Screenshot defaults — configurable via environment variables.
# NETCOREDBG_SCREENSHOT_MAX_WIDTH: Max inline preview width (default 1280, Claude vision optimal).
# NETCOREDBG_SCREENSHOT_QUALITY: WebP/JPEG quality 1-100 (default 80).
# Claude vision auto-downsamples above 1568px, so values > 1568 waste bandwidth.
SCREENSHOT_MAX_WIDTH = int(os.environ.get("NETCOREDBG_SCREENSHOT_MAX_WIDTH", "1280"))
SCREENSHOT_QUALITY = int(os.environ.get("NETCOREDBG_SCREENSHOT_QUALITY", "80"))


def create_preview(
    image_data: bytes,
    max_width: int = 1280,
    quality: int = 80,
) -> tuple[bytes, int, int]:
    """Create a WebP preview from image data (PNG or raw).

    Args:
        image_data: Source image bytes (PNG format)
        max_width: Maximum preview width (default 1280px — Claude vision optimal)
        quality: WebP quality 1-100 (default 80)

    Returns:
        Tuple of (webp_bytes, width, height).
    """
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(image_data))

    # Downscale if wider than max_width
    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.LANCZOS)

    # Convert to WebP
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    webp_bytes = buf.getvalue()

    return webp_bytes, img.width, img.height


def get_hwnd_for_pid(pid: int) -> int | None:
    """Find the main window HWND for a process ID.

    Enumerates all top-level windows and returns the first visible one
    belonging to the given PID.

    Args:
        pid: Process ID

    Returns:
        HWND as integer, or None if no window found
    """
    user32 = ctypes.windll.user32

    result_hwnd = None

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_callback(hwnd, _lparam):
        nonlocal result_hwnd
        # Check if window belongs to our process
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value != pid:
            return True  # Continue enumeration

        # Check if window is visible and has a title
        if not user32.IsWindowVisible(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            result_hwnd = hwnd
            return False  # Stop enumeration

        return True

    user32.EnumWindows(enum_callback, 0)
    return result_hwnd


def capture_window(hwnd: int) -> tuple[bytes, int, int]:
    """Capture a window screenshot via Win32 PrintWindow.

    Works even if the window is partially obscured by other windows.

    Args:
        hwnd: Window handle (HWND)

    Returns:
        Tuple of (png_bytes, width, height)

    Raises:
        RuntimeError: If capture fails
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    # Get window dimensions (client area for content, full rect for chrome)
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    if width <= 0 or height <= 0:
        raise RuntimeError(f"Window has invalid dimensions: {width}x{height}")

    # Create compatible DC and bitmap
    wdc = user32.GetDC(hwnd)
    if not wdc:
        raise RuntimeError("Failed to get window DC")

    try:
        cdc = gdi32.CreateCompatibleDC(wdc)
        if not cdc:
            raise RuntimeError("Failed to create compatible DC")

        try:
            bitmap = gdi32.CreateCompatibleBitmap(wdc, width, height)
            if not bitmap:
                raise RuntimeError("Failed to create compatible bitmap")

            try:
                old_bitmap = gdi32.SelectObject(cdc, bitmap)

                try:
                    # PrintWindow with PW_RENDERFULLCONTENT for best results
                    PW_RENDERFULLCONTENT = 0x00000002
                    success = user32.PrintWindow(hwnd, cdc, PW_RENDERFULLCONTENT)
                    if not success:
                        # Fallback: try BitBlt
                        gdi32.BitBlt(cdc, 0, 0, width, height, wdc, 0, 0, 0x00CC0020)

                    # Extract bitmap bits
                    from PIL import Image

                    bmi = _create_bitmapinfo(width, height)
                    buffer = ctypes.create_string_buffer(width * height * 4)

                    gdi32.GetDIBits(
                        cdc, bitmap, 0, height,
                        buffer, ctypes.byref(bmi),
                        0,  # DIB_RGB_COLORS
                    )

                    # Convert BGRA → RGBA
                    image = Image.frombuffer(
                        "RGBA", (width, height), buffer, "raw", "BGRA", 0, -1
                    )

                    # Encode to PNG
                    png_buffer = io.BytesIO()
                    image.save(png_buffer, format="PNG", optimize=True)
                    return png_buffer.getvalue(), width, height

                finally:
                    # Always deselect before DeleteObject to prevent GDI leak
                    gdi32.SelectObject(cdc, old_bitmap)

            finally:
                gdi32.DeleteObject(bitmap)
        finally:
            gdi32.DeleteDC(cdc)
    finally:
        user32.ReleaseDC(hwnd, wdc)


def _create_bitmapinfo(width: int, height: int) -> ctypes.Structure:
    """Create a BITMAPINFO structure for 32-bit BGRA."""

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
        ]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # Top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    return bmi


def _process_screenshot(
    raw_png_bytes: bytes,
    max_width: int = SCREENSHOT_MAX_WIDTH,
    format: str = "webp",
    quality: int = SCREENSHOT_QUALITY,
) -> tuple[bytes, int, int, str]:
    """Downsample and convert screenshot to optimal format for LLM consumption.

    Uses WebP by default — 25-35% smaller than JPEG with better text/edge quality.
    Configurable via NETCOREDBG_SCREENSHOT_MAX_WIDTH and NETCOREDBG_SCREENSHOT_QUALITY.

    Args:
        raw_png_bytes: Raw PNG capture from Win32
        max_width: Maximum width (default from NETCOREDBG_SCREENSHOT_MAX_WIDTH)
        format: Output format: "webp" (recommended), "jpeg", "png"
        quality: Compression quality for lossy formats (default from NETCOREDBG_SCREENSHOT_QUALITY)

    Returns:
        Tuple of (image_bytes, final_width, final_height, mime_type)
    """
    from PIL import Image

    image = Image.open(io.BytesIO(raw_png_bytes))

    # Downsample if needed
    if image.width > max_width:
        ratio = max_width / image.width
        new_height = int(image.height * ratio)
        image = image.resize((max_width, new_height), Image.LANCZOS)

    width, height = image.size
    buffer = io.BytesIO()

    if format == "webp":
        image.save(buffer, format="WEBP", quality=quality)
        mime = "image/webp"
    elif format == "jpeg":
        # Convert RGBA→RGB for JPEG
        if image.mode == "RGBA":
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=quality)
        mime = "image/jpeg"
    else:
        image.save(buffer, format="PNG", optimize=True)
        mime = "image/png"

    return buffer.getvalue(), width, height, mime


def capture_window_for_llm(
    hwnd: int,
    max_width: int = SCREENSHOT_MAX_WIDTH,
    format: str = "webp",
    quality: int = SCREENSHOT_QUALITY,
    save_dir: str | None = None,
) -> dict[str, Any]:
    """Capture window screenshot optimized for LLM consumption.

    Returns base64 image + optional file path. Uses WebP at 1024px by default
    to stay within MCP size limits while preserving visual quality.

    Args:
        hwnd: Window handle
        max_width: Maximum width (default 1024)
        format: Image format: "webp", "jpeg", "png"
        quality: Compression quality for lossy (default 75)
        save_dir: Directory to save full file (optional)

    Returns:
        Dict with image (base64), width, height, format, mimeType, and optionally filePath
    """
    import os
    import tempfile
    import time

    raw_png, orig_w, orig_h = capture_window(hwnd)
    img_bytes, width, height, mime = _process_screenshot(raw_png, max_width, format, quality)

    result: dict[str, Any] = {
        "image": base64.b64encode(img_bytes).decode("ascii"),
        "width": width,
        "height": height,
        "format": format,
        "mimeType": mime,
        "sizeBytes": len(img_bytes),
    }

    # Save to file if directory provided
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        ext = {"webp": ".webp", "jpeg": ".jpg", "png": ".png"}.get(format, ".webp")
        filename = f"screenshot_{int(time.time())}{ext}"
        filepath = os.path.join(save_dir, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        result["filePath"] = filepath

    return result


# Keep backward-compatible alias
def capture_window_as_base64(
    hwnd: int, max_width: int = SCREENSHOT_MAX_WIDTH,
) -> dict[str, Any]:
    """Legacy alias for capture_window_for_llm."""
    return capture_window_for_llm(hwnd, max_width=max_width)


def collect_visible_elements(
    app,
    max_depth: int = 3,
    interactive_only: bool = True,
) -> list[dict[str, Any]]:
    """Walk UIA tree and collect visible elements with bounding rectangles.

    Args:
        app: pywinauto Application object
        max_depth: Maximum depth to traverse
        interactive_only: Only collect interactive controls (buttons, textboxes, etc.)

    Returns:
        List of element dicts with id, name, type, automationId, bounds
    """
    INTERACTIVE_TYPES = {
        "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink",
        "ListItem", "MenuItem", "RadioButton", "ScrollBar",
        "Slider", "Spinner", "TabItem", "Text", "TextBox",
        "ToggleButton", "TreeItem", "DataItem",
    }

    elements: list[dict[str, Any]] = []
    element_id = 0

    def _walk(control, depth: int):
        nonlocal element_id

        if depth > max_depth:
            return

        try:
            info = control.element_info
            control_type = getattr(info, "control_type", "") or ""
            name = getattr(info, "name", "") or ""
            auto_id = getattr(info, "automation_id", "") or ""

            # Get bounding rectangle
            rect = getattr(info, "rectangle", None)
            if rect is None:
                return

            bounds = {
                "x": rect.left,
                "y": rect.top,
                "width": rect.width(),
                "height": rect.height(),
            }

            # Skip adding zero-size elements, but still walk their children
            # (interactive elements may be nested inside zero-size containers)
            if bounds["width"] > 0 and bounds["height"] > 0:
                # Filter by interactivity
                if interactive_only and control_type not in INTERACTIVE_TYPES:
                    # Still walk children — interactive elements may be nested
                    pass
                else:
                    element_id += 1
                    elements.append({
                        "id": element_id,
                        "name": name,
                        "type": control_type,
                        "automationId": auto_id,
                        "bounds": bounds,
                    })

        except Exception:
            logger.debug(f"Failed to read element info at depth {depth}", exc_info=True)

        # Walk children (always, regardless of this element's bounds)
        try:
            for child in control.children():
                _walk(child, depth + 1)
        except Exception:
            logger.debug("Failed to enumerate children at depth %d", depth, exc_info=True)

    try:
        window = app.top_window()
        _walk(window, 0)
    except Exception:
        logger.debug("Failed to walk UIA tree for annotation", exc_info=True)

    return elements


def annotate_screenshot(
    png_bytes: bytes,
    elements: list[dict[str, Any]],
    window_rect: tuple[int, int, int, int] | None = None,
    max_width: int = 1920,
) -> bytes:
    """Draw numbered bounding boxes on a screenshot.

    If the image width exceeds max_width, downsamples the image and scales
    all bounding box coordinates proportionally.

    Args:
        png_bytes: Original screenshot as PNG bytes
        elements: Element list from collect_visible_elements
        window_rect: Optional (left, top, right, bottom) of the window on screen
            to translate element bounds from screen to window coordinates
        max_width: Maximum image width; downsamples if exceeded (default 1920)

    Returns:
        Annotated PNG bytes
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(io.BytesIO(png_bytes))

    # Calculate scale factor for downsampling
    scale = 1.0
    if image.width > max_width:
        scale = max_width / image.width
        new_height = int(image.height * scale)
        image = image.resize((max_width, new_height), Image.LANCZOS)

    draw = ImageDraw.Draw(image)

    # Try to load a small font for labels
    try:
        font = ImageFont.truetype("arial.ttf", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()

    # Window offset for screen->client coordinate conversion
    offset_x = window_rect[0] if window_rect else 0
    offset_y = window_rect[1] if window_rect else 0

    for elem in elements:
        bounds = elem["bounds"]
        x = (bounds["x"] - offset_x) * scale
        y = (bounds["y"] - offset_y) * scale
        w = bounds["width"] * scale
        h = bounds["height"] * scale

        # Skip elements outside the image
        if x + w < 0 or y + h < 0 or x > image.width or y > image.height:
            continue

        # Draw box
        draw.rectangle([x, y, x + w, y + h], outline="red", width=2)

        # Draw label background
        label = str(elem["id"])
        text_bbox = draw.textbbox((0, 0), label, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        draw.rectangle([x, y - text_h - 4, x + text_w + 4, y], fill="red")
        draw.text((x + 2, y - text_h - 2), label, fill="white", font=font)

    # Encode result
    result_buffer = io.BytesIO()
    image.save(result_buffer, format="PNG")
    return result_buffer.getvalue()

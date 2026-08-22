using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Capturing;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ScreenshotCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetDpiForWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetWindowDC(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);

    [DllImport("gdi32.dll", SetLastError = true)]
    private static extern bool BitBlt(
        IntPtr hdcDest,
        int nXDest,
        int nYDest,
        int nWidth,
        int nHeight,
        IntPtr hdcSrc,
        int nXSrc,
        int nYSrc,
        int dwRop);

    private const uint PW_RENDERFULLCONTENT = 0x00000002;
    private const int SW_RESTORE = 9;
    private const int SRCCOPY = 0x00CC0020;
    private const double BlankFrameVarianceThreshold = 0.01;
    private const int MaximumRasterBytes = 64 * 1024 * 1024;
    private const int MaximumRasterDimension = 8_192;

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    private readonly record struct CaptureSnapshot(
        int WindowLeft,
        int WindowTop,
        int WindowRight,
        int WindowBottom,
        int ClientLeft,
        int ClientTop,
        int ClientRight,
        int ClientBottom,
        uint Dpi)
    {
        public int RasterWidth => WindowRight - WindowLeft;
        public int RasterHeight => WindowBottom - WindowTop;
    }

    internal readonly record struct WindowGeometry(
        long Hwnd,
        int WindowLeft,
        int WindowTop,
        int WindowRight,
        int WindowBottom,
        int ClientLeft,
        int ClientTop,
        int ClientRight,
        int ClientBottom,
        uint Dpi);

    internal sealed record LosslessPngCapture(
        byte[] Bytes,
        int Width,
        int Height,
        long Hwnd,
        int WindowLeft,
        int WindowTop,
        int WindowRight,
        int WindowBottom,
        int ClientLeft,
        int ClientTop,
        int ClientRight,
        int ClientBottom,
        int Dpi,
        double Variance);

    public static JsonNode Screenshot(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var hwndValue = @params?["hwnd"]?.GetValue<long>();
        var evidence = @params?["evidence"]?.GetValue<bool>() ?? false;

        if (JsonRpcHandler.Stealth)
        {
            var hwnd = ResolveTargetHwnd(hwndValue, mainWindow);
            return evidence ? CaptureEvidenceWithPrintWindow(hwnd) : CaptureWithPrintWindow(hwnd);
        }

        if (evidence)
        {
            var hwnd = ResolveTargetHwnd(hwndValue, mainWindow);
            return CaptureEvidenceWithPrintWindow(hwnd);
        }

        CaptureImage capture;

        if (hwndValue is not null)
        {
            // Find window element by HWND and capture its bounding rectangle
            var hwnd = new IntPtr(hwndValue.Value);
            var windowElement = automation.FromHandle(hwnd);
            var rect = windowElement.BoundingRectangle;
            capture = Capture.Rectangle(rect);
        }
        else
        {
            var rect = mainWindow.BoundingRectangle;
            capture = Capture.Rectangle(rect);
        }

        using (capture)
        {
            return EncodeBitmap(capture.Bitmap);
        }
    }

    internal static LosslessPngCapture CaptureLosslessPng(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero)
            throw new ArgumentException("Target HWND must be non-zero.", nameof(hwnd));

        var before = ReadCaptureSnapshot(hwnd);
        var bitmap = CaptureBitmapWithPrintWindow(hwnd, before.RasterWidth, before.RasterHeight)
            ?? throw new InvalidOperationException("Evidence capture requires a PrintWindow raster");
        using (bitmap)
        {
            var after = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(before, after);
            return new LosslessPngCapture(
                EncodePng(bitmap),
                bitmap.Width,
                bitmap.Height,
                hwnd.ToInt64(),
                after.WindowLeft,
                after.WindowTop,
                after.WindowRight,
                after.WindowBottom,
                after.ClientLeft,
                after.ClientTop,
                after.ClientRight,
                after.ClientBottom,
                checked((int)after.Dpi),
                NormalizedPixelVariance(bitmap));
        }
    }

    internal static void ValidateCaptureDimensions(int width, int height)
    {
        if (width <= 0 || width > MaximumRasterDimension)
        {
            throw new ArgumentOutOfRangeException(nameof(width));
        }

        if (height <= 0 || height > MaximumRasterDimension || (long)width * height * sizeof(int) > MaximumRasterBytes)
        {
            throw new ArgumentOutOfRangeException(nameof(height));
        }
    }

    private static IntPtr ResolveTargetHwnd(long? hwndValue, AutomationElement mainWindow)
    {
        if (hwndValue is not null)
            return new IntPtr(hwndValue.Value);

        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
            throw new InvalidOperationException("Connected window has no native HWND");

        return hwnd;
    }

    private static JsonObject CaptureWithPrintWindow(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero)
            throw new ArgumentException("Target HWND must be non-zero.", nameof(hwnd));

        var (width, height) = GetWindowSize(hwnd);
        double? printWindowVariance = null;
        var printWindowBitmap = CaptureBitmapWithPrintWindow(hwnd, width, height);
        if (printWindowBitmap is not null)
        {
            using (printWindowBitmap)
            {
                printWindowVariance = NormalizedPixelVariance(printWindowBitmap);
                if (!IsBlankFrame(printWindowBitmap))
                {
                    var printWindowResult = EncodeBitmap(printWindowBitmap);
                    printWindowResult["method"] = "PrintWindow";
                    printWindowResult["flags"] = (int)PW_RENDERFULLCONTENT;
                    printWindowResult["variance"] = printWindowVariance.Value;
                    return printWindowResult;
                }
            }
        }

        using var fallbackBitmap = CaptureWithFlashFocusBitBlt(hwnd, width, height);
        var result = EncodeBitmap(fallbackBitmap);
        result["method"] = "BitBlt";
        result["fallback"] = "flash-focus";
        if (printWindowVariance is not null)
        {
            result["printwindow_variance"] = printWindowVariance.Value;
        }
        return result;
    }

    private static JsonObject CaptureEvidenceWithPrintWindow(IntPtr hwnd)
    {
        var printWindowBefore = ReadCaptureSnapshot(hwnd);
        var printWindowBitmap = CaptureBitmapWithPrintWindow(
            hwnd, printWindowBefore.RasterWidth, printWindowBefore.RasterHeight)
            ?? throw new InvalidOperationException("Evidence capture requires a PrintWindow raster");
        using (printWindowBitmap)
        {
            var printWindowAfter = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(printWindowBefore, printWindowAfter);
            var printWindowResult = EncodeBitmap(printWindowBitmap);
            printWindowResult["method"] = "PrintWindow";
            printWindowResult["flags"] = (int)PW_RENDERFULLCONTENT;
            printWindowResult["variance"] = NormalizedPixelVariance(printWindowBitmap);
            return AddCaptureProvenance(printWindowResult, hwnd, printWindowAfter);
        }
    }

    private static (int width, int height) GetWindowSize(IntPtr hwnd)
    {
        if (!GetWindowRect(hwnd, out var rect))
            throw new InvalidOperationException(
                $"GetWindowRect failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");

        var width = rect.Right - rect.Left;
        var height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0)
            throw new InvalidOperationException($"Window has invalid dimensions: {width}x{height}");

        return (width, height);
    }

    internal static WindowGeometry ReadWindowGeometry(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero)
            throw new ArgumentException("Target HWND must be non-zero.", nameof(hwnd));

        if (!GetWindowRect(hwnd, out var windowRect))
            throw new InvalidOperationException(
                $"GetWindowRect failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
        if (windowRect.Right <= windowRect.Left || windowRect.Bottom <= windowRect.Top)
            throw new InvalidOperationException(
                $"Window has invalid dimensions: {windowRect.Right - windowRect.Left}x{windowRect.Bottom - windowRect.Top}");

        if (!GetClientRect(hwnd, out var clientRect))
            throw new InvalidOperationException(
                $"GetClientRect failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
        if (clientRect.Right <= clientRect.Left || clientRect.Bottom <= clientRect.Top)
            throw new InvalidOperationException(
                $"Client rectangle has invalid dimensions for HWND {hwnd.ToInt64()}");

        var dpi = GetDpiForWindow(hwnd);
        if (dpi == 0)
            throw new InvalidOperationException(
                $"GetDpiForWindow failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");

        return new WindowGeometry(
            hwnd.ToInt64(),
            windowRect.Left,
            windowRect.Top,
            windowRect.Right,
            windowRect.Bottom,
            clientRect.Left,
            clientRect.Top,
            clientRect.Right,
            clientRect.Bottom,
            dpi);
    }

    private static CaptureSnapshot ReadCaptureSnapshot(IntPtr hwnd)
    {
        var geometry = ReadWindowGeometry(hwnd);
        return new CaptureSnapshot(
            geometry.WindowLeft,
            geometry.WindowTop,
            geometry.WindowRight,
            geometry.WindowBottom,
            geometry.ClientLeft,
            geometry.ClientTop,
            geometry.ClientRight,
            geometry.ClientBottom,
            geometry.Dpi);
    }

    private static void EnsureStableCaptureSnapshot(CaptureSnapshot before, CaptureSnapshot after)
    {
        if (before != after)
            throw new InvalidOperationException("Capture target changed during raster capture.");
    }

    private static JsonObject AddCaptureProvenance(JsonObject result, IntPtr hwnd, CaptureSnapshot snapshot)
    {
        result["hwnd"] = hwnd.ToInt64();
        result["client_rect"] = new JsonObject
        {
            ["left"] = snapshot.ClientLeft,
            ["top"] = snapshot.ClientTop,
            ["right"] = snapshot.ClientRight,
            ["bottom"] = snapshot.ClientBottom,
            ["unit"] = "physical_px",
            ["coordinate_space"] = "client",
            ["source_api"] = "GetClientRect",
        };
        result["window_bounds"] = new JsonObject
        {
            ["left"] = snapshot.WindowLeft,
            ["top"] = snapshot.WindowTop,
            ["right"] = snapshot.WindowRight,
            ["bottom"] = snapshot.WindowBottom,
            ["unit"] = "physical_px",
            ["coordinate_space"] = "screen",
            ["source_api"] = "GetWindowRect",
        };
        result["dpi"] = (int)snapshot.Dpi;
        return result;
    }

    private static Bitmap? CaptureBitmapWithPrintWindow(IntPtr hwnd, int width, int height)
    {
        ValidateCaptureDimensions(width, height);
        var bitmap = new Bitmap(width, height);
        try
        {
            var printed = false;
            using (var graphics = Graphics.FromImage(bitmap))
            {
                var hdc = graphics.GetHdc();
                try
                {
                    printed = PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT);
                }
                finally
                {
                    graphics.ReleaseHdc(hdc);
                }
            }
            if (!printed)
            {
                bitmap.Dispose();
                return null;
            }
            return bitmap;
        }
        catch
        {
            bitmap.Dispose();
            throw;
        }
    }

    private static Bitmap CaptureWithFlashFocusBitBlt(IntPtr hwnd, int width, int height)
    {
        var savedForeground = GetForegroundWindow();
        try
        {
            ShowWindow(hwnd, SW_RESTORE);
            SetForegroundWindow(hwnd);
            return CaptureBitmapWithBitBlt(hwnd, width, height);
        }
        finally
        {
            if (savedForeground != IntPtr.Zero)
            {
                SetForegroundWindow(savedForeground);
            }
        }
    }


    private static Bitmap CaptureBitmapWithBitBlt(IntPtr hwnd, int width, int height)
    {
        ValidateCaptureDimensions(width, height);
        var sourceDc = GetWindowDC(hwnd);
        if (sourceDc == IntPtr.Zero)
            throw new InvalidOperationException(
                $"GetWindowDC failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");

        using var bitmap = new Bitmap(width, height);
        try
        {
            using (var graphics = Graphics.FromImage(bitmap))
            {
                var hdc = graphics.GetHdc();
                try
                {
                    if (!BitBlt(hdc, 0, 0, width, height, sourceDc, 0, 0, SRCCOPY))
                    {
                        throw new InvalidOperationException(
                            $"BitBlt failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
                    }
                }
                finally
                {
                    graphics.ReleaseHdc(hdc);
                }
            }
            return (Bitmap)bitmap.Clone();
        }
        finally
        {
            ReleaseDC(hwnd, sourceDc);
        }
    }

    private static bool IsBlankFrame(Bitmap bitmap)
    {
        return NormalizedPixelVariance(bitmap) < BlankFrameVarianceThreshold;
    }

    private static double NormalizedPixelVariance(Bitmap bitmap)
    {
        var xStep = Math.Max(1, bitmap.Width / 100);
        var yStep = Math.Max(1, bitmap.Height / 100);
        double count = 0;
        double sum = 0;
        double sumSquares = 0;

        for (var y = 0; y < bitmap.Height; y += yStep)
        {
            for (var x = 0; x < bitmap.Width; x += xStep)
            {
                var color = bitmap.GetPixel(x, y);
                var luminance = ((0.2126 * color.R) + (0.7152 * color.G) + (0.0722 * color.B)) / 255.0;
                count++;
                sum += luminance;
                sumSquares += luminance * luminance;
            }
        }

        if (count == 0)
            return 0;

        var mean = sum / count;
        return Math.Max(0, (sumSquares / count) - (mean * mean));
    }

    private static JsonObject EncodeBitmap(Bitmap bitmap)
    {
        var bytes = EncodePng(bitmap);
        var base64 = Convert.ToBase64String(bytes);
        return new JsonObject
        {
            ["base64"] = base64,
            ["width"] = bitmap.Width,
            ["height"] = bitmap.Height
        };
    }

    private static byte[] EncodePng(Bitmap bitmap)
    {
        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        return ms.ToArray();
    }
}

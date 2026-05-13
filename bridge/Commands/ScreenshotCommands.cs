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

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    public static JsonNode Screenshot(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var hwndValue = @params?["hwnd"]?.GetValue<long>();

        if (JsonRpcHandler.Stealth)
        {
            var hwnd = ResolveTargetHwnd(hwndValue, mainWindow);
            return CaptureWithPrintWindow(hwnd);
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
        string? printWindowError = null;
        try
        {
            using var bitmap = CaptureBitmapWithPrintWindow(hwnd, width, height);
            printWindowVariance = NormalizedPixelVariance(bitmap);
            if (!IsBlankFrame(bitmap))
            {
                var printWindowResult = EncodeBitmap(bitmap);
                printWindowResult["method"] = "PrintWindow";
                printWindowResult["flags"] = (int)PW_RENDERFULLCONTENT;
                printWindowResult["variance"] = printWindowVariance.Value;
                return printWindowResult;
            }
        }
        catch (InvalidOperationException ex)
        {
            printWindowError = ex.Message;
        }

        using var fallbackBitmap = CaptureWithFlashFocusBitBlt(hwnd, width, height);
        var result = EncodeBitmap(fallbackBitmap);
        result["method"] = "BitBlt";
        result["fallback"] = "flash-focus";
        if (printWindowVariance is not null)
        {
            result["printwindow_variance"] = printWindowVariance.Value;
        }
        if (printWindowError is not null)
        {
            result["printwindow_error"] = printWindowError;
        }
        return result;
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

    private static Bitmap CaptureBitmapWithPrintWindow(IntPtr hwnd, int width, int height)
    {
        var bitmap = new Bitmap(width, height);
        try
        {
            using (var graphics = Graphics.FromImage(bitmap))
            {
                var hdc = graphics.GetHdc();
                try
                {
                    if (!PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT))
                    {
                        throw new InvalidOperationException(
                            $"PrintWindow failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
                    }
                }
                finally
                {
                    graphics.ReleaseHdc(hdc);
                }
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
        using var ms = new MemoryStream();
        bitmap.Save(ms, ImageFormat.Png);
        var bytes = ms.ToArray();
        var base64 = Convert.ToBase64String(bytes);

        return new JsonObject
        {
            ["base64"] = base64,
            ["width"] = bitmap.Width,
            ["height"] = bitmap.Height
        };
    }
}

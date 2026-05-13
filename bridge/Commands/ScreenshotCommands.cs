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

    private const uint PW_RENDERFULLCONTENT = 0x00000002;

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

        if (!GetWindowRect(hwnd, out var rect))
            throw new InvalidOperationException(
                $"GetWindowRect failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");

        var width = rect.Right - rect.Left;
        var height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0)
            throw new InvalidOperationException($"Window has invalid dimensions: {width}x{height}");

        using var bitmap = new Bitmap(width, height);
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

        var result = EncodeBitmap(bitmap);
        result["method"] = "PrintWindow";
        result["flags"] = (int)PW_RENDERFULLCONTENT;
        return result;
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

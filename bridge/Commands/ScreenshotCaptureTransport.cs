using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;

namespace FlaUIBridge.Commands;

internal readonly record struct CaptureSnapshot(
    int WindowLeft,
    int WindowTop,
    int WindowRight,
    int WindowBottom,
    int ClientLeft,
    int ClientTop,
    int ClientRight,
    int ClientBottom,
    uint Dpi,
    uint ProcessId)
{
    internal int RasterWidth => WindowRight - WindowLeft;
    internal int RasterHeight => WindowBottom - WindowTop;
}

internal readonly record struct ForegroundTransition(bool SetForegroundReturned, bool Verified);

internal interface IScreenshotCaptureTransport
{
    CaptureSnapshot ReadSnapshot(IntPtr hwnd);
    Bitmap? CapturePrintWindow(IntPtr hwnd, int width, int height);
    Bitmap CaptureBitBlt(IntPtr hwnd, int width, int height);
    IntPtr GetForegroundWindow();
    bool RestoreWindow(IntPtr hwnd);
    ForegroundTransition ActivateForegroundVerified(
        IntPtr hwnd,
        uint expectedProcessId,
        IntPtr requiredForeground,
        uint requiredForegroundProcessId);
    bool SetForegroundWindow(IntPtr hwnd);
}

internal sealed class NativeScreenshotCaptureTransport : IScreenshotCaptureTransport
{
    private const uint PwRenderFullContent = 0x00000002;
    private const int SwRestore = 9;
    private const int SrcCopy = 0x00CC0020;

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr hwnd, out Rect rect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    [DllImport("user32.dll", EntryPoint = "GetForegroundWindow")]
    private static extern IntPtr NativeGetForegroundWindow();

    [DllImport("user32.dll", EntryPoint = "SetForegroundWindow", SetLastError = true)]
    private static extern bool NativeSetForegroundWindow(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hwnd, int command);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool BringWindowToTop(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetWindowDC(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern int ReleaseDC(IntPtr hwnd, IntPtr hdc);

    [DllImport("gdi32.dll", SetLastError = true)]
    private static extern bool BitBlt(
        IntPtr destination,
        int xDestination,
        int yDestination,
        int width,
        int height,
        IntPtr source,
        int xSource,
        int ySource,
        int rasterOperation);

    public CaptureSnapshot ReadSnapshot(IntPtr hwnd)
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
            throw new InvalidOperationException($"Client rectangle has invalid dimensions for HWND {hwnd.ToInt64()}");
        var dpi = GetDpiForWindow(hwnd);
        if (dpi == 0)
            throw new InvalidOperationException(
                $"GetDpiForWindow failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
        if (GetWindowThreadProcessId(hwnd, out var processId) == 0 || processId == 0)
            throw new InvalidOperationException(
                $"GetWindowThreadProcessId failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");

        return new CaptureSnapshot(
            windowRect.Left, windowRect.Top, windowRect.Right, windowRect.Bottom,
            clientRect.Left, clientRect.Top, clientRect.Right, clientRect.Bottom, dpi, processId);
    }

    public Bitmap? CapturePrintWindow(IntPtr hwnd, int width, int height)
    {
        ScreenshotCommands.ValidateCaptureDimensions(width, height);
        var bitmap = new Bitmap(width, height);
        try
        {
            bool printed;
            using (var graphics = Graphics.FromImage(bitmap))
            {
                var hdc = graphics.GetHdc();
                try
                {
                    printed = PrintWindow(hwnd, hdc, PwRenderFullContent);
                }
                finally
                {
                    graphics.ReleaseHdc(hdc);
                }
            }

            if (printed)
                return bitmap;
            bitmap.Dispose();
            return null;
        }
        catch
        {
            bitmap.Dispose();
            throw;
        }
    }

    public Bitmap CaptureBitBlt(IntPtr hwnd, int width, int height)
    {
        ScreenshotCommands.ValidateCaptureDimensions(width, height);
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
                    if (!BitBlt(hdc, 0, 0, width, height, sourceDc, 0, 0, SrcCopy))
                        throw new InvalidOperationException(
                            $"BitBlt failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
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

    public IntPtr GetForegroundWindow() => NativeGetForegroundWindow();

    public bool RestoreWindow(IntPtr hwnd) => ShowWindow(hwnd, SwRestore);

    public ForegroundTransition ActivateForegroundVerified(
        IntPtr hwnd,
        uint expectedProcessId,
        IntPtr requiredForeground,
        uint requiredForegroundProcessId)
    {
        if (expectedProcessId == 0 ||
            !TryGetWindowIdentity(hwnd, out var targetThread, out var targetProcessId) ||
            targetProcessId != expectedProcessId)
        {
            return default;
        }

        var currentThread = GetCurrentThreadId();
        if (currentThread == 0 || !SharesDesktop(currentThread, targetThread))
            return default;

        var foreground = NativeGetForegroundWindow();
        if (requiredForeground != IntPtr.Zero &&
            (foreground != requiredForeground || requiredForegroundProcessId == 0))
        {
            return default;
        }

        uint foregroundThread = 0;
        if (foreground != IntPtr.Zero &&
            (!TryGetWindowIdentity(foreground, out foregroundThread, out var foregroundProcessId) ||
             (requiredForeground != IntPtr.Zero && foregroundProcessId != requiredForegroundProcessId) ||
             !SharesDesktop(currentThread, foregroundThread)))
        {
            return default;
        }

        var foregroundAttached = false;
        var targetAttached = false;
        try
        {
            if (foregroundThread != 0 && foregroundThread != currentThread)
            {
                if (!AttachThreadInput(currentThread, foregroundThread, true))
                    return default;
                foregroundAttached = true;
            }

            if (targetThread != currentThread && targetThread != foregroundThread)
            {
                if (!AttachThreadInput(currentThread, targetThread, true))
                    return default;
                targetAttached = true;
            }

            RestoreWindow(hwnd);
            BringWindowToTop(hwnd);
            var setForegroundReturned = NativeSetForegroundWindow(hwnd);
            if (!WaitForForeground(hwnd))
            {
                RestoreWindow(hwnd);
                BringWindowToTop(hwnd);
                NativeSetForegroundWindow(hwnd);
            }

            return new ForegroundTransition(setForegroundReturned, WaitForForeground(hwnd));
        }
        finally
        {
            if (targetAttached)
                AttachThreadInput(currentThread, targetThread, false);
            if (foregroundAttached)
                AttachThreadInput(currentThread, foregroundThread, false);
        }
    }

    public bool SetForegroundWindow(IntPtr hwnd) => NativeSetForegroundWindow(hwnd);

    private static bool TryGetWindowIdentity(IntPtr hwnd, out uint threadId, out uint processId)
    {
        processId = 0;
        threadId = hwnd == IntPtr.Zero ? 0 : GetWindowThreadProcessId(hwnd, out processId);
        return threadId != 0 && processId != 0;
    }

    private static bool SharesDesktop(uint firstThread, uint secondThread)
    {
        var firstDesktop = GetThreadDesktop(firstThread);
        return firstDesktop != IntPtr.Zero && firstDesktop == GetThreadDesktop(secondThread);
    }

    private static bool WaitForForeground(IntPtr hwnd)
    {
        const int foregroundActivationTimeoutMs = 750;
        const int foregroundActivationPollMs = 25;
        var stopwatch = Stopwatch.StartNew();
        while (stopwatch.ElapsedMilliseconds < foregroundActivationTimeoutMs)
        {
            if (NativeGetForegroundWindow() == hwnd)
                return true;

            Thread.Sleep(foregroundActivationPollMs);
        }

        return NativeGetForegroundWindow() == hwnd;
    }
}

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
    uint ProcessId,
    long ProcessCreationTicks,
    uint SessionId,
    long Hwnd)
{
    internal int RasterWidth => checked(WindowRight - WindowLeft);
    internal int RasterHeight => checked(WindowBottom - WindowTop);
}

internal sealed record CompositorCapture(
    Bitmap Bitmap,
    long FrameTimeTicks,
    int ItemWidth,
    int ItemHeight,
    int FrameWidth,
    int FrameHeight,
    bool WindowClosed) : IDisposable
{
    public void Dispose() => Bitmap.Dispose();
}

internal interface IScreenshotCaptureTransport
{
    CaptureSnapshot ReadSnapshot(IntPtr hwnd);
    uint ReadCaptureAffinity(IntPtr hwnd);
    Bitmap? CapturePrintWindow(IntPtr hwnd, int width, int height);
    CompositorCapture CaptureCompositorWindow(
        IntPtr hwnd, CaptureSnapshot expected, CancellationToken cancellationToken);
}

internal sealed class NativeScreenshotCaptureTransport : IScreenshotCaptureTransport
{
    private const uint PwRenderFullContent = 0x00000002;

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

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowDisplayAffinity(IntPtr hwnd, out uint affinity);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(
        IntPtr hwnd, uint attribute, out uint value, uint size);

    internal static uint ReadWindowProcessId(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero || GetWindowThreadProcessId(hwnd, out var processId) == 0 || processId == 0)
            throw new InvalidOperationException("Window process identity is unavailable.");
        return processId;
    }

    public CaptureSnapshot ReadSnapshot(IntPtr hwnd)
    {
        if (hwnd == IntPtr.Zero)
            throw new ArgumentException("Target HWND must be non-zero.", nameof(hwnd));
        if (!GetWindowRect(hwnd, out var windowRect))
            throw new InvalidOperationException(
                $"GetWindowRect failed for HWND {hwnd.ToInt64()}: {Marshal.GetLastWin32Error()}");
        if (windowRect.Right <= windowRect.Left || windowRect.Bottom <= windowRect.Top)
            throw new InvalidOperationException("Window has invalid physical dimensions.");
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
        using var process = Process.GetProcessById(checked((int)processId));
        _ = process.SafeHandle;
        var creationTicks = process.StartTime.ToUniversalTime().Ticks;
        var sessionId = checked((uint)process.SessionId);
        if (process.HasExited || GetWindowThreadProcessId(hwnd, out var afterProcessId) == 0 ||
            afterProcessId != processId)
            throw new InvalidOperationException("Capture target process changed while reading identity.");

        return new CaptureSnapshot(
            windowRect.Left, windowRect.Top, windowRect.Right, windowRect.Bottom,
            clientRect.Left, clientRect.Top, clientRect.Right, clientRect.Bottom, dpi, processId,
            creationTicks, sessionId, hwnd.ToInt64());
    }

    public uint ReadCaptureAffinity(IntPtr hwnd)
    {
        var targetThread = GetWindowThreadProcessId(hwnd, out var processId);
        var currentDesktop = GetThreadDesktop(GetCurrentThreadId());
        if (targetThread == 0 || processId == 0 || GetAncestor(hwnd, 2) != hwnd ||
            !IsWindowVisible(hwnd) || IsIconic(hwnd) || currentDesktop == IntPtr.Zero ||
            GetThreadDesktop(targetThread) != currentDesktop)
            throw new InvalidOperationException("Capture requires a visible, non-minimized top-level window on the current desktop.");
        using var target = Process.GetProcessById(checked((int)processId));
        using var current = Process.GetCurrentProcess();
        if (target.HasExited || target.SessionId != current.SessionId)
            throw new InvalidOperationException("Capture target is not in the bridge session.");
        Marshal.ThrowExceptionForHR(DwmGetWindowAttribute(hwnd, 14, out var cloaked, sizeof(uint)));
        if (cloaked != 0)
            throw new InvalidOperationException("Capture target is cloaked.");
        if (!GetWindowDisplayAffinity(hwnd, out var affinity))
            throw new InvalidOperationException(
                $"Capture policy is UNKNOWN: GetWindowDisplayAffinity failed ({Marshal.GetLastWin32Error()}).");
        if (affinity != 0)
            throw new InvalidOperationException($"Capture policy forbids this window (affinity {affinity}).");
        return affinity;
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

    public CompositorCapture CaptureCompositorWindow(
        IntPtr hwnd, CaptureSnapshot expected, CancellationToken cancellationToken) =>
        CompositorWindowCapture.Capture(hwnd, expected, this, cancellationToken);
}

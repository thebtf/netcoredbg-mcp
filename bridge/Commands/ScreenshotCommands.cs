using System.Diagnostics;
using System.Drawing;
using System.Buffers;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ScreenshotCommands
{
    private static readonly IScreenshotCaptureTransport ProductionCaptureTransport =
        new NativeScreenshotCaptureTransport();
    private static readonly AsyncLocal<IScreenshotCaptureTransport?> ScopedCaptureTransport = new();
    private const uint PW_RENDERFULLCONTENT = 0x00000002;
    private const int MaximumRasterBytes = 64 * 1024 * 1024;
    private const int MaximumRasterDimension = 8_192;

    private static IScreenshotCaptureTransport CaptureTransport =>
        ScopedCaptureTransport.Value ?? ProductionCaptureTransport;

    internal static IDisposable PushCaptureTransportForTesting(IScreenshotCaptureTransport transport)
    {
        ArgumentNullException.ThrowIfNull(transport);
        var previous = ScopedCaptureTransport.Value;
        ScopedCaptureTransport.Value = transport;
        return new CaptureTransportScope(previous);
    }

    private sealed class CaptureTransportScope(IScreenshotCaptureTransport? previous) : IDisposable
    {
        private bool _disposed;

        public void Dispose()
        {
            if (_disposed)
                return;

            ScopedCaptureTransport.Value = previous;
            _disposed = true;
        }
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

        var hwnd = ResolveTargetHwnd(@params?["hwnd"]?.GetValue<long>(), mainWindow);
        var compositorFallback = @params?["compositor_fallback"]?.GetValue<bool>() ?? true;
        var expectedProcessId = @params?["expected_process_id"]?.GetValue<int>();
        if (@params?["compositor_fallback"] is not null && (expectedProcessId is null or <= 0))
            throw new ArgumentException("Compositor capture requires a positive expected process ID.");
        if (expectedProcessId is not null && expectedProcessId != JsonRpcHandler.ProcessId)
            throw new InvalidOperationException("Capture target does not belong to the active debuggee process.");

        var expectedHwnd = @params?["expected_hwnd"]?.GetValue<long>();
        var expectedWidth = @params?["expected_physical_width"]?.GetValue<int>();
        var expectedHeight = @params?["expected_physical_height"]?.GetValue<int>();
        if (expectedHwnd is not null || expectedWidth is not null || expectedHeight is not null)
        {
            if (expectedHwnd is null or 0 || expectedWidth is null or <= 0 || expectedHeight is null or <= 0)
                throw new ArgumentException("Strict capture requires an expected HWND and positive physical dimensions.");
            if (hwnd.ToInt64() != expectedHwnd)
                throw new InvalidOperationException("Capture target HWND does not match the expected HWND.");
        }

        return CaptureWindowScreenshot(hwnd, compositorFallback);
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

    private static JsonObject CaptureWindowScreenshot(IntPtr hwnd, bool compositorFallback)
    {
        var connectedProcessId = RequireConnectedProcessId();
        using var process = Process.GetProcessById(checked((int)connectedProcessId));
        _ = process.SafeHandle;
        if (process.HasExited || process.StartTime.ToUniversalTime().Ticks != JsonRpcHandler.ProcessCreationTicks ||
            checked((uint)process.SessionId) != JsonRpcHandler.ProcessSessionId)
            throw new InvalidOperationException("Connected capture process identity is no longer valid.");

        var before = ReadCaptureSnapshot(hwnd);
        EnsureConnectedCaptureTarget(hwnd, before);
        var affinity = CaptureTransport.ReadCaptureAffinity(hwnd);
        if (affinity != 0)
            throw new InvalidOperationException("Capture policy forbids this window.");
        double? printWindowVariance = null;
        var printWindowBitmap = CaptureBitmapWithPrintWindow(hwnd, before.RasterWidth, before.RasterHeight);
        CaptureSnapshot after;
        using (printWindowBitmap)
        {
            after = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(before, after);
            EnsureConnectedCaptureTarget(hwnd, after);
            EnsureStableCapturePolicy(hwnd, affinity);
            if (process.HasExited)
                throw new InvalidOperationException("Capture process exited during PrintWindow.");
            if (printWindowBitmap is not null)
            {
                EnsureRasterDimensions(printWindowBitmap, before);
                printWindowVariance = NormalizedPixelVariance(printWindowBitmap);
                if (!IsProbablyBlackFrame(printWindowBitmap))
                {
                    var result = EncodeBitmap(printWindowBitmap);
                    result["method"] = "PrintWindow";
                    result["flags"] = (int)PW_RENDERFULLCONTENT;
                    result["variance"] = printWindowVariance.Value;
                    AddCaptureStability(result, hwnd, before, after);
                    return AddCaptureProvenance(result, hwnd, after);
                }
            }
        }

        if (!compositorFallback)
            throw new InvalidOperationException("PrintWindow did not produce a usable raster and compositor capture is disabled.");
        using var compositor = CaptureTransport.CaptureCompositorWindow(hwnd, after, CancellationToken.None);
        var compositorAfter = ReadCaptureSnapshot(hwnd);
        EnsureStableCaptureSnapshot(after, compositorAfter);
        EnsureConnectedCaptureTarget(hwnd, compositorAfter);
        EnsureStableCapturePolicy(hwnd, affinity);
        if (process.HasExited || compositor.WindowClosed)
            throw new InvalidOperationException("Capture target closed during compositor capture.");
        EnsureRasterDimensions(compositor.Bitmap, compositorAfter);
        if (compositor.FrameTimeTicks <= 0 || compositor.ItemWidth != compositorAfter.RasterWidth ||
            compositor.ItemHeight != compositorAfter.RasterHeight ||
            compositor.FrameWidth != compositor.ItemWidth || compositor.FrameHeight != compositor.ItemHeight)
            throw new InvalidOperationException("Compositor item/frame geometry or timestamp is invalid.");

        var fallbackResult = EncodeBitmap(compositor.Bitmap);
        fallbackResult["method"] = "WindowsGraphicsCapture";
        fallbackResult["evidence_grade"] = "compositor_window_raster";
        fallbackResult["source_api"] = "GraphicsCaptureItem.CreateForWindow";
        fallbackResult["capture_authority"] = "hwnd_compositor_frame";
        fallbackResult["alternate_attempts"] = 1;
        fallbackResult["fallback_reason"] = printWindowVariance is null ? "printwindow_failed" : "probable_black_printwindow";
        if (printWindowVariance is not null)
        {
            fallbackResult["printwindow_variance"] = printWindowVariance.Value;
            fallbackResult["printwindow_classification"] = "probable_black_discarded";
            fallbackResult["printwindow_analysis"] = new JsonObject
            {
                ["classification"] = "PROBABLE_BLACK_FRAME",
                ["variance"] = printWindowVariance.Value,
            };
        }
        fallbackResult["compositor"] = new JsonObject
        {
            ["frame_time_ticks"] = compositor.FrameTimeTicks,
            ["item_size"] = new JsonObject { ["width"] = compositor.ItemWidth, ["height"] = compositor.ItemHeight },
            ["frame_content_size"] = new JsonObject { ["width"] = compositor.FrameWidth, ["height"] = compositor.FrameHeight },
            ["policy"] = new JsonObject { ["status"] = "ALLOWED", ["affinity"] = affinity },
            ["process_identity"] = new JsonObject
            {
                ["connected_creation_ticks"] = JsonRpcHandler.ProcessCreationTicks,
                ["before_creation_ticks"] = after.ProcessCreationTicks,
                ["after_creation_ticks"] = compositorAfter.ProcessCreationTicks,
                ["process_id"] = checked((int)compositorAfter.ProcessId),
                ["session_id"] = compositorAfter.SessionId,
            },
            ["window_closed"] = false,
        };
        AddCaptureStability(fallbackResult, hwnd, after, compositorAfter);
        return AddCaptureProvenance(fallbackResult, hwnd, compositorAfter);
    }

    private static void AddCaptureStability(
        JsonObject result, IntPtr hwnd, CaptureSnapshot before, CaptureSnapshot after)
    {
        result["capture_stability"] = new JsonObject
        {
            ["before"] = CaptureSnapshotJson(hwnd, before),
            ["after"] = CaptureSnapshotJson(hwnd, after),
        };
        result["foreground"] = new JsonObject { ["mutation_attempted"] = false };
        result["capture_policy"] = new JsonObject { ["status"] = "ALLOWED", ["affinity"] = 0 };
    }

    private static void EnsureRasterDimensions(Bitmap bitmap, CaptureSnapshot snapshot)
    {
        if (bitmap.Width != snapshot.RasterWidth || bitmap.Height != snapshot.RasterHeight)
            throw new InvalidOperationException("Raster dimensions do not match the physical capture target.");
    }

    private static void EnsureStableCapturePolicy(IntPtr hwnd, uint expected)
    {
        if (CaptureTransport.ReadCaptureAffinity(hwnd) != expected)
            throw new InvalidOperationException("Capture policy changed during raster capture.");
    }

    private static JsonObject CaptureSnapshotJson(IntPtr hwnd, CaptureSnapshot snapshot)
    {
        return new JsonObject
        {
            ["hwnd"] = hwnd.ToInt64(),
            ["process_id"] = checked((int)snapshot.ProcessId),
            ["process_creation_ticks"] = snapshot.ProcessCreationTicks,
            ["session_id"] = snapshot.SessionId,
            ["client_rect"] = new JsonObject
            {
                ["left"] = snapshot.ClientLeft,
                ["top"] = snapshot.ClientTop,
                ["right"] = snapshot.ClientRight,
                ["bottom"] = snapshot.ClientBottom,
                ["unit"] = "physical_px",
                ["coordinate_space"] = "client",
                ["source_api"] = "GetClientRect",
            },
            ["window_bounds"] = new JsonObject
            {
                ["left"] = snapshot.WindowLeft,
                ["top"] = snapshot.WindowTop,
                ["right"] = snapshot.WindowRight,
                ["bottom"] = snapshot.WindowBottom,
                ["unit"] = "physical_px",
                ["coordinate_space"] = "screen",
                ["source_api"] = "GetWindowRect",
            },
            ["dpi"] = checked((int)snapshot.Dpi),
        };
    }


    private static uint RequireConnectedProcessId()
    {
        var processId = JsonRpcHandler.ProcessId;
        if (processId <= 0)
            throw new InvalidOperationException("Capture requires a positive connected process ID.");

        return checked((uint)processId);
    }

    private static void EnsureConnectedCaptureTarget(IntPtr hwnd, CaptureSnapshot snapshot)
    {
        if (snapshot.Hwnd != hwnd.ToInt64() || snapshot.ProcessId != RequireConnectedProcessId() ||
            snapshot.ProcessCreationTicks <= 0 || snapshot.ProcessCreationTicks != JsonRpcHandler.ProcessCreationTicks ||
            snapshot.SessionId != JsonRpcHandler.ProcessSessionId)
            throw new InvalidOperationException("Capture target does not match the connected HWND/process creation/session identity.");
    }


    internal static WindowGeometry ReadWindowGeometry(IntPtr hwnd)
    {
        var snapshot = ReadCaptureSnapshot(hwnd);
        return new WindowGeometry(
            hwnd.ToInt64(),
            snapshot.WindowLeft,
            snapshot.WindowTop,
            snapshot.WindowRight,
            snapshot.WindowBottom,
            snapshot.ClientLeft,
            snapshot.ClientTop,
            snapshot.ClientRight,
            snapshot.ClientBottom,
            snapshot.Dpi);
    }

    private static CaptureSnapshot ReadCaptureSnapshot(IntPtr hwnd) =>
        CaptureTransport.ReadSnapshot(hwnd);

    private static void EnsureStableCaptureSnapshot(CaptureSnapshot before, CaptureSnapshot after)
    {
        if (before != after)
            throw new InvalidOperationException("Capture target changed during raster capture.");
    }

    private static JsonObject AddCaptureProvenance(JsonObject result, IntPtr hwnd, CaptureSnapshot snapshot)
    {
        result["hwnd"] = hwnd.ToInt64();
        result["process_id"] = checked((int)snapshot.ProcessId);
        result["process_creation_ticks"] = snapshot.ProcessCreationTicks;
        result["connected_creation_ticks"] = JsonRpcHandler.ProcessCreationTicks;
        result["session_id"] = snapshot.SessionId;
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
        result["bridge_assembly"] = GetManagedAssemblyIdentity();
        return result;
    }

    private static JsonObject GetManagedAssemblyIdentity()
    {
        var path = typeof(ScreenshotCommands).Assembly.Location;
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            path = Environment.ProcessPath ?? string.Empty;
        if (!File.Exists(path))
            throw new InvalidOperationException("Evidence capture requires a readable managed bridge artifact path.");

        using var stream = File.OpenRead(path);
        return new JsonObject
        {
            ["path"] = path,
            ["sha256"] = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant(),
        };
    }

    private static Bitmap? CaptureBitmapWithPrintWindow(IntPtr hwnd, int width, int height) =>
        CaptureTransport.CapturePrintWindow(hwnd, width, height);


    private static bool IsProbablyBlackFrame(Bitmap bitmap)
    {
        const double maxMeanLuminance = 8.0;
        const int darkLuminanceThreshold = 16;
        const double minimumDarkPixelFraction = 0.995;

        if (SupportsDirectPixelScanning(bitmap.PixelFormat))
        {
            return ClassifyFullFrame(
                bitmap,
                maxMeanLuminance,
                darkLuminanceThreshold,
                minimumDarkPixelFraction);
        }

        using var classificationBitmap = ConvertToClassificationBitmap(bitmap);
        return ClassifyFullFrame(
            classificationBitmap,
            maxMeanLuminance,
            darkLuminanceThreshold,
            minimumDarkPixelFraction);
    }

    private static bool SupportsDirectPixelScanning(PixelFormat pixelFormat) =>
        pixelFormat is PixelFormat.Format24bppRgb
            or PixelFormat.Format32bppRgb
            or PixelFormat.Format32bppArgb
            or PixelFormat.Format32bppPArgb;

    private static Bitmap ConvertToClassificationBitmap(Bitmap bitmap)
    {
        var converted = new Bitmap(bitmap.Width, bitmap.Height, PixelFormat.Format32bppArgb);
        try
        {
            using var graphics = Graphics.FromImage(converted);
            graphics.DrawImageUnscaled(bitmap, 0, 0);
            return converted;
        }
        catch
        {
            converted.Dispose();
            throw;
        }
    }

    private static bool ClassifyFullFrame(
        Bitmap bitmap,
        double maxMeanLuminance,
        int darkLuminanceThreshold,
        double minimumDarkPixelFraction)
    {
        var pixelFormat = bitmap.PixelFormat;
        var bytesPerPixel = Image.GetPixelFormatSize(pixelFormat) / 8;
        var pixelCount = checked((long)bitmap.Width * bitmap.Height);
        var darkLuminanceThresholdMilli = darkLuminanceThreshold * 1_000;
        var bitmapData = bitmap.LockBits(
            new Rectangle(0, 0, bitmap.Width, bitmap.Height),
            ImageLockMode.ReadOnly,
            pixelFormat);
        try
        {
            var stride = Math.Abs(bitmapData.Stride);
            var row = ArrayPool<byte>.Shared.Rent(stride);
            try
            {
                long luminanceSumMilli = 0;
                long darkPixelCount = 0;
                for (var y = 0; y < bitmap.Height; y++)
                {
                    var rowStart = IntPtr.Add(bitmapData.Scan0, checked(y * bitmapData.Stride));
                    Marshal.Copy(rowStart, row, 0, stride);
                    for (var x = 0; x < bitmap.Width; x++)
                    {
                        var pixelOffset = x * bytesPerPixel;
                        var luminanceMilli = (299 * row[pixelOffset + 2])
                            + (587 * row[pixelOffset + 1])
                            + (114 * row[pixelOffset]);
                        luminanceSumMilli += luminanceMilli;
                        if (luminanceMilli <= darkLuminanceThresholdMilli)
                            darkPixelCount++;
                    }
                }

                return (double)luminanceSumMilli / (pixelCount * 1_000) <= maxMeanLuminance
                    && (double)darkPixelCount / pixelCount >= minimumDarkPixelFraction;
            }
            finally
            {
                ArrayPool<byte>.Shared.Return(row);
            }
        }
        finally
        {
            bitmap.UnlockBits(bitmapData);
        }
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

using System.Drawing;
using System.Buffers;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Capturing;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ScreenshotCommands
{
    private static readonly IScreenshotCaptureTransport ProductionCaptureTransport =
        new NativeScreenshotCaptureTransport();
    private static readonly AsyncLocal<IScreenshotCaptureTransport?> ScopedCaptureTransport = new();
    private const uint PW_RENDERFULLCONTENT = 0x00000002;
    private const double BlankFrameVarianceThreshold = 0.01;
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
    private readonly record struct StrictCaptureTarget(long ExpectedHwnd, uint ExpectedProcessId);

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
        var typedBitBltFallback = @params?["typed_bitblt_fallback"]?.GetValue<bool>() ?? false;
        StrictCaptureTarget? strictCaptureTarget = null;
        if (typedBitBltFallback)
        {
            var expectedHwnd = @params?["expected_hwnd"]?.GetValue<long>();
            var expectedWidth = @params?["expected_physical_width"]?.GetValue<int>();
            var expectedHeight = @params?["expected_physical_height"]?.GetValue<int>();
            var expectedProcessId = @params?["expected_process_id"]?.GetValue<int>();
            if (expectedHwnd is null || expectedHwnd == 0 || expectedWidth is null or <= 0 ||
                expectedHeight is null or <= 0 || expectedProcessId is null or <= 0)
            {
                throw new ArgumentException(
                    "Typed BitBlt fallback requires an expected HWND, active process ID, and positive physical dimensions.");
            }
            strictCaptureTarget = new StrictCaptureTarget(
                expectedHwnd.Value, checked((uint)expectedProcessId.Value));
        }

        if (JsonRpcHandler.Stealth)
        {
            var hwnd = ResolveTargetHwnd(hwndValue, mainWindow);
            return evidence
                ? CaptureEvidenceWithPrintWindow(hwnd, typedBitBltFallback, strictCaptureTarget)
                : CaptureWithPrintWindow(hwnd);
        }

        if (evidence)
        {
            var hwnd = ResolveTargetHwnd(hwndValue, mainWindow);
            return CaptureEvidenceWithPrintWindow(hwnd, typedBitBltFallback, strictCaptureTarget);
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

    private static JsonObject CaptureEvidenceWithPrintWindow(
        IntPtr hwnd,
        bool typedBitBltFallback,
        StrictCaptureTarget? strictCaptureTarget)
    {
        var connectedProcessId = RequireConnectedProcessId();
        if (typedBitBltFallback && strictCaptureTarget is StrictCaptureTarget expectedTarget)
            EnsureStrictExpectedHandle(expectedTarget);

        var printWindowBefore = ReadCaptureSnapshot(hwnd);
        EnsureStrictCaptureProcess(printWindowBefore, connectedProcessId);
        if (typedBitBltFallback && strictCaptureTarget is StrictCaptureTarget strictBefore)
            EnsureStrictCaptureProcess(printWindowBefore, strictBefore.ExpectedProcessId);
        var printWindowBitmap = CaptureBitmapWithPrintWindow(
            hwnd, printWindowBefore.RasterWidth, printWindowBefore.RasterHeight)
            ?? throw new InvalidOperationException("Evidence capture requires a PrintWindow raster");
        if (printWindowBitmap.Width != printWindowBefore.RasterWidth ||
            printWindowBitmap.Height != printWindowBefore.RasterHeight)
        {
            printWindowBitmap.Dispose();
            throw new InvalidOperationException("PrintWindow raster dimensions do not match the capture target.");
        }

        CaptureSnapshot printWindowAfter;
        double printWindowVariance;
        using (printWindowBitmap)
        {
            printWindowAfter = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(printWindowBefore, printWindowAfter);
            EnsureStrictCaptureProcess(printWindowAfter, connectedProcessId);
            if (typedBitBltFallback && strictCaptureTarget is StrictCaptureTarget strictAfter)
                EnsureStrictCaptureProcess(printWindowAfter, strictAfter.ExpectedProcessId);
            printWindowVariance = NormalizedPixelVariance(printWindowBitmap);
            if (!IsProbablyBlackFrame(printWindowBitmap))
            {
                var printWindowResult = EncodeBitmap(printWindowBitmap);
                printWindowResult["method"] = "PrintWindow";
                printWindowResult["flags"] = (int)PW_RENDERFULLCONTENT;
                printWindowResult["variance"] = printWindowVariance;
                return AddCaptureProvenance(printWindowResult, hwnd, printWindowAfter);
            }
        }

        var fallbackTarget = strictCaptureTarget ?? new StrictCaptureTarget(
            hwnd.ToInt64(), connectedProcessId);
        return CaptureEvidenceWithVerifiedBitBltFallback(
            hwnd, fallbackTarget, printWindowAfter, printWindowVariance);
    }

    private static JsonObject CaptureEvidenceWithVerifiedBitBltFallback(
        IntPtr hwnd,
        StrictCaptureTarget strictCaptureTarget,
        CaptureSnapshot printWindowAfter,
        double printWindowVariance)
    {
        var savedForeground = CaptureTransport.GetForegroundWindow();
        CaptureSnapshot? savedForegroundSnapshot = savedForeground == IntPtr.Zero
            ? null
            : ReadCaptureSnapshot(savedForeground);
        var activation = new JsonObject
        {
            ["attempted"] = true,
            ["set_foreground_returned"] = false,
            ["foreground_hwnd"] = hwnd.ToInt64(),
            ["verified"] = false,
        };
        var restoration = new JsonObject
        {
            ["required"] = savedForeground != IntPtr.Zero,
            ["attempted"] = false,
            ["set_foreground_returned"] = false,
            ["foreground_hwnd"] = savedForeground.ToInt64(),
            ["verified"] = false,
        };
        if (savedForegroundSnapshot is CaptureSnapshot savedForegroundIdentity)
            restoration["process_id"] = checked((int)savedForegroundIdentity.ProcessId);

        try
        {
            var activationTransition = CaptureTransport.ActivateForegroundVerified(
                hwnd,
                strictCaptureTarget.ExpectedProcessId,
                IntPtr.Zero,
                0);
            activation["set_foreground_returned"] = activationTransition.SetForegroundReturned;
            if (!activationTransition.Verified)
                throw new InvalidOperationException("Typed BitBlt fallback could not activate the capture target safely.");
            activation["verified"] = true;

            var fallbackBefore = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(printWindowAfter, fallbackBefore);
            EnsureStrictCaptureProcess(fallbackBefore, strictCaptureTarget.ExpectedProcessId);
            if (CaptureTransport.GetForegroundWindow() != hwnd)
                throw new InvalidOperationException("Typed BitBlt fallback lost foreground before raster capture.");
            using var fallbackBitmap = CaptureBitmapWithBitBlt(
                hwnd, fallbackBefore.RasterWidth, fallbackBefore.RasterHeight);
            if (CaptureTransport.GetForegroundWindow() != hwnd)
                throw new InvalidOperationException("Typed BitBlt fallback lost foreground during raster capture.");
            var fallbackAfter = ReadCaptureSnapshot(hwnd);
            EnsureStableCaptureSnapshot(fallbackBefore, fallbackAfter);
            EnsureStrictCaptureProcess(fallbackAfter, strictCaptureTarget.ExpectedProcessId);

            var result = EncodeBitmap(fallbackBitmap);
            result["method"] = "BitBlt";
            result["fallback"] = "flash-focus";
            result["fallback_reason"] = "probable_black_printwindow";
            result["authority"] = "foreground_window_gdi_raster";
            result["capture_authority"] = "foreground_window_gdi_raster";
            result["source_api"] = "GetWindowDC";
            result["rop"] = "SRCCOPY";
            result["evidence_grade"] = "typed_bitblt_fallback";
            result["alternate_attempts"] = 1;
            result["printwindow_classification"] = "probable_black_discarded";
            result["printwindow_variance"] = printWindowVariance;
            result["printwindow_analysis"] = new JsonObject
            {
                ["classification"] = "PROBABLE_BLACK_FRAME",
                ["variance"] = printWindowVariance,
            };
            result["process_id"] = checked((int)fallbackAfter.ProcessId);
            result["capture_stability"] = new JsonObject
            {
                ["before"] = CaptureSnapshotJson(hwnd, fallbackBefore),
                ["after"] = CaptureSnapshotJson(hwnd, fallbackAfter),
            };
            result["foreground"] = new JsonObject
            {
                ["activation"] = activation,
                ["restoration"] = restoration,
            };
            return AddCaptureProvenance(result, hwnd, fallbackAfter);
        }
        finally
        {
            if (savedForeground == IntPtr.Zero)
            {
                restoration["verified"] = true;
            }
            else
            {
                if (savedForegroundSnapshot is not CaptureSnapshot restoredForegroundIdentity ||
                    CaptureTransport.GetForegroundWindow() != hwnd ||
                    ReadCaptureSnapshot(savedForeground).ProcessId != restoredForegroundIdentity.ProcessId)
                {
                    throw new InvalidOperationException("Typed BitBlt fallback could not restore foreground safely.");
                }

                restoration["attempted"] = true;
                var restorationTransition = CaptureTransport.ActivateForegroundVerified(
                    savedForeground,
                    restoredForegroundIdentity.ProcessId,
                    hwnd,
                    printWindowAfter.ProcessId);
                restoration["set_foreground_returned"] = restorationTransition.SetForegroundReturned;
                restoration["verified"] = restorationTransition.Verified;
                if (!restorationTransition.Verified)
                    throw new InvalidOperationException("Typed BitBlt fallback could not restore foreground safely.");
            }
        }
    }

    private static JsonObject CaptureSnapshotJson(IntPtr hwnd, CaptureSnapshot snapshot)
    {
        return new JsonObject
        {
            ["hwnd"] = hwnd.ToInt64(),
            ["process_id"] = checked((int)snapshot.ProcessId),
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

    private static void EnsureStrictExpectedHandle(StrictCaptureTarget strictCaptureTarget)
    {
        var expectedSnapshot = ReadCaptureSnapshot(new IntPtr(strictCaptureTarget.ExpectedHwnd));
        EnsureStrictCaptureProcess(expectedSnapshot, strictCaptureTarget.ExpectedProcessId);
    }

    private static uint RequireConnectedProcessId()
    {
        var processId = JsonRpcHandler.ProcessId;
        if (processId <= 0)
            throw new InvalidOperationException("Evidence capture requires a positive connected process ID.");

        return checked((uint)processId);
    }

    private static void EnsureStrictCaptureProcess(CaptureSnapshot snapshot, uint expectedProcessId)
    {
        if (snapshot.ProcessId != expectedProcessId)
            throw new InvalidOperationException("Capture target does not belong to the active debuggee process.");
    }

    private static (int width, int height) GetWindowSize(IntPtr hwnd)
    {
        var snapshot = ReadCaptureSnapshot(hwnd);
        return (snapshot.RasterWidth, snapshot.RasterHeight);
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

    private static Bitmap CaptureWithFlashFocusBitBlt(IntPtr hwnd, int width, int height)
    {
        var savedForeground = CaptureTransport.GetForegroundWindow();
        try
        {
            CaptureTransport.RestoreWindow(hwnd);
            CaptureTransport.SetForegroundWindow(hwnd);
            return CaptureBitmapWithBitBlt(hwnd, width, height);
        }
        finally
        {
            if (savedForeground != IntPtr.Zero)
                CaptureTransport.SetForegroundWindow(savedForeground);
        }
    }

    private static Bitmap CaptureBitmapWithBitBlt(IntPtr hwnd, int width, int height) =>
        CaptureTransport.CaptureBitBlt(hwnd, width, height);

    private static bool IsBlankFrame(Bitmap bitmap)
    {
        return NormalizedPixelVariance(bitmap) < BlankFrameVarianceThreshold;
    }

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

using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;

namespace FlaUIBridge.Commands;

internal static class NativeSceneEvidenceCommands
{
    private const string CaptureVisualEvidenceOperation = "capture_visual_evidence";
    private const int MaximumPngBytes = 64 * 1024 * 1024;

    internal static JsonObject Handle(JsonObject request, int boundProcessId)
    {
        if (request.Count != 3 ||
            !string.Equals(request["operation"]?.GetValue<string>(), CaptureVisualEvidenceOperation, StringComparison.Ordinal) ||
            request["processId"] is not JsonValue processIdValue ||
            !processIdValue.TryGetValue<int>(out var processId) ||
            request["processIdentity"] is not JsonValue processIdentityValue ||
            !processIdentityValue.TryGetValue<string>(out var processIdentity) ||
            string.IsNullOrWhiteSpace(processIdentity) ||
            processId != boundProcessId)
        {
            throw new InvalidDataException("Native scene evidence request is not authorized for the bound process.");
        }
        using var process = Process.GetProcessById(processId);
        process.Refresh();
        if (process.HasExited ||
            !string.Equals(processIdentity, CreateProcessIdentity(process), StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Bound process identity is unavailable.");
        }

        var hwnd = process.MainWindowHandle;
        if (hwnd == IntPtr.Zero || GetWindowThreadProcessId(hwnd, out var hwndProcessId) == 0 || hwndProcessId != (uint)processId)
        {
            throw new InvalidOperationException("Bound process does not expose an owned top-level window.");
        }

        var capture = ScreenshotCommands.CaptureLosslessPng(hwnd);
        process.Refresh();
        if (process.HasExited ||
            !string.Equals(processIdentity, CreateProcessIdentity(process), StringComparison.Ordinal) ||
            GetWindowThreadProcessId(hwnd, out hwndProcessId) == 0 ||
            hwndProcessId != (uint)processId)
        {
            throw new InvalidOperationException("Bound process or window changed during evidence capture.");
        }
        if (capture.Bytes.Length > MaximumPngBytes)
        {
            throw new InvalidDataException("Lossless PNG exceeds the bounded bridge response capacity.");
        }

        return new JsonObject
        {
            ["pngBase64"] = Convert.ToBase64String(capture.Bytes),
            ["byteLength"] = capture.Bytes.Length,
            ["sha256"] = Convert.ToHexString(SHA256.HashData(capture.Bytes)).ToLowerInvariant(),
            ["provenance"] = new JsonObject
            {
                ["processId"] = processId,
                ["processIdentity"] = processIdentity,
                ["hwnd"] = capture.Hwnd,
                ["width"] = capture.Width,
                ["height"] = capture.Height,
                ["clientRect"] = new JsonObject
                {
                    ["left"] = capture.ClientLeft,
                    ["top"] = capture.ClientTop,
                    ["right"] = capture.ClientRight,
                    ["bottom"] = capture.ClientBottom,
                },
                ["dpi"] = capture.Dpi,
                ["captureMethod"] = "PrintWindow",
                ["printWindowFlags"] = 2,
            },
        };
    }

    private static string CreateProcessIdentity(Process process) => string.Concat(
        "process_",
        process.Id.ToString(CultureInfo.InvariantCulture),
        "_start_",
        process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture));

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
}

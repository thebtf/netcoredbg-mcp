using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using FlaUIBridge;
using FlaUIBridge.Commands;

namespace TypedBitBltFallbackBridgeHost;

internal static class Program
{
    private const string TraceFileEnvironmentVariable = "NETCOREDBG_TYPED_BITBLT_TRACE_FILE";

    private static void Main()
    {
        var traceFile = Environment.GetEnvironmentVariable(TraceFileEnvironmentVariable);
        if (string.IsNullOrWhiteSpace(traceFile))
            throw new InvalidOperationException($"Missing {TraceFileEnvironmentVariable}.");
        WriteIdentity(traceFile);

        using var scope = ScreenshotCommands.PushCaptureTransportForTesting(
            new BlackPrimaryCaptureTransport(traceFile));
        try
        {
            FlaUIBridge.Program.RunStdin();
        }
        finally
        {
            JsonRpcHandler.Dispose();
        }
    }


    private static void WriteIdentity(string traceFile)
    {
        var sourceIdentity = Environment.GetEnvironmentVariable("NETCOREDBG_TYPED_BITBLT_SOURCE_SHA256");
        if (string.IsNullOrWhiteSpace(sourceIdentity))
            throw new InvalidOperationException("Missing NETCOREDBG_TYPED_BITBLT_SOURCE_SHA256.");

        var identityPath = Path.GetFullPath(traceFile) + ".identity.json";
        var parent = Path.GetDirectoryName(identityPath);
        if (string.IsNullOrEmpty(parent))
            throw new InvalidOperationException("Identity file must have a parent directory.");
        Directory.CreateDirectory(parent);
        File.WriteAllText(identityPath, JsonSerializer.Serialize(new
        {
            source_identity = sourceIdentity,
            host_assembly = ManagedAssemblyIdentity(typeof(Program).Assembly),
            bridge_assembly = ManagedAssemblyIdentity(typeof(ScreenshotCommands).Assembly),
        }));
    }

    private static object ManagedAssemblyIdentity(Assembly assembly)
    {
        var assemblyName = assembly.GetName().Name;
        var path = string.IsNullOrWhiteSpace(assemblyName)
            ? string.Empty
            : Path.Combine(AppContext.BaseDirectory, $"{assemblyName}.dll");
        if (!File.Exists(path))
            throw new InvalidOperationException("Managed test-host assembly path is unavailable.");

        using var stream = File.OpenRead(path);
        return new
        {
            path,
            sha256 = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant(),
        };
    }
    private sealed class BlackPrimaryCaptureTransport(string traceFile) : IScreenshotCaptureTransport
    {
        private readonly bool _shortPrimary = string.Equals(
            Environment.GetEnvironmentVariable("NETCOREDBG_TYPED_BITBLT_PRIMARY_SHAPE"),
            "short",
            StringComparison.Ordinal);
        private readonly bool _foreignSnapshotProcess = string.Equals(
            Environment.GetEnvironmentVariable("NETCOREDBG_TYPED_BITBLT_SNAPSHOT_PROCESS"),
            "foreign",
            StringComparison.Ordinal);
        private readonly NativeScreenshotCaptureTransport _native = new();
        private readonly string _traceFile = Path.GetFullPath(traceFile);
        private int _foregroundSequence;

        public CaptureSnapshot ReadSnapshot(IntPtr hwnd)
        {
            var inputs = new { hwnd = hwnd.ToInt64() };
            var snapshot = ObserveNative("ReadSnapshot", inputs, () => _native.ReadSnapshot(hwnd));
            var returned = _foreignSnapshotProcess
                ? snapshot with { ProcessId = checked((uint)Environment.ProcessId) }
                : snapshot;
            if (_foreignSnapshotProcess)
                RecordForeground("ReadSnapshot", "deliberate_test_override", inputs, returned);
            return returned;
        }

        public Bitmap? CapturePrintWindow(IntPtr hwnd, int width, int height)
        {
            using var actual = _native.CapturePrintWindow(hwnd, width, height);
            if (actual is null)
                return null;

            AppendTrace("primary");
            var black = new Bitmap(_shortPrimary ? 1 : width, _shortPrimary ? 1 : height);
            using var graphics = Graphics.FromImage(black);
            graphics.Clear(Color.Black);
            return black;
        }

        public Bitmap CaptureBitBlt(IntPtr hwnd, int width, int height)
        {
            var actual = ObserveNative("CaptureBitBlt", new { hwnd = hwnd.ToInt64(), width, height },
                () => _native.CaptureBitBlt(hwnd, width, height));
            AppendTrace("alternate");
            return actual;
        }

        public IntPtr GetForegroundWindow() =>
            ObserveNative("GetForegroundWindow", null, _native.GetForegroundWindow);

        public bool RestoreWindow(IntPtr hwnd) => _native.RestoreWindow(hwnd);

        public ForegroundTransition ActivateForegroundVerified(
            IntPtr hwnd,
            uint expectedProcessId,
            IntPtr requiredForeground,
            uint requiredForegroundProcessId) =>
            ObserveNative("ActivateForegroundVerified", new
            {
                hwnd = hwnd.ToInt64(),
                expectedProcessId,
                requiredForeground = requiredForeground.ToInt64(),
                requiredForegroundProcessId,
            }, () => _native.ActivateForegroundVerified(
                hwnd, expectedProcessId, requiredForeground, requiredForegroundProcessId));

        public bool SetForegroundWindow(IntPtr hwnd) => _native.SetForegroundWindow(hwnd);

        private T ObserveNative<T>(string operation, object? inputs, Func<T> call)
        {
            RecordForeground(operation, "enter", inputs, null);
            var started = Stopwatch.GetTimestamp();
            try
            {
                var result = call();
                var elapsedMs = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
                object? actual = result is IntPtr handle ? handle.ToInt64()
                    : result is Bitmap ? "bitmap_returned" : result;
                RecordForeground(operation, "return", inputs, actual, elapsedMs);
                return result;
            }
            catch (Exception error)
            {
                RecordForeground(operation, "exception", inputs,
                    new { type = error.GetType().FullName, error.Message, error.HResult },
                    Stopwatch.GetElapsedTime(started).TotalMilliseconds);
                throw;
            }
        }

        private void RecordForeground(
            string operation, string phase, object? inputs, object? actual, double elapsedMs = 0)
        {
            if (_foregroundSequence >= 256) return;
            var sequence = ++_foregroundSequence;
            try
            {
                var entry = new { sequence, operation, phase, inputs, actual, elapsed_ms = elapsedMs };
                File.AppendAllText(_traceFile + ".foreground.jsonl",
                    JsonSerializer.Serialize(entry) + Environment.NewLine);
            }
            catch (Exception traceError)
            {
                // Diagnostic I/O must not replace the native return value or exception.
                try { Console.Error.WriteLine($"Foreground trace unavailable: {traceError.GetType().Name}"); }
                catch { }
            }
        }

        private void AppendTrace(string operation)
        {
            var parent = Path.GetDirectoryName(_traceFile);
            if (string.IsNullOrEmpty(parent))
                throw new InvalidOperationException("Trace file must have a parent directory.");
            Directory.CreateDirectory(parent);
            File.AppendAllText(_traceFile, operation + Environment.NewLine);
        }
    }
}

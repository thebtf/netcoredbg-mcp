using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using FlaUIBridge;
using FlaUIBridge.Commands;

namespace TypedBitBltFallbackBridgeHost;

internal static class Program
{
    private const string TraceFileEnvironmentVariable = "NETCOREDBG_COMPOSITOR_TRACE_FILE";

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
        var sourceIdentity = Environment.GetEnvironmentVariable("NETCOREDBG_COMPOSITOR_SOURCE_SHA256");
        if (string.IsNullOrWhiteSpace(sourceIdentity))
            throw new InvalidOperationException("Missing NETCOREDBG_COMPOSITOR_SOURCE_SHA256.");

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
            Environment.GetEnvironmentVariable("NETCOREDBG_COMPOSITOR_PRIMARY_SHAPE"),
            "short",
            StringComparison.Ordinal);
        private readonly bool _foreignSnapshotProcess = string.Equals(
            Environment.GetEnvironmentVariable("NETCOREDBG_COMPOSITOR_SNAPSHOT_PROCESS"),
            "foreign",
            StringComparison.Ordinal);
        private readonly NativeScreenshotCaptureTransport _native = new();
        private readonly string _traceFile = Path.GetFullPath(traceFile);
        private readonly string _mode = Environment.GetEnvironmentVariable("NETCOREDBG_COMPOSITOR_CAPTURE_MODE") ?? "real";
        private bool _alternateCompleted;
        private CaptureSnapshot? _acceptanceSnapshot;
        private uint _acceptanceAffinity;
        private IntPtr _acceptanceHwnd;
        private Func<bool>? _acceptanceClosedObserver;

        [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", SetLastError = true)]
        private static extern IntPtr SendMessageTimeout(
            IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam, uint flags, uint timeoutMilliseconds,
            out IntPtr result);

        public CaptureSnapshot ReadSnapshot(IntPtr hwnd)
        {
            if (_acceptanceClosedObserver is Func<bool> observedClosed)
            {
                _acceptanceClosedObserver = null;
                if (hwnd != _acceptanceHwnd)
                    throw new InvalidOperationException("Acceptance validation changed the controlled HWND.");
                ReplaceDuringValidation(hwnd, observedClosed);
            }
            if (_acceptanceSnapshot is CaptureSnapshot equivalentIdentity)
                return equivalentIdentity;
            var snapshot = _native.ReadSnapshot(hwnd);
            if (_foreignSnapshotProcess)
                return snapshot with { ProcessId = checked((uint)Environment.ProcessId) };
            if (_alternateCompleted)
            {
                return _mode switch
                {
                    "target-change" => snapshot with { Hwnd = checked(snapshot.Hwnd + 1) },
                    "process-change" => snapshot with { ProcessCreationTicks = checked(snapshot.ProcessCreationTicks + 1) },
                    "resize" => snapshot with { WindowRight = checked(snapshot.WindowRight + 1) },
                    _ => snapshot,
                };
            }
            return snapshot;
        }

        public uint ReadCaptureAffinity(IntPtr hwnd) => _acceptanceSnapshot is not null
            ? _acceptanceAffinity
            : _mode switch
            {
                "policy-unknown" => throw new InvalidOperationException("Capture policy is UNKNOWN in the controlled transport."),
                "protected" => 0x11,
                _ => _native.ReadCaptureAffinity(hwnd),
            };

        public Bitmap? CapturePrintWindow(IntPtr hwnd, int width, int height)
        {
            AppendTrace("primary");
            using var actual = _native.CapturePrintWindow(hwnd, width, height);
            if (actual is null)
                return null;

            var black = new Bitmap(_shortPrimary ? 1 : width, _shortPrimary ? 1 : height);
            using var graphics = Graphics.FromImage(black);
            graphics.Clear(Color.Black);
            return black;
        }

        public CompositorCapture CaptureCompositorWindow(
            IntPtr hwnd, CaptureSnapshot expected, CancellationToken cancellationToken)
        {
            AppendTrace("alternate");
            if (_mode == "timeout")
            {
                using var cancelled = new CancellationTokenSource();
                cancelled.Cancel();
                try
                {
                    return _native.CaptureCompositorWindow(hwnd, expected, cancelled.Token);
                }
                catch (OperationCanceledException ex)
                {
                    throw new TimeoutException("Controlled compositor acquisition timed out.", ex);
                }
            }
            if (_mode == "close-at-acceptance")
                return CompositorWindowCapture.Capture(hwnd, expected, this, cancellationToken, CloseAtAcceptance);
            var actual = _native.CaptureCompositorWindow(hwnd, expected, cancellationToken);
            _alternateCompleted = true;
            return _mode == "closed" ? actual with { WindowClosed = true } : actual;
        }

        private void CloseAtAcceptance(IntPtr hwnd, Func<bool> observedClosed)
        {
            _acceptanceHwnd = hwnd;
            _acceptanceClosedObserver = observedClosed;
        }

        private void ReplaceDuringValidation(IntPtr hwnd, Func<bool> observedClosed)
        {
            var equivalentIdentity = _native.ReadSnapshot(hwnd);
            var affinity = _native.ReadCaptureAffinity(hwnd);
            if (SendMessageTimeout(hwnd, 0x802B, IntPtr.Zero, IntPtr.Zero, 3, 1_000, out var replacement) == IntPtr.Zero ||
                replacement == IntPtr.Zero)
                throw new InvalidOperationException("The owned calibration window could not be replaced within the bounded request.");
            var actualReplacement = _native.ReadSnapshot(replacement);
            if ((actualReplacement with { Hwnd = equivalentIdentity.Hwnd }) != equivalentIdentity ||
                _native.ReadCaptureAffinity(replacement) != affinity)
                throw new InvalidOperationException("The replacement changed the owned calibration process, geometry or policy.");
            AppendTrace("acceptance-window-replaced");
            // The window really closes; equal identity/policy model ABA, not a promised numeric HWND reuse.
            _acceptanceSnapshot = equivalentIdentity;
            _acceptanceAffinity = affinity;
            var wait = Stopwatch.StartNew();
            while (!observedClosed() && wait.ElapsedMilliseconds < 3_000)
                Thread.Sleep(10);
            AppendTrace(observedClosed() ? "acceptance-close-observed" : "acceptance-close-not-observed");
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

using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.ExceptionServices;
using System.Security.Cryptography;
using System.Text;
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
        using var diagnostics = string.Equals(
            Environment.GetEnvironmentVariable("NETCOREDBG_COMPOSITOR_NATIVE_DIAGNOSTICS"),
            "1", StringComparison.Ordinal)
            ? new NativeCaptureDiagnostics(traceFile)
            : null;

        using var scope = ScreenshotCommands.PushCaptureTransportForTesting(
            new BlackPrimaryCaptureTransport(traceFile, diagnostics));
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
    private sealed class NativeCaptureDiagnostics : IDisposable
    {
        private const int MaximumRecords = 96;
        private const int MaximumBytes = 256 * 1024;
        private const int MaximumExceptionRecords = 16;
        private const int MaximumExceptionCharacters = 8 * 1024;
        private readonly string _path;
        private readonly object _sync = new();
        private readonly Stopwatch _elapsed = Stopwatch.StartNew();
        private int _active;
        private int _attempt;
        private int _exceptionRecords;
        private int _records;
        private long _bytes;
        private bool _sinkFailed;
        [ThreadStatic] private static bool _writing;

        public NativeCaptureDiagnostics(string traceFile)
        {
            _path = Path.GetFullPath(traceFile) + ".native-diagnostics.jsonl";
            _bytes = File.Exists(_path) ? new FileInfo(_path).Length : 0;
            Progress("diagnostic-enabled");
        }

        public void Begin()
        {
            Interlocked.Increment(ref _attempt);
            Volatile.Write(ref _active, 1);
            AppDomain.CurrentDomain.FirstChanceException += OnFirstChance;
            Progress("capture-enter");
        }

        public void End()
        {
            AppDomain.CurrentDomain.FirstChanceException -= OnFirstChance;
            Volatile.Write(ref _active, 0);
            Progress("capture-end");
        }

        public void Dispose()
        {
            if (Volatile.Read(ref _active) != 0)
                End();
        }

        public void Progress(string phase) => Record(phase, null);

        public void Failure(Exception exception) => Record("capture-threw", exception);

        private void OnFirstChance(object? sender, FirstChanceExceptionEventArgs args)
        {
            if (_writing || Volatile.Read(ref _active) == 0)
                return;
            var count = Interlocked.Increment(ref _exceptionRecords);
            if (count <= MaximumExceptionRecords)
                Record("first-chance", args.Exception);
            else if (count == MaximumExceptionRecords + 1)
                Progress("first-chance-limit-reached");
        }

        private void Record(string phase, Exception? exception)
        {
            if (_writing)
                return;
            _writing = true;
            try
            {
                lock (_sync)
                {
                    if (_sinkFailed || _records >= MaximumRecords || _bytes >= MaximumBytes)
                        return;
                    var details = exception?.ToString();
                    var originalLength = details?.Length ?? 0;
                    var entry = JsonSerializer.Serialize(new
                    {
                        sequence = _records + 1,
                        attempt = Volatile.Read(ref _attempt),
                        phase,
                        utc = DateTimeOffset.UtcNow,
                        elapsed_ms = _elapsed.ElapsedMilliseconds,
                        process_id = Environment.ProcessId,
                        managed_thread_id = Environment.CurrentManagedThreadId,
                        apartment = Thread.CurrentThread.GetApartmentState().ToString(),
                        exception_type = exception?.GetType().FullName,
                        hresult = exception is null ? null : $"0x{exception.HResult:X8}",
                        exception_details = originalLength > MaximumExceptionCharacters
                            ? details![..MaximumExceptionCharacters] : details,
                        exception_details_characters = originalLength,
                        exception_details_truncated = originalLength > MaximumExceptionCharacters,
                    }) + Environment.NewLine;
                    var bytes = Encoding.UTF8.GetByteCount(entry);
                    if (_records == MaximumRecords - 1 || _bytes + bytes > MaximumBytes - 512)
                    {
                        entry = "{\"phase\":\"diagnostic-capacity-reached\"}" + Environment.NewLine;
                        bytes = Encoding.UTF8.GetByteCount(entry);
                        if (_bytes + bytes > MaximumBytes)
                            return;
                        _sinkFailed = true;
                    }
                    File.AppendAllText(_path, entry);
                    _bytes += bytes;
                    _records++;
                }
            }
            catch (Exception)
            {
                // A diagnostic sink failure must not replace the capture's original exception.
                lock (_sync)
                    _sinkFailed = true;
            }
            finally
            {
                _writing = false;
            }
        }
    }

    private sealed class BlackPrimaryCaptureTransport(string traceFile, NativeCaptureDiagnostics? diagnostics) : IScreenshotCaptureTransport
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
            diagnostics?.Begin();
            try
            {
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
                {
                    var closedCapture = CompositorWindowCapture.Capture(hwnd, expected, this,
                        cancellationToken, CloseAtAcceptance, diagnostics is null ? null : diagnostics.Progress);
                    diagnostics?.Progress("capture-return");
                    return closedCapture;
                }
                var actual = diagnostics is null
                    ? _native.CaptureCompositorWindow(hwnd, expected, cancellationToken)
                    : CompositorWindowCapture.Capture(hwnd, expected, _native, cancellationToken,
                        progressForTesting: diagnostics.Progress);
                diagnostics?.Progress("capture-return");
                _alternateCompleted = true;
                return _mode == "closed" ? actual with { WindowClosed = true } : actual;
            }
            catch (Exception exception)
            {
                diagnostics?.Failure(exception);
                throw;
            }
            finally
            {
                diagnostics?.End();
            }
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

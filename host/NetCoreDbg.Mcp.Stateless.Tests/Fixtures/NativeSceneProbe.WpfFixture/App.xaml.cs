using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Windows;
using System.Windows.Interop;
using NetCoreDbg.Mcp.DesignProbe.Wpf;


namespace NativeSceneProbe.WpfFixture;

public partial class App : Application
{
    private LocalProbeClient? _probeClient;
    private readonly CancellationTokenSource _readinessCancellation = new();
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        if (!FixtureStartupOptions.TryCreate(e.Args, out var options) || options is null)
        {
            Shutdown(-1);
            return;
        }
        var startupBarrier = Environment.GetEnvironmentVariable("NETCOREDBG_NATIVE_SCENE_PROBE_FIXTURE_STARTUP_BARRIER");
        if (!string.IsNullOrWhiteSpace(startupBarrier))
        {
            using var started = EventWaitHandle.OpenExisting(startupBarrier + "-started");
            using var release = EventWaitHandle.OpenExisting(startupBarrier + "-release");
            started.Set();
            release.WaitOne();
        }


        var window = new ProbeFixtureWindow(options.Mode);
        window.ContentRendered += SignalWindowReadinessAsync;
        MainWindow = window;
        ShutdownMode = ShutdownMode.OnMainWindowClose;
        window.Show();
        _probeClient = LocalProbeClient.TryStartFromEnvironment(
            new WpfAtomicSnapshotTransaction(window.Dispatcher, (IWpfProbeSnapshotSource)window));
    }

    private async void SignalWindowReadinessAsync(object? sender, EventArgs e)
    {
        var window = (Window)sender!;
        window.ContentRendered -= SignalWindowReadinessAsync;
        var pipeName = Environment.GetEnvironmentVariable("CONTROLLED_DAP_WINDOWED_DESCENDANT_READINESS_PIPE");
        if (string.IsNullOrWhiteSpace(pipeName))
        {
            return;
        }

        using var process = Process.GetCurrentProcess();
        var handle = new WindowInteropHelper(window).Handle;
        if (handle == IntPtr.Zero || string.IsNullOrWhiteSpace(process.MainModule?.FileName))
        {
            throw new InvalidOperationException("WPF fixture rendered without loader/window readiness.");
        }

        using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.Out, PipeOptions.Asynchronous);
        try
        {
            await pipe.ConnectAsync(_readinessCancellation.Token);
            var payload = new byte[sizeof(long)];
            BinaryPrimitives.WriteInt64LittleEndian(payload, handle.ToInt64());
            await pipe.WriteAsync(payload, _readinessCancellation.Token);
            await pipe.FlushAsync(_readinessCancellation.Token);
        }
        catch (OperationCanceledException) when (_readinessCancellation.IsCancellationRequested)
        {
            // Application exit cancels an unpublished readiness signal.
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _readinessCancellation.Cancel();
        _probeClient?.Dispose();
        _probeClient = null;
        base.OnExit(e);
        _readinessCancellation.Dispose();
    }

}

internal sealed record FixtureStartupOptions(ProbeFixtureMode Mode)
{
    private const string HarnessArgument = "--native-scene-probe-test-harness";
    private const string ModeArgumentPrefix = "--native-scene-probe-mode=";
    private const string ModeEnvironmentVariable = "NETCOREDBG_NATIVE_SCENE_PROBE_FIXTURE_MODE";

    public static bool TryCreate(string[] arguments, out FixtureStartupOptions? options)
    {
        options = null;

        if (arguments.Count(argument => string.Equals(argument, HarnessArgument, StringComparison.Ordinal)) != 1)
        {
            return false;
        }

        var commandLineModes = arguments
            .Where(argument => argument.StartsWith(ModeArgumentPrefix, StringComparison.Ordinal))
            .Select(argument => argument[ModeArgumentPrefix.Length..])
            .ToArray();
        if (commandLineModes.Length > 1)
        {
            return false;
        }

        var environmentMode = Environment.GetEnvironmentVariable(ModeEnvironmentVariable);
        if (commandLineModes.Length == 1
            && !string.IsNullOrWhiteSpace(environmentMode)
            && !string.Equals(commandLineModes[0], environmentMode, StringComparison.Ordinal))
        {
            return false;
        }

        var modeName = commandLineModes.Length == 1 ? commandLineModes[0] : environmentMode;
        if (string.IsNullOrWhiteSpace(modeName) || !ProbeFixtureModeParser.TryParse(modeName, out var mode))
        {
            return false;
        }

        options = new FixtureStartupOptions(mode);
        return true;
    }
}

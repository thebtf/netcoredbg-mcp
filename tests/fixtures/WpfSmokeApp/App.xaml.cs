using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Windows;
using System.Windows.Interop;

namespace WpfSmokeApp;

public partial class App : Application
{
    private CancellationTokenSource? _readinessCancellation;
    private Task? _readinessTask;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var startupBarrier = Environment.GetEnvironmentVariable("NETCOREDBG_WPF_SMOKE_FIXTURE_STARTUP_BARRIER");
        if (!string.IsNullOrWhiteSpace(startupBarrier))
        {
            using var started = EventWaitHandle.OpenExisting(startupBarrier + "-started");
            using var release = EventWaitHandle.OpenExisting(startupBarrier + "-release");
            started.Set();
            release.WaitOne();
        }
    }

    internal void OnMainWindowContentRendered(object? sender, EventArgs e)
    {
        var window = (Window)sender!;
        window.ContentRendered -= OnMainWindowContentRendered;
        var pipeName = Environment.GetEnvironmentVariable("CONTROLLED_DAP_WINDOWED_DESCENDANT_READINESS_PIPE");
        if (string.IsNullOrWhiteSpace(pipeName))
        {
            return;
        }

        _readinessCancellation = new CancellationTokenSource();
        _readinessTask = PublishWindowReadinessAsync(window, pipeName, _readinessCancellation.Token);
    }

    private async Task PublishWindowReadinessAsync(Window window, string pipeName, CancellationToken cancellationToken)
    {
        try
        {
            using var process = Process.GetCurrentProcess();
            var handle = new WindowInteropHelper(window).Handle;
            if (!ReferenceEquals(window, MainWindow) || !window.IsLoaded || !window.IsVisible
                || window.WindowState == WindowState.Minimized || handle == IntPtr.Zero
                || string.IsNullOrWhiteSpace(process.MainModule?.FileName))
            {
                throw new InvalidOperationException("WPF fixture rendered without visible main-window/loader readiness.");
            }

            using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.Out, PipeOptions.Asynchronous);
            await pipe.ConnectAsync(cancellationToken).ConfigureAwait(false);
            var payload = new byte[sizeof(long)];
            BinaryPrimitives.WriteInt64LittleEndian(payload, handle.ToInt64());
            await pipe.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
            await pipe.FlushAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // Application exit cancels an unpublished readiness signal.
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"WPF fixture readiness failed: {exception}");
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            _readinessCancellation?.Cancel();
            _readinessTask?.GetAwaiter().GetResult();
        }
        finally
        {
            _readinessCancellation?.Dispose();
            base.OnExit(e);
        }
    }
}

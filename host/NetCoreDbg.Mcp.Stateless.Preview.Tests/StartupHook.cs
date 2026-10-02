using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

internal static class StartupHook
{
    internal const string StartupBarrierEnvironmentVariable = "NETCOREDBG_PREVIEW_TEST_STARTUP_BARRIER";
    internal const string ExitBarrierEnvironmentVariable = "NETCOREDBG_PREVIEW_TEST_EXIT_BARRIER";
    internal const string HostReadyEnvironmentVariable = "NETCOREDBG_PREVIEW_TEST_HOST_READY";

    public static void Initialize()
    {
        var readyName = Environment.GetEnvironmentVariable(HostReadyEnvironmentVariable);
        if (!string.IsNullOrEmpty(readyName))
        {
            var observer = new HostStartupObserver(readyName);
            AppDomain.CurrentDomain.ProcessExit += (_, _) => observer.Dispose();
        }

        var startupBarrier = Environment.GetEnvironmentVariable(StartupBarrierEnvironmentVariable);
        if (!string.IsNullOrEmpty(startupBarrier))
        {
            WaitAtBarrier(startupBarrier);
        }

        var exitBarrier = Environment.GetEnvironmentVariable(ExitBarrierEnvironmentVariable);
        if (!string.IsNullOrEmpty(exitBarrier))
        {
            AppDomain.CurrentDomain.ProcessExit += (_, _) => WaitAtBarrier(exitBarrier);
        }
    }

    private static void WaitAtBarrier(string name)
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException("Preview startup barriers require Windows.");
        }

        using var started = EventWaitHandle.OpenExisting(name + "-started");
        using var release = EventWaitHandle.OpenExisting(name + "-release");
        started.Set();
        release.WaitOne();
    }

    private sealed class HostStartupObserver : IObserver<DiagnosticListener>, IObserver<KeyValuePair<string, object?>>, IDisposable
    {
        private readonly string _readyName;
        private readonly IDisposable _allListeners;
        private IDisposable? _hostListener;
        private CancellationTokenRegistration _startedRegistration;

        internal HostStartupObserver(string readyName)
        {
            _readyName = readyName;
            _allListeners = DiagnosticListener.AllListeners.Subscribe(this);
        }

        public void OnNext(DiagnosticListener listener)
        {
            if (listener.Name == "Microsoft.Extensions.Hosting")
            {
                _hostListener = listener.Subscribe(this, static name => name == "HostBuilt");
            }
        }

        public void OnNext(KeyValuePair<string, object?> diagnostic)
        {
            if (diagnostic.Key == "HostBuilt" && diagnostic.Value is IHost host)
            {
                var lifetime = host.Services.GetRequiredService<IHostApplicationLifetime>();
                _startedRegistration = lifetime.ApplicationStarted.Register(() =>
                {
                    if (!OperatingSystem.IsWindows())
                    {
                        throw new PlatformNotSupportedException("Preview host-ready events require Windows.");
                    }

                    using var ready = EventWaitHandle.OpenExisting(_readyName);
                    ready.Set();
                });
                _hostListener?.Dispose();
                _allListeners.Dispose();
            }
        }

        public void OnCompleted() { }
        public void OnError(Exception error) => throw error;

        public void Dispose()
        {
            _startedRegistration.Dispose();
            _hostListener?.Dispose();
            _allListeners.Dispose();
        }
    }
}

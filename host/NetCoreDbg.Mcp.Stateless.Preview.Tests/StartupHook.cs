internal static class StartupHook
{
    internal const string StartupBarrierEnvironmentVariable = "NETCOREDBG_PREVIEW_TEST_STARTUP_BARRIER";
    internal const string ExitBarrierEnvironmentVariable = "NETCOREDBG_PREVIEW_TEST_EXIT_BARRIER";

    public static void Initialize()
    {
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
}

using System.Windows;
using NetCoreDbg.Mcp.DesignProbe.Wpf;


namespace NativeSceneProbe.WpfFixture;

public partial class App : Application
{
    private LocalProbeClient? _probeClient;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        if (!FixtureStartupOptions.TryCreate(e.Args, out var options) || options is null)
        {
            Shutdown(-1);
            return;
        }

        var window = new ProbeFixtureWindow(options.Mode);
        MainWindow = window;
        ShutdownMode = ShutdownMode.OnMainWindowClose;
        window.Show();
        _probeClient = LocalProbeClient.TryStartFromEnvironment(
            new WpfAtomicSnapshotTransaction(window.Dispatcher, (IWpfProbeSnapshotSource)window));
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _probeClient?.Dispose();
        _probeClient = null;
        base.OnExit(e);
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

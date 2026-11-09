using System.Runtime.InteropServices;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless;

internal static class NativeSetup
{
    internal static async Task<bool> RunAsync()
    {
        try
        {
            var processPath = Environment.ProcessPath;
            if (string.IsNullOrEmpty(processPath) || !File.Exists(processPath))
                return await ErrorAsync("setup could not identify the current native host executable.").ConfigureAwait(false);

            var args = new List<string>();
            if (string.Equals(Path.GetFileNameWithoutExtension(processPath), "dotnet", StringComparison.OrdinalIgnoreCase))
            {
                var assembly = typeof(NativeSetup).Assembly.Location;
                if (string.IsNullOrEmpty(assembly) || !File.Exists(assembly))
                    return await ErrorAsync("setup could not identify the current managed host assembly.").ConfigureAwait(false);
                args.Add(Path.GetFullPath(assembly));
            }
            args.Add("--legacy-compat");
            args.Add("--project-from-cwd");

            var configuredBridge = Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH");
            string? bridge = null;
            if (!string.IsNullOrEmpty(configuredBridge))
            {
                bridge = ValidBridge(configuredBridge);
                if (bridge is null)
                    return await ErrorAsync("FLAUI_BRIDGE_PATH does not identify an existing bridge; no fallback was selected.").ConfigureAwait(false);
            }
            else if (OperatingSystem.IsWindows() && LegacyDebuggerDiscovery.GetHomeDirectory() is { Length: > 0 } home)
            {
                bridge = ValidBridge(Path.Combine(home, ".netcoredbg-mcp", "bridge", "FlaUIBridge.exe"));
            }
            var configured = Environment.GetEnvironmentVariable("NETCOREDBG_PATH");
            var selected = LegacyDebuggerDiscovery.Resolve(configured);
            var debugger = selected is null ? null : DapLaunchEnvironment.FindExecutable(selected);
            if (debugger is null && selected is not null)
                return await ErrorAsync(!string.IsNullOrEmpty(configured)
                    ? "NETCOREDBG_PATH does not identify an executable file; no fallback was selected."
                    : "The discovered debugger is not executable; no replacement was installed.").ConfigureAwait(false);

            var downloaded = false;
            if (debugger is null)
            {
                var homeDirectory = LegacyDebuggerDiscovery.GetHomeDirectory();
                var asset = CurrentAsset();
                if (string.IsNullOrWhiteSpace(homeDirectory) || asset is null)
                    return await ErrorAsync("No managed home or supported debugger release is available for this platform.").ConfigureAwait(false);
                var managedRoot = Path.Combine(Path.GetFullPath(homeDirectory), ".netcoredbg-mcp");
                Directory.CreateDirectory(managedRoot);
                using var client = NativeDebuggerInstaller.CreateHttpClient();
                client.Timeout = TimeSpan.FromSeconds(120);
                try
                {
                    debugger = await NativeDebuggerInstaller.InstallAsync(client,
                        new Uri("https://api.github.com/repos/Samsung/netcoredbg/releases/latest"),
                        Path.Combine(managedRoot, "netcoredbg"), asset, CancellationToken.None).ConfigureAwait(false);
                }
                catch (NativeDebuggerInstalledCleanupException exception)
                {
                    await Console.Error.WriteLineAsync($"Warning: {exception.Message}").ConfigureAwait(false);
                    debugger = DapLaunchEnvironment.FindExecutable(exception.ExecutablePath);
                    if (debugger is null)
                        return await ErrorAsync("The installed debugger is no longer available; inspect the reported paths.").ConfigureAwait(false);
                }
                downloaded = true;
            }

            var environment = new Dictionary<string, string> { ["NETCOREDBG_PATH"] = debugger };
            if (bridge is not null) environment["FLAUI_BRIDGE_PATH"] = bridge;

            var config = new { mcpServers = new Dictionary<string, object> { ["netcoredbg"] = new { command = Path.GetFullPath(processPath), args, env = environment } } };
            await Console.Error.WriteLineAsync(downloaded ? "Downloaded a verified managed debugger." : "Existing debugger reused; no download was needed.").ConfigureAwait(false);
            await Console.Error.WriteLineAsync("Warning: setup does not cache dbgshim or build the FlaUI bridge; these remain unimplemented.").ConfigureAwait(false);
            await Console.Out.WriteLineAsync(JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true })).ConfigureAwait(false);
            return true;
        }
        catch (Exception exception) when (IsReportableFailure(exception))
        {
            return await ErrorAsync($"setup failed: {exception.Message}").ConfigureAwait(false);
        }
    }

    internal static bool IsReportableFailure(Exception exception) => exception is InvalidDataException or IOException
        or UnauthorizedAccessException or ArgumentException or NotSupportedException or System.Security.SecurityException
        or HttpRequestException or JsonException or OperationCanceledException or AggregateException
        or EntryPointNotFoundException or DllNotFoundException;

    internal static string? CurrentAsset()
    {
        var architecture = RuntimeInformation.ProcessArchitecture;
        if (OperatingSystem.IsWindows()) return architecture == Architecture.X64 ? "win64" : null;
        if (OperatingSystem.IsLinux()) return architecture switch
        {
            Architecture.X64 => "linux-amd64",
            Architecture.Arm64 => "linux-arm64",
            _ => null,
        };
        if (OperatingSystem.IsMacOS()) return architecture == Architecture.Arm64 ? "osx-arm64" : null;
        return null;
    }

    private static string? ValidBridge(string path)
    {
        var fullPath = Path.GetFullPath(path);
        return File.Exists(fullPath) && (fullPath.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)
            || fullPath.EndsWith(".dll", StringComparison.OrdinalIgnoreCase)) ? fullPath : null;
    }

    private static async Task<bool> ErrorAsync(string message)
    {
        await Console.Error.WriteLineAsync($"netcoredbg-mcp-stateless: error: {message}").ConfigureAwait(false);
        Environment.ExitCode = 1;
        return true;
    }
}

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyDebuggerDiscovery
{
    internal static string? Resolve(string? configuredPath)
    {
        if (!string.IsNullOrEmpty(configuredPath)) return configuredPath;

        var home = OperatingSystem.IsWindows()
            ? Environment.GetEnvironmentVariable("USERPROFILE")
            : Environment.GetEnvironmentVariable("HOME");
        if (OperatingSystem.IsWindows() && home is null
            && Environment.GetEnvironmentVariable("HOMEPATH") is { } homePath)
        {
            var drive = Environment.GetEnvironmentVariable("HOMEDRIVE") ?? string.Empty;
            home = Path.IsPathFullyQualified(homePath) || (homePath.Length >= 2 && homePath[1] == ':')
                ? homePath
                : Path.IsPathRooted(homePath) || drive.EndsWith(':') ? drive + homePath : Path.Combine(drive, homePath);
        }
        if (!OperatingSystem.IsWindows() && home is null) home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (!string.IsNullOrEmpty(home))
        {
            try
            {
                var directory = Path.Combine(home, ".netcoredbg-mcp", "netcoredbg");
                var name = OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg";
                var direct = Path.Combine(directory, name);
                if (File.Exists(direct)) return Path.GetFullPath(direct);
                if (Directory.Exists(directory))
                {
                    foreach (var child in Directory.EnumerateDirectories(directory))
                    {
                        var nested = Path.Combine(child, name);
                        if (File.Exists(nested)) return Path.GetFullPath(nested);
                    }
                }
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                or ArgumentException or NotSupportedException or System.Security.SecurityException)
            {
                return null;
            }
        }

        return DapLaunchEnvironment.FindExecutable("netcoredbg");
    }
}

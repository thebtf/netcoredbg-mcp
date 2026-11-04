using System.Runtime.InteropServices;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class DapLaunchEnvironment
{
    private const int ExecuteAccess = 1;

    internal static StringComparer NameComparer => OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

    internal static HashSet<string>? Removals(IReadOnlyDictionary<string, string?>? overrides)
    {
        HashSet<string>? removals = null;
        if (overrides is null) return null;
        foreach (var (name, value) in overrides)
        {
            if (value is null) (removals ??= new HashSet<string>(NameComparer)).Add(name);
        }
        return removals;
    }

    internal static IReadOnlyDictionary<string, string>? Merge(
        IReadOnlyDictionary<string, string>? probeEnvironment,
        IReadOnlyDictionary<string, string?>? overrides)
    {
        var hasOverrides = overrides is { Count: > 0 };
        string? windir = null;
        string? systemRoot = null;
        var windirExplicit = false;
        var systemRootExplicit = false;
        var syncWindir = false;
        var syncSystemRoot = false;
        string? aliasValue = null;
        if (OperatingSystem.IsWindows())
        {
            windirExplicit = overrides?.TryGetValue("WINDIR", out windir) == true;
            systemRootExplicit = overrides?.TryGetValue("SYSTEMROOT", out systemRoot) == true;
            if (!windirExplicit) windir = Environment.GetEnvironmentVariable("WINDIR");
            if (!systemRootExplicit) systemRoot = Environment.GetEnvironmentVariable("SYSTEMROOT");
            aliasValue = windirExplicit && windir is not null ? windir
                : systemRootExplicit && systemRoot is not null ? systemRoot
                : windir ?? systemRoot;
            if (aliasValue is not null)
            {
                syncWindir = !windirExplicit && !string.Equals(windir, aliasValue, StringComparison.Ordinal);
                syncSystemRoot = !systemRootExplicit && !string.Equals(systemRoot, aliasValue, StringComparison.Ordinal);
            }
        }
        if (!hasOverrides && !syncWindir && !syncSystemRoot) return probeEnvironment;

        var result = new Dictionary<string, string>(NameComparer);
        if (probeEnvironment is not null)
        {
            foreach (var (name, value) in probeEnvironment) result.Add(name, value);
        }
        if (overrides is not null)
        {
            foreach (var (name, value) in overrides)
            {
                if (value is not null) result[name] = value;
            }
        }
        if (syncWindir) result["WINDIR"] = aliasValue!;
        if (syncSystemRoot) result["SYSTEMROOT"] = aliasValue!;
        return result;
    }

    internal static string? FindExecutable(string name)
    {
        if (name.IndexOfAny([Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar]) >= 0)
            return ExistingExecutable(name);
        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path)) return null;
        var fileName = OperatingSystem.IsWindows() && !Path.HasExtension(name) ? name + ".exe" : name;
        foreach (var directory in path.Split(Path.PathSeparator))
        {
            try
            {
                var candidate = ExistingExecutable(Path.Combine(directory.Length == 0 ? "." : directory, fileName));
                if (candidate is not null) return candidate;
            }
            catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
            {
            }
        }
        return null;
    }

    private static string? ExistingExecutable(string candidate)
    {
        if (!File.Exists(candidate)) return null;
        if (!OperatingSystem.IsWindows() && Access(candidate, ExecuteAccess) != 0) return null;
        return Path.GetFullPath(candidate);
    }

    [DllImport("libc", EntryPoint = "access")]
    private static extern int Access([MarshalAs(UnmanagedType.LPUTF8Str)] string path, int mode);
}

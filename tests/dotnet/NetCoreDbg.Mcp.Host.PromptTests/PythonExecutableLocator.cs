namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Resolves the Python interpreter used to launch the direct <c>netcoredbg_mcp</c> server
/// baseline these tests compare against. Test-only concern: production
/// <c>NativePrompts.cs</c> never launches Python at all, and <c>Program.cs</c>'s own
/// <c>NETCOREDBG_MCP_PYTHON_EXECUTABLE</c>/default-<c>python</c> resolution is untouched by
/// this file.
/// </summary>
internal static class PythonExecutableLocator
{
    private const string OverrideEnvironmentVariable = "NETCOREDBG_MCP_TEST_PYTHON_EXECUTABLE";

    public static string Resolve()
    {
        var overridden = Environment.GetEnvironmentVariable(OverrideEnvironmentVariable);
        if (!string.IsNullOrEmpty(overridden))
        {
            return overridden;
        }

        var repoRoot = FindRepoRoot(AppContext.BaseDirectory);
        if (repoRoot is not null)
        {
            var venvPython = OperatingSystem.IsWindows()
                ? Path.Combine(repoRoot, ".venv", "Scripts", "python.exe")
                : Path.Combine(repoRoot, ".venv", "bin", "python");
            if (File.Exists(venvPython))
            {
                return venvPython;
            }
        }

        return "python";
    }

    private static string? FindRepoRoot(string startDirectory)
    {
        var current = new DirectoryInfo(startDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "pyproject.toml"))
                && File.Exists(Path.Combine(current.FullName, "netcoredbg-mcp.sln")))
            {
                return current.FullName;
            }

            current = current.Parent;
        }

        return null;
    }
}

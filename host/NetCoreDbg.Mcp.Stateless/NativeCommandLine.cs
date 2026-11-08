using System.Reflection;

namespace NetCoreDbg.Mcp.Stateless;

internal static class NativeCommandLine
{
    internal static async Task<bool> HandleAsync(string[] arguments)
    {
        if (!arguments.Contains("--legacy-compat", StringComparer.Ordinal))
            return false;

        var projectSpecified = false;
        var projectFromCwd = false;
        var setupRequested = false;
        var encRequested = false;
        for (var index = 0; index < arguments.Length; index++)
        {
            var argument = arguments[index];

            switch (argument)
            {
                case "--legacy-compat":
                    break;
                case "--help":
                case "-h":
                    await Console.Out.WriteLineAsync(Help).ConfigureAwait(false);
                    return true;
                case "--version":
                    var version = typeof(NativeCommandLine).Assembly
                        .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
                        ?? typeof(NativeCommandLine).Assembly.GetName().Version?.ToString()
                        ?? "unknown";
                    await Console.Out.WriteLineAsync($"netcoredbg-mcp-stateless {version}").ConfigureAwait(false);
                    return true;
                case "--project-from-cwd":
                    projectFromCwd = true;
                    break;
                case "--project":
                    if (index + 1 >= arguments.Length || arguments[index + 1].StartsWith('-'))
                        return await ErrorAsync("--project requires a path").ConfigureAwait(false);
                    index++;
                    projectSpecified = true;
                    break;
                case "setup":
                case "--setup":
                    setupRequested = true;
                    break;
                case "--enc":
                    encRequested = true;
                    break;
                default:
                    if (argument.StartsWith("--project=", StringComparison.Ordinal))
                    {
                        projectSpecified = true;
                        break;
                    }
                    return await ErrorAsync($"unrecognized argument: {argument}").ConfigureAwait(false);
            }
        }

        if (encRequested && !setupRequested)
            return await ErrorAsync("--enc must be used with setup or --setup").ConfigureAwait(false);
        if (setupRequested)
            return await ErrorAsync("native setup is not available; use the installed Python netcoredbg-mcp setup command").ConfigureAwait(false);
        if (projectSpecified && projectFromCwd)
            return await ErrorAsync("--project-from-cwd cannot be used with --project").ConfigureAwait(false);
        return false;
    }

    private static async Task<bool> ErrorAsync(string message)
    {
        await Console.Error.WriteLineAsync($"netcoredbg-mcp-stateless: error: {message}").ConfigureAwait(false);
        Environment.ExitCode = 2;
        return true;
    }

    private const string Help = """
        Usage: netcoredbg-mcp-stateless --legacy-compat [--project PATH | --project-from-cwd]
               netcoredbg-mcp-stateless --legacy-compat [--help | --version]

        --legacy-compat     Run the experimental legacy-client MCP route.
        --project PATH      Set the default project root.
        --project-from-cwd  Detect the project root from the current directory.
        --help, -h          Show this help and exit.
        --version           Show the native host build version and exit.

        Native setup is not available; use the installed Python netcoredbg-mcp setup command.
        """;
}

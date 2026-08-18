using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Resolves the root for native source navigation without changing the Python session's
/// project scope. Operator-selected paths are authoritative; client roots remain a bounded,
/// local-only fallback.
/// </summary>
internal sealed class ProjectRootResolver
{
    private const string NetCoreDbgProjectRoot = "NETCOREDBG_PROJECT_ROOT";
    private const string McpProjectRoot = "MCP_PROJECT_ROOT";
    private static readonly TimeSpan ClientRootsTimeout = TimeSpan.FromSeconds(2);

    private readonly string? _netCoreDbgProjectRoot;
    private readonly string? _mcpProjectRoot;
    private readonly string? _explicitProjectPath;
    private readonly bool _projectFromCwd;
    private readonly string _startupCwd;

    private ProjectRootResolver(
        string? netCoreDbgProjectRoot,
        string? mcpProjectRoot,
        string? explicitProjectPath,
        bool projectFromCwd,
        string startupCwd)
    {
        _netCoreDbgProjectRoot = netCoreDbgProjectRoot;
        _mcpProjectRoot = mcpProjectRoot;
        _explicitProjectPath = explicitProjectPath;
        _projectFromCwd = projectFromCwd;
        _startupCwd = startupCwd;
    }

    internal static ProjectRootResolver FromHostArguments(IReadOnlyList<string> arguments, string startupCwd)
    {
        string? explicitProjectPath = null;
        var projectFromCwd = false;

        for (var index = 0; index < arguments.Count; index++)
        {
            var argument = arguments[index];
            if (string.Equals(argument, "--project-from-cwd", StringComparison.Ordinal))
            {
                projectFromCwd = true;
                continue;
            }

            if (string.Equals(argument, "--project", StringComparison.Ordinal) && index + 1 < arguments.Count)
            {
                explicitProjectPath = arguments[++index];
                continue;
            }

            const string projectPrefix = "--project=";
            if (argument.StartsWith(projectPrefix, StringComparison.Ordinal))
            {
                explicitProjectPath = argument[projectPrefix.Length..];
            }
        }

        return new ProjectRootResolver(
            Environment.GetEnvironmentVariable(NetCoreDbgProjectRoot),
            Environment.GetEnvironmentVariable(McpProjectRoot),
            string.IsNullOrEmpty(explicitProjectPath) ? null : explicitProjectPath,
            projectFromCwd,
            Path.GetFullPath(startupCwd));
    }

    internal async ValueTask<string?> ResolveAsync(McpServer server, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();

        // A configured but invalid operator path intentionally fails closed: it must not be
        // displaced by a client-supplied root.
        if (HasOperatorScope())
        {
            return ResolveOperatorScope();
        }

        var clientRoot = await TryResolveClientRootAsync(server, cancellationToken).ConfigureAwait(false);
        if (clientRoot is not null)
        {
            return clientRoot;
        }

        return _projectFromCwd
            ? FindDotNetProjectRoot(_startupCwd)
            : TryGetDirectory(_startupCwd, out var startupRoot) ? startupRoot : null;
    }

    private bool HasOperatorScope() =>
        !string.IsNullOrEmpty(_netCoreDbgProjectRoot)
        || !string.IsNullOrEmpty(_mcpProjectRoot)
        || _explicitProjectPath is not null;

    private string? ResolveOperatorScope()
    {
        foreach (var candidate in new[] { _netCoreDbgProjectRoot, _mcpProjectRoot, _explicitProjectPath })
        {
            if (!string.IsNullOrEmpty(candidate) && TryGetDirectory(candidate, out var root))
            {
                return root;
            }
        }

        return null;
    }

    private static async ValueTask<string?> TryResolveClientRootAsync(McpServer server, CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(ClientRootsTimeout);

        ListRootsResult roots;
        try
        {
            roots = await server.RequestRootsAsync(new ListRootsRequestParams(), deadline.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return null;
        }
        catch
        {
            // A client without roots support (or an invalid roots response) is equivalent to
            // Python's failed ctx.session.list_roots() fallback.
            return null;
        }

        foreach (var root in roots.Roots)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (TryGetLocalFileRoot(root.Uri, out var localRoot))
            {
                return localRoot;
            }
        }

        return null;
    }

    private static bool TryGetLocalFileRoot(string uriText, out string root)
    {
        root = string.Empty;
        if (!Uri.TryCreate(uriText, UriKind.Absolute, out var uri)
            || !string.Equals(uri.Scheme, Uri.UriSchemeFile, StringComparison.OrdinalIgnoreCase)
            || (!string.IsNullOrEmpty(uri.Host) && !IsLocalAuthority(uri.Host)))
        {
            return false;
        }

        var localPath = uri.LocalPath;
        return !IsNetworkPath(localPath) && TryGetDirectory(localPath, out root);
    }

    private static bool IsLocalAuthority(string authority) =>
        string.Equals(authority, "localhost", StringComparison.OrdinalIgnoreCase)
        || string.Equals(authority, "127.0.0.1", StringComparison.Ordinal)
        || string.Equals(authority, "[::1]", StringComparison.Ordinal);

    private static bool IsNetworkPath(string path)
    {
        var normalized = path.Replace('/', '\\');
        return normalized.StartsWith("\\\\", StringComparison.Ordinal)
            || normalized.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase);
    }

    private static bool TryGetDirectory(string path, out string root)
    {
        root = string.Empty;
        try
        {
            if (!Directory.Exists(path))
            {
                return false;
            }

            root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
            return true;
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException or IOException)
        {
            return false;
        }
    }

    private static string FindDotNetProjectRoot(string startDirectory)
    {
        var start = Path.GetFullPath(startDirectory);

        foreach (var directory in Ancestors(start))
        {
            if (ContainsFileMatching(directory, "*.sln"))
            {
                return directory;
            }
        }

        foreach (var directory in Ancestors(start))
        {
            if (ContainsFileMatching(directory, "*.csproj")
                || ContainsFileMatching(directory, "*.vbproj")
                || ContainsFileMatching(directory, "*.fsproj"))
            {
                return directory;
            }
        }

        foreach (var directory in Ancestors(start))
        {
            var gitPath = Path.Combine(directory, ".git");
            if (Directory.Exists(gitPath) || File.Exists(gitPath))
            {
                return directory;
            }
        }

        return start;
    }

    private static IEnumerable<string> Ancestors(string start)
    {
        for (var directory = new DirectoryInfo(start); directory is not null; directory = directory.Parent)
        {
            yield return directory.FullName;
        }
    }

    private static bool ContainsFileMatching(string directory, string pattern)
    {
        try
        {
            return Directory.EnumerateFiles(directory, pattern, SearchOption.TopDirectoryOnly).Any();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            return false;
        }
    }
}

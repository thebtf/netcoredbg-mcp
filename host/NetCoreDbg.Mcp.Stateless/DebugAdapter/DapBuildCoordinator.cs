using System.Buffers;
using System.Text;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed record DapBuildCommandResult(int ExitCode, string Stdout, string Stderr);
internal sealed record DapBuildPlan(string ProjectRoot, string ProjectPath, string Configuration);
internal sealed record DapBuildResult(string Stage, DapBuildCommandResult Command);

internal sealed class DapBuildCleanupException(string message, Exception innerException) : Exception(message, innerException) { }

internal static class DapBuildOutput
{
    private const int MaximumRetainedCharacters = 5_000_000;
    private const int ReadBufferSize = 8192;

    internal static async Task<string> ReadAsync(Stream stream)
    {
        using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true, ReadBufferSize, leaveOpen: true);
        var buffer = ArrayPool<char>.Shared.Rent(ReadBufferSize);
        var chunks = new Queue<string>();
        var retained = 0;
        try
        {
            int count;
            while ((count = await reader.ReadAsync(buffer.AsMemory(0, ReadBufferSize)).ConfigureAwait(false)) != 0)
            {
                chunks.Enqueue(new string(buffer, 0, count));
                retained += count;
                while (chunks.Count > 1 && retained - chunks.Peek().Length >= MaximumRetainedCharacters)
                    retained -= chunks.Dequeue().Length;
            }

            var skipped = Math.Max(0, retained - MaximumRetainedCharacters);
            var result = new StringBuilder(retained - skipped);
            foreach (var chunk in chunks)
            {
                var start = Math.Min(skipped, chunk.Length);
                skipped -= start;
                result.Append(chunk, start, chunk.Length - start);
            }
            return result.ToString();
        }
        finally
        {
            ArrayPool<char>.Shared.Return(buffer);
        }
    }
}

internal static class DapBuildCoordinator
{
    private static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(150);
    private static readonly string[] LockErrors =
    [
        "MSB3021", "MSB3026", "MSB3027", "being used by another process",
        "The process cannot access the file", "because it is being used by another process",
    ];

    internal static bool TryCreatePlan(string projectRoot, string? project, string? configuration, out DapBuildPlan? plan)
    {
        plan = null;
        if (string.IsNullOrWhiteSpace(project) || project.Contains('\0') ||
            configuration is not ("Debug" or "Release")) return false;
        try
        {
            var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(projectRoot));
            if (IsNetworkOrDevicePath(root) || IsNetworkOrDevicePath(project) || !Directory.Exists(root)) return false;
            var path = Path.GetFullPath(project);
            if ((!File.Exists(path) && !Directory.Exists(path)) || HasReparsePoint(path)) return false;
            plan = new DapBuildPlan(root, path, configuration);
            return true;
        }
        catch (Exception exception) when (exception is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException)
        {
            return false;
        }
    }

    internal static bool TryNormalizeProgram(string program, out string? normalized)
    {
        normalized = null;
        try
        {
            var path = Path.GetFullPath(program);
            var extension = Path.GetExtension(path);
            if (!extension.Equals(".dll", StringComparison.OrdinalIgnoreCase) && !extension.Equals(".exe", StringComparison.OrdinalIgnoreCase)) return false;
            normalized = path;
            return true;
        }
        catch (Exception exception) when (exception is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException)
        {
            return false;
        }
    }

    internal static bool IsValidDirectoryPath(string path)
    {
        try { _ = Path.GetFullPath(path); return true; }
        catch (Exception exception) when (exception is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException) { return false; }
    }

    internal static bool TryResolveBuiltProgram(string program, out string? resolved)
    {
        resolved = null;
        try
        {
            var path = program;
            var extension = Path.GetExtension(path);
            if (!File.Exists(path) ||
                (!extension.Equals(".dll", StringComparison.OrdinalIgnoreCase) && !extension.Equals(".exe", StringComparison.OrdinalIgnoreCase)))
                return false;
            if (extension.Equals(".exe", StringComparison.OrdinalIgnoreCase))
            {
                var dll = Path.ChangeExtension(path, ".dll");
                var runtimeConfig = Path.ChangeExtension(path, ".runtimeconfig.json");
                if (File.Exists(dll) && File.Exists(runtimeConfig)) path = dll;
            }
            resolved = path;
            return true;
        }
        catch (Exception exception) when (exception is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException)
        {
            return false;
        }
    }

    internal static async Task<DapBuildResult> BuildAsync(DapBuildPlan plan, CancellationToken cancellationToken)
    {
        var dotnet = DapLaunchEnvironment.FindExecutable("dotnet")
            ?? throw new FileNotFoundException("The dotnet host is unavailable.");
        var restore = await RunAsync(dotnet, ["restore", plan.ProjectPath, "--interactive", "false", "--disable-build-servers", "-p:UseSharedCompilation=false"], plan.ProjectRoot, cancellationToken).ConfigureAwait(false);
        if (restore.ExitCode != 0) return new DapBuildResult("restore", restore);

        DapBuildCommandResult build;
        for (var attempt = 0; ; attempt++)
        {
            build = await RunAsync(dotnet, ["build", plan.ProjectPath, "-c", plan.Configuration, "--disable-build-servers", "-p:UseSharedCompilation=false"], plan.ProjectRoot, cancellationToken).ConfigureAwait(false);
            if (build.ExitCode == 0 || attempt >= 2 || !IsFileLockFailure(build)) break;
            await Task.Delay(TimeSpan.FromSeconds(attempt + 1), cancellationToken).ConfigureAwait(false);
        }
        return new DapBuildResult("build", build);
    }

    private static Task<DapBuildCommandResult> RunAsync(string executable, IReadOnlyList<string> arguments, string root, CancellationToken cancellationToken) =>
        OperatingSystem.IsWindows()
            ? DapBuildWindowsRunner.RunAsync(executable, arguments, root, CommandTimeout, cancellationToken)
            : DapBuildUnixRunner.RunAsync(executable, arguments, root, CommandTimeout, cancellationToken);

    private static bool IsFileLockFailure(DapBuildCommandResult result) =>
        LockErrors.Any(pattern => result.Stdout.Contains(pattern, StringComparison.Ordinal) || result.Stderr.Contains(pattern, StringComparison.Ordinal));

    private static bool IsNetworkOrDevicePath(string path) =>
        path.Replace('/', '\\').StartsWith("\\\\", StringComparison.Ordinal);

    private static bool HasReparsePoint(string path)
    {
        for (var current = path; current is not null; current = Path.GetDirectoryName(current))
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) return true;
        return false;
    }
}

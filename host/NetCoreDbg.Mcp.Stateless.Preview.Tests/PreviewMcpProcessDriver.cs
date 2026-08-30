using System.Text.Json;
using System.Diagnostics;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

internal sealed class PreviewMcpProcessDriver : IAsyncDisposable
{
    internal const string CurrentProtocolVersion = "2026-07-28";
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(5);
    private readonly ITransport _transport;
    private bool _disposed;

    private PreviewMcpProcessDriver(ITransport transport)
    {
        _transport = transport;
    }

    internal static Task<PreviewMcpProcessDriver> StartRawAsync(
        string projectRoot,
        string? workingDirectory = null,
        IReadOnlyDictionary<string, string?>? environment = null,
        CancellationToken cancellationToken = default) =>
        StartAsync(
            PreviewOutputPathResolver.ResolveProcess(),
            projectRoot,
            workingDirectory ?? PreviewRepositoryLayout.Root,
            environment,
            cancellationToken);

    internal static Task<PreviewMcpProcessDriver> StartVerifiedExtractedExecutableAsync(
        string verifiedExecutablePath,
        string projectRoot,
        string? workingDirectory = null,
        IReadOnlyDictionary<string, string?>? environment = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(verifiedExecutablePath);
        if (!Path.IsPathFullyQualified(verifiedExecutablePath))
        {
            throw new ArgumentException("The verified extracted preview executable path must be fully qualified.", nameof(verifiedExecutablePath));
        }

        var executablePath = Path.GetFullPath(verifiedExecutablePath);
        Assert.True(File.Exists(executablePath), $"Verified extracted preview executable is absent: '{executablePath}'.");
        var executableDirectory = Path.GetDirectoryName(executablePath)
            ?? throw new InvalidOperationException($"Verified extracted preview executable has no parent directory: '{executablePath}'.");
        return StartAsync(
            new PreviewOutputProcess(executablePath, []),
            projectRoot,
            workingDirectory ?? executableDirectory,
            environment,
            cancellationToken);
    }

    private static async Task<PreviewMcpProcessDriver> StartAsync(
        PreviewOutputProcess candidate,
        string projectRoot,
        string workingDirectory,
        IReadOnlyDictionary<string, string?>? environment,
        CancellationToken cancellationToken)
    {
        var transport = new StdioClientTransport(new StdioClientTransportOptions
        {
            Command = candidate.Command,
            Arguments = [.. candidate.Arguments, "--project", projectRoot],
            Name = "netcoredbg-mcp-stateless-preview-contract",
            WorkingDirectory = workingDirectory,
            EnvironmentVariables = environment is null ? null : new Dictionary<string, string?>(environment),
            ShutdownTimeout = TimeSpan.FromSeconds(2),
        });
        var connection = await transport.ConnectAsync(cancellationToken).ConfigureAwait(false);
        return new PreviewMcpProcessDriver(connection);
    }

    internal static JsonObject CurrentMeta()
    {
        return new JsonObject
        {
            [MetaKeys.ProtocolVersion] = CurrentProtocolVersion,
            [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "preview-contract-tests", ["version"] = "1.0" },
            [MetaKeys.ClientCapabilities] = new JsonObject(),
        };
    }

    internal Task<JsonRpcMessage> DiscoverAsync(RequestId id, CancellationToken cancellationToken = default) =>
        SendAsync("server/discover", new JsonObject { ["_meta"] = CurrentMeta() }, id, cancellationToken);

    internal Task<JsonRpcMessage> ListToolsAsync(RequestId id, JsonObject? meta = null, CancellationToken cancellationToken = default) =>
        SendAsync("tools/list", new JsonObject { ["_meta"] = (meta ?? CurrentMeta()).DeepClone() }, id, cancellationToken);

    internal Task<JsonRpcMessage> CallToolAsync(
        string name,
        JsonObject? arguments,
        RequestId id,
        JsonObject? meta = null,
        CancellationToken cancellationToken = default) =>
        SendAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = name,
                ["arguments"] = arguments?.DeepClone(),
                ["_meta"] = (meta ?? CurrentMeta()).DeepClone(),
            },
            id,
            cancellationToken);

    internal async Task<JsonRpcMessage> SendAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default)
    {
        await SendRequestAsync(method, parameters, id, cancellationToken).ConfigureAwait(false);
        return await ReadResponseAsync(id, cancellationToken).ConfigureAwait(false);
    }

    internal Task SendRequestAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default) =>
        _transport.SendMessageAsync(
            new JsonRpcRequest
            {
                Id = id,
                Method = method,
                Params = parameters?.DeepClone(),
            },
            cancellationToken);

    internal Task SendCancellationAsync(RequestId id, CancellationToken cancellationToken = default) =>
        _transport.SendMessageAsync(
            new JsonRpcNotification
            {
                Method = NotificationMethods.CancelledNotification,
                Params = JsonSerializer.SerializeToNode(
                    new CancelledNotificationParams { RequestId = id },
                    McpJsonUtilities.DefaultOptions),
            },
            cancellationToken);

    internal async Task<JsonRpcMessage?> TryReadResponseAsync(RequestId id, TimeSpan timeout)
    {
        using var deadline = new CancellationTokenSource(timeout);
        try
        {
            return await ReadResponseAsync(id, deadline.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested)
        {
            return null;
        }
    }

    internal async Task<JsonRpcMessage?> TryReadMessageAsync(TimeSpan timeout)
    {
        using var deadline = new CancellationTokenSource(timeout);
        try
        {
            return await _transport.MessageReader.ReadAsync(deadline.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested)
        {
            return null;
        }
        catch (System.Threading.Channels.ChannelClosedException)
        {
            return null;
        }
        catch (ClientTransportClosedException)
        {
            return null;
        }
    }

    internal async Task<bool> WaitForTransportClosureAsync(TimeSpan timeout)
    {
        using var deadline = new CancellationTokenSource(timeout);
        try
        {
            await _transport.MessageReader.Completion.WaitAsync(deadline.Token).ConfigureAwait(false);
            return true;
        }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested)
        {
            return false;
        }
        catch (ClientTransportClosedException)
        {
            return true;
        }
    }

    private async Task<JsonRpcMessage> ReadResponseAsync(RequestId id, CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(RequestTimeout);
        while (true)
        {
            var incoming = await _transport.MessageReader.ReadAsync(deadline.Token).ConfigureAwait(false);
            if (incoming is JsonRpcMessageWithId correlated && correlated.Id == id)
            {
                return incoming;
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        await _transport.DisposeAsync().ConfigureAwait(false);
    }
}

internal static class PreviewRepositoryLayout
{
    internal static readonly string Root = FindRoot();
    internal static readonly string FixtureRoot = Path.Combine(Root, "tests", "fixtures", "PreviewSearchApp");

    private static string FindRoot()
    {
        for (var current = new DirectoryInfo(AppContext.BaseDirectory); current is not null; current = current.Parent)
        {
            if (File.Exists(Path.Combine(current.FullName, "AGENTS.md")))
            {
                return current.FullName;
            }
        }

        throw new InvalidOperationException("Repository root could not be located from the test assembly base directory.");
    }
}

internal sealed record PreviewOutputProcess(string Command, List<string> Arguments);

internal static class PreviewOutputPathResolver
{
    internal static PreviewOutputProcess ResolveProcess()
    {
        var outputDirectory = new DirectoryInfo(AppContext.BaseDirectory);
        var targetFramework = outputDirectory.Name;
        var configuration = outputDirectory.Parent?.Name
            ?? throw new InvalidOperationException($"Test output configuration is absent from '{AppContext.BaseDirectory}'.");
        var projectDirectory = Path.Combine(PreviewRepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless.Preview");
        var assemblyName = "NetCoreDbg.Mcp.Stateless.Preview";
        var targetPath = Path.Combine(projectDirectory, "bin", configuration, targetFramework, $"{assemblyName}.dll");
        Assert.True(File.Exists(targetPath), $"Built preview target is absent: '{targetPath}'.");
        var appHost = Path.Combine(Path.GetDirectoryName(targetPath)!, OperatingSystem.IsWindows() ? $"{assemblyName}.exe" : assemblyName);
        return File.Exists(appHost)
            ? new PreviewOutputProcess(appHost, [])
            : new PreviewOutputProcess("dotnet", [targetPath]);
    }

    internal static Process StartDirect(params string[] arguments) => StartDirectIn(null, arguments);

    internal static Process StartDirectIn(string? workingDirectory, params string[] arguments)
    {
        var candidate = ResolveProcess();
        var start = new ProcessStartInfo(candidate.Command)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = workingDirectory ?? string.Empty,
        };
        foreach (var argument in candidate.Arguments.Concat(arguments))
        {
            start.ArgumentList.Add(argument);
        }

        return Process.Start(start) ?? throw new InvalidOperationException("Preview process did not start.");
    }
}

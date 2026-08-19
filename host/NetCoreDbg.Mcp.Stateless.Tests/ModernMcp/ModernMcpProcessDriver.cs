using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

/// <summary>
/// Owns one official MCP SDK stdio session and its controlled native-adapter evidence.
/// It deliberately exposes protocol observations, never candidate implementation types.
/// </summary>
internal sealed class ModernMcpProcessDriver : IAsyncDisposable
{
    internal const string CurrentProtocolVersion = "2026-07-28";
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(2);

    private readonly FixtureProcess _fixture;
    private readonly string _scratchDirectory;
    private readonly JsonObject? _initialMeta;
    private readonly List<ModernMcpRequestObservation> _requests = [];
    private readonly List<string> _standardErrorLines = [];
    private readonly object _gate = new();
    private bool _clientClosed;
    private bool _disposed;

    private ModernMcpProcessDriver(FixtureProcess fixture, string scratchDirectory, McpClient client, string inertProgramPath, JsonObject? initialMeta)
    {
        _scratchDirectory = scratchDirectory;
        _fixture = fixture;
        _initialMeta = initialMeta?.DeepClone().AsObject();
        Client = client;
        InertProgramPath = inertProgramPath;
    }

    internal McpClient Client { get; }

    /// <summary>
    /// The SDK exposes this only after stdio completion. It is intentionally absent while live.
    /// </summary>
    internal int? ProcessId { get; private set; }

    internal string InertProgramPath { get; }

    internal IReadOnlyList<ModernMcpRequestObservation> Requests
    {
        get
        {
            lock (_gate)
            {
                return _requests.ToArray();
            }
        }
    }

    internal static async Task<ModernMcpProcessDriver> StartAsync(
        ModernMcpStartOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        options ??= new ModernMcpStartOptions();
        var scratchDirectory = ModernMcpScratchDirectory.Create();
        FixtureProcess? fixture = null;
        StdioClientTransport? transport = null;
        McpClient? client = null;
        try
        {
            fixture = FixtureProcess.Create(options.FixtureConfiguration ?? new FixtureConfiguration());
            var candidate = TestOutputPathResolver.ResolveProcess(Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");

            var inertProgramPath = Path.Combine(scratchDirectory, "controlled-program.dll");
            using var operation = CreateBoundedCancellation(cancellationToken);
            await File.WriteAllBytesAsync(inertProgramPath, [], operation.Token).ConfigureAwait(false);

            ModernMcpProcessDriver? driver = null;
            transport = new StdioClientTransport(new StdioClientTransportOptions
            {
                Command = candidate.Command,
                Arguments = candidate.Arguments,
                Name = "netcoredbg-mcp-stateless-modern-contract",
                WorkingDirectory = RepositoryLayout.Root,
                EnvironmentVariables = CandidateEnvironment(fixture, inertProgramPath, options.AdditionalEnvironment),
                ShutdownTimeout = TimeSpan.FromSeconds(2),
                StandardErrorLines = line => driver?.AddStandardErrorLine(line),
            });

            client = await McpClient.CreateAsync(transport, cancellationToken: operation.Token).ConfigureAwait(false);
            driver = new ModernMcpProcessDriver(
                fixture,
                scratchDirectory,
                client,
                inertProgramPath,
                options.InitialMeta ?? CurrentMeta(formElicitation: !options.DisableFormElicitation));
            client = null;
            transport = null;
            fixture = null;
            return driver;
        }
        catch
        {
            await DisposeFailedStartTransportAsync(client, connection: null).ConfigureAwait(false);
            await CleanupFailedStartAsync(fixture, scratchDirectory).ConfigureAwait(false);
            throw;
        }
    }

    internal static async Task<ModernMcpFirstWireDriver> StartFirstWireAsync(CancellationToken cancellationToken = default)
    {
        var scratchDirectory = ModernMcpScratchDirectory.Create();
        FixtureProcess? fixture = null;
        StdioClientTransport? transport = null;
        ITransport? connection = null;
        try
        {
            fixture = FixtureProcess.Create(new FixtureConfiguration());
            var candidate = TestOutputPathResolver.ResolveProcess(Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");

            var inertProgramPath = Path.Combine(scratchDirectory, "controlled-program.dll");
            using var operation = CreateBoundedCancellation(cancellationToken);
            await File.WriteAllBytesAsync(inertProgramPath, [], operation.Token).ConfigureAwait(false);

            transport = new StdioClientTransport(new StdioClientTransportOptions
            {
                Command = candidate.Command,
                Arguments = candidate.Arguments,
                Name = "netcoredbg-mcp-stateless-first-wire-contract",
                WorkingDirectory = RepositoryLayout.Root,
                EnvironmentVariables = CandidateEnvironment(fixture, inertProgramPath),
                ShutdownTimeout = TimeSpan.FromSeconds(2),
            });
            connection = await transport.ConnectAsync(operation.Token).ConfigureAwait(false);
            var driver = new ModernMcpFirstWireDriver(fixture, scratchDirectory, connection);
            connection = null;
            transport = null;
            fixture = null;
            return driver;
        }
        catch
        {
            await DisposeFailedStartTransportAsync(client: null, connection).ConfigureAwait(false);
            await CleanupFailedStartAsync(fixture, scratchDirectory).ConfigureAwait(false);
            throw;
        }
    }

    internal async Task<CallToolResult> CallToolAsync(
        string name,
        JsonObject arguments,
        JsonObject? meta = null,
        CancellationToken cancellationToken = default)
    {
        var effectiveMeta = meta ?? _initialMeta;
        var requestOptions = effectiveMeta is null
            ? null
            : new RequestOptions { Meta = effectiveMeta.DeepClone().AsObject() };
        var convertedArguments = arguments.ToDictionary(
            static property => property.Key,
            static property => (object?)property.Value?.DeepClone());

        using var operation = CreateBoundedCancellation(cancellationToken);
        return await Client.CallToolAsync(
            name,
            convertedArguments,
            options: requestOptions,
            cancellationToken: operation.Token).ConfigureAwait(false);
    }

    internal static JsonObject CurrentMeta(bool formElicitation = false, JsonObject? extra = null)
    {
        var meta = extra?.DeepClone().AsObject() ?? new JsonObject();
        var capabilities = new JsonObject();
        if (formElicitation)
        {
            capabilities["elicitation"] = new JsonObject { ["form"] = new JsonObject() };
        }

        meta[MetaKeys.ProtocolVersion] = CurrentProtocolVersion;
        if (!meta.ContainsKey(MetaKeys.ClientInfo))
        {
            meta[MetaKeys.ClientInfo] = new JsonObject { ["name"] = "netcoredbg-mcp-stateless-tests", ["version"] = "1.0" };
        }

        meta[MetaKeys.ClientCapabilities] = capabilities;
        return meta;
    }

    internal Task<JsonRpcResponse> DiscoverAsync(JsonObject meta, RequestId id, CancellationToken cancellationToken = default) =>
        SendRawRequestAsync("server/discover", new JsonObject { ["_meta"] = meta.DeepClone() }, id, cancellationToken);

    internal Task<JsonRpcResponse> ListToolsRawAsync(JsonObject meta, RequestId id, CancellationToken cancellationToken = default) =>
        SendRawRequestAsync("tools/list", new JsonObject { ["_meta"] = meta.DeepClone() }, id, cancellationToken);

    internal Task<JsonRpcResponse> CallToolRawAsync(
        string name,
        JsonObject? arguments,
        JsonObject meta,
        RequestId id,
        CancellationToken cancellationToken = default) =>
        SendRawRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = name,
                ["arguments"] = arguments?.DeepClone(),
                ["_meta"] = meta.DeepClone(),
            },
            id,
            cancellationToken);

    internal async Task<JsonRpcResponse> SendRawRequestAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default)
    {
        lock (_gate)
        {
            _requests.Add(new ModernMcpRequestObservation(id, method, parameters?.DeepClone()));
        }

        using var operation = CreateBoundedCancellation(cancellationToken);
        return await Client.SendRequestAsync(
            new JsonRpcRequest
            {
                Id = id,
                Method = method,
                Params = parameters?.DeepClone(),
            },
            operation.Token).ConfigureAwait(false);
    }

    internal static JsonObject RequireResult(JsonRpcResponse response) =>
        Assert.IsType<JsonObject>(response.Result);

    internal async Task<IReadOnlyList<ModernNativeAction>> ReadNativeActionsAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
        return transcript
            .Where(static entry => entry.Kind is "startup" or "request")
            .Select(static entry => new ModernNativeAction(entry.Kind, entry.Command, entry.RawPayload))
            .ToArray();
    }

    internal async Task<int> ReadDescendantProcessIdAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
        return Assert.Single(transcript, static entry => entry.Kind == "descendant").ProcessId
            ?? throw new InvalidOperationException("Controlled adapter did not record its descendant process id.");
    }

    internal async Task<int> PublishSecondWindowedDescendantAsync(CancellationToken cancellationToken = default)
    {
        using var operation = CreateBoundedCancellation(cancellationToken);
        operation.Token.ThrowIfCancellationRequested();
        _fixture.ReleaseSecondWindowedDescendant();
        return await _fixture.WaitForSecondWindowedDescendantPublicationAsync(operation.Token).ConfigureAwait(false);
    }

    internal Task TerminateControlledAdapterAsync(CancellationToken cancellationToken = default) =>
        _fixture.TerminateAdapterAsync(cancellationToken);


    /// <summary>
    /// Disposes the official client and returns its public stdio completion receipt.
    /// No SDK-internal process reflection or bespoke process launch is used.
    /// </summary>
    internal async Task<StdioClientCompletionDetails> CloseClientAsync(CancellationToken cancellationToken = default)
    {
        if (!_clientClosed)
        {
            _clientClosed = true;
            await Client.DisposeAsync().ConfigureAwait(false);
        }

        var completion = await Client.Completion.WaitAsync(cancellationToken).ConfigureAwait(false);
        var stdioCompletion = Assert.IsType<StdioClientCompletionDetails>(completion);
        ProcessId = stdioCompletion.ProcessId;
        return stdioCompletion;
    }

    internal async Task<ModernMcpTransportClosure> WaitForTransportClosureAsync(TimeSpan timeout)
    {
        using var cancellation = new CancellationTokenSource(timeout);
        try
        {
            var completion = await Client.Completion.WaitAsync(cancellation.Token).ConfigureAwait(false);
            if (completion is StdioClientCompletionDetails stdio)
            {
                ProcessId = stdio.ProcessId;
                return new ModernMcpTransportClosure(true, stdio.ProcessId, stdio.ExitCode, StandardErrorLines());
            }

            return new ModernMcpTransportClosure(true, null, null, StandardErrorLines());
        }
        catch (OperationCanceledException)
        {
            return new ModernMcpTransportClosure(false, ProcessId, null, StandardErrorLines());
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        Exception? cleanupFailure = null;
        try
        {
            if (!_clientClosed)
            {
                using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                _ = await CloseClientAsync(cancellation.Token).ConfigureAwait(false);
            }
        }
        catch (Exception exception)
        {
            cleanupFailure = exception;
        }

        try
        {
            await _fixture.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception exception) when (cleanupFailure is null)
        {
            cleanupFailure = exception;
        }

        try
        {
            await ModernMcpScratchDirectory.DeleteAsync(_scratchDirectory).ConfigureAwait(false);
        }
        catch (Exception exception) when (cleanupFailure is null)
        {
            cleanupFailure = exception;
        }

        if (cleanupFailure is not null)
        {
            throw cleanupFailure;
        }
    }

    private void AddStandardErrorLine(string line)
    {
        lock (_gate)
        {
            _standardErrorLines.Add(line);
        }
    }

    private IReadOnlyList<string> StandardErrorLines()
    {
        lock (_gate)
        {
            return _standardErrorLines.ToArray();
        }
    }


    private static Dictionary<string, string?> CandidateEnvironment(
        FixtureProcess fixture,
        string inertProgramPath,
        IReadOnlyDictionary<string, string?>? additionalEnvironment = null)
    {
        var environment = new Dictionary<string, string?>
        {
            ["NETCOREDBG_PATH"] = fixture.ExecutablePath,
            ["NETCOREDBG_PROGRAM_PATH"] = inertProgramPath,
        };
        if (additionalEnvironment is not null)
        {
            foreach (var (name, value) in additionalEnvironment)
            {
                environment[name] = value;
            }
        }

        return environment;
    }

    private static async Task CleanupFailedStartAsync(FixtureProcess? fixture, string scratchDirectory)
    {
        try
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratchDirectory).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Preserve the primary startup failure after bounded scratch cleanup was attempted.
        }

        if (fixture is null)
        {
            return;
        }

        try
        {
            await fixture.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Preserve the primary startup failure after fixture cleanup was attempted.
        }
    }

    private static CancellationTokenSource CreateBoundedCancellation(CancellationToken cancellationToken)
    {
        var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        operation.CancelAfter(RequestTimeout);
        return operation;
    }

    private static async Task DisposeFailedStartTransportAsync(McpClient? client, ITransport? connection)
    {
        try
        {
            if (client is not null)
            {
                await client.DisposeAsync().ConfigureAwait(false);
            }
            else if (connection is not null)
            {
                await connection.DisposeAsync().ConfigureAwait(false);
            }
        }
        catch (Exception)
        {
            // Preserve the primary startup failure after the returned stdio owner was disposed.
        }
    }
}

internal static class ModernMcpRegistryContractDriver
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string RegistryTypeName = "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry";
    private const string SessionTypeName = "NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession";

    internal static async Task<RegistryCleanupFailureObservation> ObserveUnusableSessionCleanupFailureAsync()
    {
        var productionAssembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(
            TestOutputPathResolver.ResolveManagedAssembly(
                RepositoryLayout.Root,
                Path.Combine("host", ProductionAssemblyName),
                ProductionAssemblyName));
        var registryType = productionAssembly.GetType(RegistryTypeName, throwOnError: false);
        var sessionType = productionAssembly.GetType(SessionTypeName, throwOnError: false);
        Assert.NotNull(registryType);
        Assert.NotNull(sessionType);

        var isUsableType = typeof(Func<,>).MakeGenericType(sessionType!, typeof(bool));
        var disposeType = typeof(Func<,>).MakeGenericType(sessionType, typeof(ValueTask));
        var constructor = registryType!.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string), isUsableType, disposeType],
            modifiers: null);
        Assert.NotNull(constructor);

        var cleanupAttempts = 0;
        Func<object, ValueTask> failingCleanup = _ =>
        {
            cleanupAttempts++;
            return new ValueTask(Task.FromException(new InvalidOperationException("Injected cleanup failure.")));
        };
        var registry = constructor!.Invoke([
            null,
            CreateIsUsableDelegate(sessionType, static _ => false),
            CreateDisposerDelegate(sessionType, failingCleanup),
        ]);
        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        var sessions = sessionsField!.GetValue(registry);
        Assert.NotNull(sessions);

        const string sessionId = "unusable-session";
        var session = RuntimeHelpers.GetUninitializedObject(sessionType);
        var tryAdd = sessions!.GetType().GetMethod("TryAdd", [typeof(string), sessionType]);
        var containsKey = sessions.GetType().GetMethod("ContainsKey", [typeof(string)]);
        Assert.NotNull(tryAdd);
        Assert.NotNull(containsKey);
        Assert.True((bool)tryAdd!.Invoke(sessions, [sessionId, session])!);

        var getStateAsync = registryType.GetMethod(
            "GetStateAsync",
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(CallToolRequestParams), typeof(CancellationToken)],
            modifiers: null);
        Assert.NotNull(getStateAsync);

        var unusable = await GetStateAsync(getStateAsync!, registry, sessionId);
        var missing = await GetStateAsync(getStateAsync, registry, "missing-session");
        return new RegistryCleanupFailureObservation(
            cleanupAttempts,
            (bool)containsKey!.Invoke(sessions, [sessionId])!,
            unusable.ResultType,
            unusable.IsError == true,
            Assert.IsType<JsonElement>(unusable.StructuredContent),
            Assert.IsType<JsonElement>(missing.StructuredContent));
    }

    private static Delegate CreateIsUsableDelegate(Type sessionType, Func<object, bool> isUsable) =>
        (Delegate)typeof(ModernMcpRegistryContractDriver)
            .GetMethod(nameof(CreateIsUsableDelegateCore), BindingFlags.Static | BindingFlags.NonPublic)!
            .MakeGenericMethod(sessionType)
            .Invoke(null, [isUsable])!;

    private static Delegate CreateDisposerDelegate(Type sessionType, Func<object, ValueTask> disposer) =>
        (Delegate)typeof(ModernMcpRegistryContractDriver)
            .GetMethod(nameof(CreateDisposerDelegateCore), BindingFlags.Static | BindingFlags.NonPublic)!
            .MakeGenericMethod(sessionType)
            .Invoke(null, [disposer])!;

    private static Func<TSession, bool> CreateIsUsableDelegateCore<TSession>(Func<object, bool> isUsable) =>
        session => isUsable(session!);

    private static Func<TSession, ValueTask> CreateDisposerDelegateCore<TSession>(Func<object, ValueTask> disposer) =>
        session => disposer(session!);

    private static async Task<CallToolResult> GetStateAsync(MethodInfo method, object registry, string sessionId)
    {
        var request = new CallToolRequestParams
        {
            Name = "get_debug_state",
            Arguments = new Dictionary<string, JsonElement>
            {
                ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
            },
        };
        var pending = method.Invoke(registry, [request, CancellationToken.None]);
        Assert.NotNull(pending);
        var asTask = pending!.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
        Assert.NotNull(asTask);
        var task = Assert.IsAssignableFrom<Task>(asTask!.Invoke(pending, []));
        await task.ConfigureAwait(false);
        return Assert.IsType<CallToolResult>(task.GetType().GetProperty("Result")!.GetValue(task));
    }
}

internal sealed record RegistryCleanupFailureObservation(
    int CleanupAttempts,
    bool TokenRetained,
    string? ResultType,
    bool IsError,
    JsonElement UnusableContent,
    JsonElement MissingContent);

internal sealed record ModernMcpStartOptions(
    JsonObject? InitialMeta = null,
    bool DisableFormElicitation = false,
    string? PriorProcessToken = null,
    FixtureConfiguration? FixtureConfiguration = null,
    IReadOnlyDictionary<string, string?>? AdditionalEnvironment = null);

/// <summary>Kind is the controlled transcript kind: <c>startup</c> or <c>request</c>.</summary>
internal sealed record ModernNativeAction(string Kind, string? Command, string? Detail);

internal sealed record ModernMcpRequestObservation(RequestId Id, string Method, JsonNode? Parameters);


internal sealed record ModernMcpTransportClosure(
    bool Observed,
    int? ProcessId,
    int? ExitCode,
    IReadOnlyList<string> StandardErrorLines);

internal static class ModernMcpScratchDirectory
{
    private static readonly TimeSpan CleanupTimeout = TimeSpan.FromSeconds(1);

    internal static string Create()
    {
        var directory = Path.Combine(RepositoryLayout.ScratchRoot, $"modern-mcp-{Guid.NewGuid():N}");
        Directory.CreateDirectory(directory);
        return directory;
    }


    internal static async Task DeleteAsync(string directory)
    {
        var startedAt = Stopwatch.GetTimestamp();
        Exception? lastFailure = null;
        while (Directory.Exists(directory))
        {
            try
            {
                Directory.Delete(directory, recursive: true);
                return;
            }
            catch (IOException exception)
            {
                lastFailure = exception;
            }
            catch (UnauthorizedAccessException exception)
            {
                lastFailure = exception;
            }

            if (Stopwatch.GetElapsedTime(startedAt) >= CleanupTimeout)
            {
                throw new InvalidOperationException($"Owned modern MCP scratch directory '{directory}' could not be removed within {CleanupTimeout}.", lastFailure);
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25)).ConfigureAwait(false);
        }
    }
}

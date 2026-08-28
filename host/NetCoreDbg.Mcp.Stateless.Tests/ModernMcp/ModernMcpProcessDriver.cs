using System.Diagnostics;
using System.Globalization;
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
    private static readonly TimeSpan MaximumCoverageStartupTimeout = TimeSpan.FromSeconds(10);
    internal const string CoverageStartupTimeoutEnvironmentVariable =
        "NETCOREDBG_MCP_COVERAGE_STARTUP_TIMEOUT_MS";

    private readonly FixtureProcess _fixture;
    private readonly string _scratchDirectory;
    private readonly JsonObject? _initialMeta;
    private readonly List<ModernMcpRequestObservation> _requests = [];
    private readonly ModernMcpStandardErrorTail _standardErrorTail;
    private readonly object _gate = new();
    private bool _clientClosed;
    private bool _disposed;

    private ModernMcpProcessDriver(
        FixtureProcess fixture,
        string scratchDirectory,
        McpClient client,
        string inertProgramPath,
        JsonObject? initialMeta,
        ModernMcpStandardErrorTail standardErrorTail)
    {
        _scratchDirectory = scratchDirectory;
        _fixture = fixture;
        _initialMeta = initialMeta?.DeepClone().AsObject();
        _standardErrorTail = standardErrorTail;
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
        var startupTimeout = ResolveCoverageStartupTimeout(
            Environment.GetEnvironmentVariable(CoverageStartupTimeoutEnvironmentVariable));
        using var operation = CreateBoundedCancellation(cancellationToken, startupTimeout);
        var startupStartedAt = Stopwatch.GetTimestamp();
        var scratchDirectory = ModernMcpScratchDirectory.Create();
        var standardErrorTail = new ModernMcpStandardErrorTail();
        FixtureProcess? fixture = null;
        StdioClientTransport? transport = null;
        McpClient? client = null;
        try
        {
            fixture = FixtureProcess.Create(options.FixtureConfiguration ?? new FixtureConfiguration());
            var candidate = options.CandidateProcess
                ?? TestOutputPathResolver.ResolveProcess(
                    Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"),
                    "NetCoreDbg.Mcp.Stateless");

            var inertProgramPath = Path.Combine(scratchDirectory, "controlled-program.dll");
            var clientOptions = CreateClientOptions(startupTimeout);
            await File.WriteAllBytesAsync(inertProgramPath, [], operation.Token).ConfigureAwait(false);

            transport = new StdioClientTransport(new StdioClientTransportOptions
            {
                Command = candidate.Command,
                Arguments = candidate.Arguments,
                Name = "netcoredbg-mcp-stateless-modern-contract",
                WorkingDirectory = RepositoryLayout.Root,
                EnvironmentVariables = CandidateEnvironment(fixture, inertProgramPath, options.AdditionalEnvironment),
                ShutdownTimeout = TimeSpan.FromSeconds(2),
                StandardErrorLines = standardErrorTail.Add,
            });

            try
            {
                client = await McpClient.CreateAsync(
                    transport,
                    clientOptions,
                    cancellationToken: operation.Token).ConfigureAwait(false);
            }
            catch (TimeoutException exception) when (!cancellationToken.IsCancellationRequested)
            {
                throw CreatePhaseTimeoutException(
                    ModernMcpTimeoutPhase.SdkStartup,
                    startupTimeout,
                    startupStartedAt,
                    toolName: null,
                    method: null,
                    requestId: null,
                    exception);
            }
            var driver = new ModernMcpProcessDriver(
                fixture,
                scratchDirectory,
                client,
                inertProgramPath,
                options.InitialMeta ?? CurrentMeta(formElicitation: !options.DisableFormElicitation),
                standardErrorTail);
            client = null;
            transport = null;
            fixture = null;
            return driver;
        }
        catch (ClientTransportClosedException exception)
        {
            var failure = ModernMcpProcessStartFailure.FromTransportClosed(
                exception,
                standardErrorTail.Snapshot());
            await DisposeFailedStartTransportAsync(client, connection: null).ConfigureAwait(false);
            await CleanupFailedStartAsync(fixture, scratchDirectory).ConfigureAwait(false);
            throw new ModernMcpProcessStartException(failure, exception);
        }
        catch (ModernMcpPhaseTimeoutException)
        {
            await DisposeFailedStartTransportAsync(client, connection: null).ConfigureAwait(false);
            await CleanupFailedStartAsync(fixture, scratchDirectory).ConfigureAwait(false);
            throw;
        }
        catch (OperationCanceledException exception)
            when (operation.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
        {
            await DisposeFailedStartTransportAsync(client, connection: null).ConfigureAwait(false);
            await CleanupFailedStartAsync(fixture, scratchDirectory).ConfigureAwait(false);
            throw CreatePhaseTimeoutException(
                ModernMcpTimeoutPhase.SdkStartup,
                startupTimeout,
                startupStartedAt,
                toolName: null,
                method: null,
                requestId: null,
                exception);
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

        var deadline = RequestTimeout;
        using var operation = CreateBoundedCancellation(cancellationToken, deadline);
        var startedAt = Stopwatch.GetTimestamp();
        try
        {
            return await Client.CallToolAsync(
                name,
                convertedArguments,
                options: requestOptions,
                cancellationToken: operation.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException exception)
            when (operation.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
        {
            throw CreatePhaseTimeoutException(
                ModernMcpTimeoutPhase.PostStartMcpRequest,
                deadline,
                startedAt,
                toolName: name,
                method: null,
                requestId: null,
                exception);
        }
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
        CancellationToken cancellationToken = default,
        TimeSpan? timeout = null) =>
        SendRawRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = name,
                ["arguments"] = arguments?.DeepClone(),
                ["_meta"] = meta.DeepClone(),
            },
            id,
            cancellationToken,
            timeout);

    internal async Task<JsonRpcResponse> SendRawRequestAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default,
        TimeSpan? timeout = null)
    {
        lock (_gate)
        {
            _requests.Add(new ModernMcpRequestObservation(id, method, parameters?.DeepClone()));
        }

        var deadline = timeout ?? RequestTimeout;
        using var operation = CreateBoundedCancellation(cancellationToken, deadline);
        var startedAt = Stopwatch.GetTimestamp();
        try
        {
            return await Client.SendRequestAsync(
                new JsonRpcRequest
                {
                    Id = id,
                    Method = method,
                    Params = parameters?.DeepClone(),
                },
                operation.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException exception)
            when (operation.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
        {
            throw CreatePhaseTimeoutException(
                ModernMcpTimeoutPhase.PostStartMcpRequest,
                deadline,
                startedAt,
                toolName: null,
                method,
                id.ToString(),
                exception);
        }
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

    internal async Task WaitForThreadsRequestAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            await _fixture.WaitForThreadsRequestAsync(cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw new Xunit.Sdk.XunitException("get_threads did not emit a DAP 'threads' request before the test deadline.");
        }
    }

    internal async Task WaitForStackTraceRequestAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            await _fixture.WaitForStackTraceRequestAsync(cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw new Xunit.Sdk.XunitException("get_call_stack did not emit a DAP 'stackTrace' request before the test deadline.");
        }
    }

    internal Task WaitForFixtureEventAsync(string eventName, CancellationToken cancellationToken = default) =>
        _fixture.WaitForEventAsync(eventName, cancellationToken);

    internal Task WaitForFixtureRecordAsync(string kind, CancellationToken cancellationToken = default) =>
        _fixture.WaitForTranscriptKindAsync(kind, cancellationToken);

    internal void ReleaseThreadsResponse() => _fixture.ReleaseThreadsResponse();
    internal void ReleaseStackTraceResponse() => _fixture.ReleaseStackTraceResponse();

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

    private IReadOnlyList<string> StandardErrorLines() => _standardErrorTail.Snapshot().Lines;


    internal static TimeSpan ResolveCoverageStartupTimeout(string? rawMilliseconds)
    {
        if (rawMilliseconds is null)
        {
            return RequestTimeout;
        }

        if (!int.TryParse(
                rawMilliseconds,
                NumberStyles.None,
                CultureInfo.InvariantCulture,
                out var milliseconds)
            || milliseconds < (int)RequestTimeout.TotalMilliseconds
            || milliseconds > (int)MaximumCoverageStartupTimeout.TotalMilliseconds)
        {
            throw new InvalidOperationException(
                $"{CoverageStartupTimeoutEnvironmentVariable} must be an integer from {(int)RequestTimeout.TotalMilliseconds} through {(int)MaximumCoverageStartupTimeout.TotalMilliseconds}.");
        }

        return TimeSpan.FromMilliseconds(milliseconds);
    }


    internal static McpClientOptions CreateClientOptions(TimeSpan startupTimeout) => new()
    {
        ProtocolVersion = CurrentProtocolVersion,
        InitializationTimeout = startupTimeout,
        DiscoverProbeTimeout = startupTimeout,
    };
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

    private static ModernMcpPhaseTimeoutException CreatePhaseTimeoutException(
        ModernMcpTimeoutPhase phase,
        TimeSpan deadline,
        long startedAt,
        string? toolName,
        string? method,
        string? requestId,
        Exception exception) =>
        new(
            new ModernMcpPhaseTimeoutFailure(
                phase,
                "test_driver",
                deadline,
                Stopwatch.GetElapsedTime(startedAt),
                toolName,
                method,
                requestId),
            exception);

    private static CancellationTokenSource CreateBoundedCancellation(CancellationToken cancellationToken, TimeSpan? timeout = null)
    {
        var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        operation.CancelAfter(timeout ?? RequestTimeout);
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

    internal static async Task<RegistryThreadsAdmissionObservation>
        ObserveUnavailableThreadsAdmissionAsync(bool closedSlot)
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
        var registryConstructor = registryType!.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string), isUsableType, disposeType],
            modifiers: null);
        Assert.NotNull(registryConstructor);
        var registry = registryConstructor!.Invoke([
            null,
            CreateIsUsableDelegate(sessionType, _ => closedSlot),
            CreateDisposerDelegate(sessionType, static _ => ValueTask.CompletedTask),
        ]);

        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        var slotsField = registryType.GetField("_slots", BindingFlags.Instance | BindingFlags.NonPublic);
        var slotType = registryType.GetNestedType("SessionSlot", BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        Assert.NotNull(slotsField);
        Assert.NotNull(slotType);
        var sessions = sessionsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._sessions returned null.");
        var slots = slotsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._slots returned null.");
        var session = RuntimeHelpers.GetUninitializedObject(sessionType);
        const string sessionId = "unavailable-threads-session";
        var addSession = sessions.GetType().GetMethod("TryAdd", [typeof(string), sessionType]);
        var addSlot = slots.GetType().GetMethod("TryAdd", [typeof(string), slotType]);
        var removeSession = sessions.GetType().GetMethod("TryRemove", [typeof(string), sessionType.MakeByRefType()]);
        var removeSlot = slots.GetType().GetMethod("TryRemove", [typeof(string), slotType.MakeByRefType()]);
        var containsSession = sessions.GetType().GetMethod("ContainsKey", [typeof(string)]);
        var containsSlot = slots.GetType().GetMethod("ContainsKey", [typeof(string)]);
        Assert.NotNull(addSession);
        Assert.NotNull(addSlot);
        Assert.NotNull(removeSession);
        Assert.NotNull(removeSlot);
        Assert.NotNull(containsSession);
        Assert.NotNull(containsSlot);

        var stopCalls = 0;
        var disposeCalls = 0;
        Func<CancellationToken, Task> stop = _ =>
        {
            stopCalls++;
            return Task.CompletedTask;
        };
        Func<ValueTask> dispose = () =>
        {
            disposeCalls++;
            return ValueTask.CompletedTask;
        };
        Action remove = closedSlot
            ? static () => { }
            : () =>
            {
                _ = removeSlot!.Invoke(slots, [sessionId, null]);
                _ = removeSession!.Invoke(sessions, [sessionId, null]);
            };
        var slotConstructor = slotType!.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(TimeSpan), typeof(Func<CancellationToken, Task>), typeof(Func<ValueTask>), typeof(Action)],
            modifiers: null);
        Assert.NotNull(slotConstructor);
        var slot = slotConstructor!.Invoke([TimeSpan.FromSeconds(1), stop, dispose, remove]);
        Assert.True((bool)addSession!.Invoke(sessions, [sessionId, session])!);
        Assert.True((bool)addSlot!.Invoke(slots, [sessionId, slot])!);

        if (closedSlot)
        {
            var close = slotType.GetMethod(
                "CloseAndDrainAsync",
                BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null,
                types: [typeof(CancellationToken)],
                modifiers: null);
            Assert.NotNull(close);
            await Assert.IsAssignableFrom<Task>(close!.Invoke(slot, [CancellationToken.None]));
        }

        var getThreadsAsync = registryType.GetMethod(
            "GetThreadsAsync",
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(CallToolRequestParams), typeof(CancellationToken)],
            modifiers: null);
        Assert.NotNull(getThreadsAsync);
        var pending = getThreadsAsync!.Invoke(registry,
        [
            new CallToolRequestParams
            {
                Name = "get_threads",
                Arguments = new Dictionary<string, JsonElement>
                {
                    ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
                },
            },
            CancellationToken.None,
        ]);
        var asTask = pending?.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
        Assert.NotNull(asTask);
        var task = Assert.IsAssignableFrom<Task>(asTask!.Invoke(pending, []));
        await task.ConfigureAwait(false);
        var result = Assert.IsType<CallToolResult>(task.GetType().GetProperty("Result")!.GetValue(task));
        var content = Assert.IsType<JsonElement>(result.StructuredContent);
        return new RegistryThreadsAdmissionObservation(
            result.ResultType,
            result.IsError == true,
            content.GetProperty("kind").GetString(),
            content.GetProperty("error").GetString(),
            (bool)containsSession!.Invoke(sessions, [sessionId])!,
            (bool)containsSlot!.Invoke(slots, [sessionId])!,
            stopCalls,
            disposeCalls);
    }

    internal static async Task<RegistryMissingSlotStopObservation> ObserveMissingSlotStopAsync()
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
        var registryConstructor = registryType!.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string)],
            modifiers: null);
        Assert.NotNull(registryConstructor);
        var registry = registryConstructor!.Invoke([null]);
        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        var sessions = sessionsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._sessions returned null.");
        var session = RuntimeHelpers.GetUninitializedObject(sessionType);
        const string sessionId = "missing-slot-stop-session";
        var addSession = sessions.GetType().GetMethod("TryAdd", [typeof(string), sessionType]);
        var containsSession = sessions.GetType().GetMethod("ContainsKey", [typeof(string)]);
        Assert.NotNull(addSession);
        Assert.NotNull(containsSession);
        Assert.True((bool)addSession!.Invoke(sessions, [sessionId, session])!);

        var stopAsync = registryType.GetMethod(
            "StopAsync",
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(CallToolRequestParams), typeof(CancellationToken)],
            modifiers: null);
        Assert.NotNull(stopAsync);
        var pending = stopAsync!.Invoke(registry,
        [
            new CallToolRequestParams
            {
                Name = "stop_debug",
                Arguments = new Dictionary<string, JsonElement>
                {
                    ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
                },
            },
            CancellationToken.None,
        ]);
        var asTask = pending?.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
        Assert.NotNull(asTask);
        var task = Assert.IsAssignableFrom<Task>(asTask!.Invoke(pending, []));
        await task.ConfigureAwait(false);
        var result = Assert.IsType<CallToolResult>(task.GetType().GetProperty("Result")!.GetValue(task));
        var content = Assert.IsType<JsonElement>(result.StructuredContent);
        return new RegistryMissingSlotStopObservation(
            result.ResultType,
            result.IsError == true,
            content.GetProperty("kind").GetString(),
            content.GetProperty("error").GetString(),
            (bool)containsSession!.Invoke(sessions, [sessionId])!);
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

internal sealed record RegistryThreadsAdmissionObservation(
    string? ResultType,
    bool IsError,
    string? Kind,
    string? Error,
    bool SessionRetained,
    bool SlotRetained,
    int StopCalls,
    int DisposeCalls);

internal sealed record RegistryMissingSlotStopObservation(
    string? ResultType,
    bool IsError,
    string? Kind,
    string? Error,
    bool SessionRetained);

internal sealed record ModernMcpStartOptions(
    JsonObject? InitialMeta = null,
    bool DisableFormElicitation = false,
    string? PriorProcessToken = null,
    FixtureConfiguration? FixtureConfiguration = null,
    IReadOnlyDictionary<string, string?>? AdditionalEnvironment = null,
    TestOutputProcess? CandidateProcess = null);

/// <summary>Kind is the controlled transcript kind: <c>startup</c> or <c>request</c>.</summary>
internal sealed record ModernNativeAction(string Kind, string? Command, string? Detail);

internal sealed record ModernMcpRequestObservation(RequestId Id, string Method, JsonNode? Parameters);


internal sealed record ModernMcpTransportClosure(
    bool Observed,
    int? ProcessId,
    int? ExitCode,
    IReadOnlyList<string> StandardErrorLines);

internal enum ModernMcpTimeoutPhase
{
    SdkStartup,
    PostStartMcpRequest,
}

internal sealed class ModernMcpPhaseTimeoutException : TimeoutException
{
    internal ModernMcpPhaseTimeoutException(
        ModernMcpPhaseTimeoutFailure failure,
        Exception innerException)
        : base("Modern MCP test-driver phase deadline elapsed.", innerException)
    {
        Failure = failure;
    }

    internal ModernMcpPhaseTimeoutFailure Failure { get; }
}

internal sealed class ModernMcpPhaseTimeoutFailure
{
    internal ModernMcpPhaseTimeoutFailure(
        ModernMcpTimeoutPhase phase,
        string owner,
        TimeSpan deadline,
        TimeSpan elapsed,
        string? toolName,
        string? method,
        string? requestId)
    {
        if (deadline <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(deadline));
        }

        if (elapsed < TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(elapsed));
        }

        Phase = phase;
        Owner = owner;
        Deadline = deadline;
        Elapsed = elapsed;
        ToolName = toolName;
        Method = method;
        RequestId = requestId;
    }

    internal ModernMcpTimeoutPhase Phase { get; }
    internal string Owner { get; }
    internal TimeSpan Deadline { get; }
    internal TimeSpan Elapsed { get; }
    internal string? ToolName { get; }
    internal string? Method { get; }
    internal string? RequestId { get; }
    internal bool CompletionObserved => false;
    internal int? ProcessId => null;
    internal int? ExitCode => null;
    internal IReadOnlyList<string> StandardErrorTail => Array.Empty<string>();
}

internal enum ModernMcpProcessStartFailureCategory
{
    TransportClosed,
}

internal sealed class ModernMcpProcessStartException : Exception
{
    internal ModernMcpProcessStartException(
        ModernMcpProcessStartFailure failure,
        ClientTransportClosedException innerException)
        : base("Modern MCP client startup closed before initialization.", innerException)
    {
        Failure = failure;
    }

    internal ModernMcpProcessStartFailure Failure { get; }
}

internal sealed class ModernMcpProcessStartFailure
{
    private ModernMcpProcessStartFailure(
        ModernMcpProcessStartFailureCategory category,
        bool completionObserved,
        int? processId,
        int? exitCode,
        IReadOnlyList<string> standardErrorTail,
        bool standardErrorTruncated)
    {
        Category = category;
        CompletionObserved = completionObserved;
        ProcessId = processId;
        ExitCode = exitCode;
        StandardErrorTail = Array.AsReadOnly(standardErrorTail.ToArray());
        StandardErrorTruncated = standardErrorTruncated;
    }

    internal ModernMcpProcessStartFailureCategory Category { get; }
    internal bool CompletionObserved { get; }
    internal int? ProcessId { get; }
    internal int? ExitCode { get; }
    internal IReadOnlyList<string> StandardErrorTail { get; }
    internal bool StandardErrorTruncated { get; }

    internal static ModernMcpProcessStartFailure FromTransportClosed(
        ClientTransportClosedException exception,
        ModernMcpStandardErrorTailSnapshot fallback)
    {
        if (exception.Details is not StdioClientCompletionDetails completion)
        {
            return new ModernMcpProcessStartFailure(
                ModernMcpProcessStartFailureCategory.TransportClosed,
                completionObserved: false,
                processId: null,
                exitCode: null,
                fallback.Lines,
                fallback.Truncated);
        }

        var sdkTail = ModernMcpStandardErrorTail.Capture(completion.StandardErrorTail);
        return new ModernMcpProcessStartFailure(
            ModernMcpProcessStartFailureCategory.TransportClosed,
            completionObserved: true,
            completion.ProcessId,
            completion.ExitCode,
            sdkTail.Lines.Count > 0 ? sdkTail.Lines : fallback.Lines,
            sdkTail.Truncated || fallback.Truncated);
    }
}

internal sealed class ModernMcpStandardErrorTail
{
    private const int MaximumLineCount = 10;
    private const int MaximumLineCharacters = 512;

    private readonly Queue<string> _lines = new();
    private readonly object _gate = new();
    private bool _truncated;

    internal void Add(string? line)
    {
        if (line is null)
        {
            return;
        }

        lock (_gate)
        {
            if (line.Length > MaximumLineCharacters)
            {
                line = line[..MaximumLineCharacters];
                _truncated = true;
            }

            if (_lines.Count == MaximumLineCount)
            {
                _lines.Dequeue();
                _truncated = true;
            }

            _lines.Enqueue(line);
        }
    }

    internal ModernMcpStandardErrorTailSnapshot Snapshot()
    {
        lock (_gate)
        {
            return new ModernMcpStandardErrorTailSnapshot(
                Array.AsReadOnly(_lines.ToArray()),
                _truncated);
        }
    }

    internal static ModernMcpStandardErrorTailSnapshot Capture(
        IEnumerable<string>? lines)
    {
        var tail = new ModernMcpStandardErrorTail();
        if (lines is not null)
        {
            foreach (var line in lines)
            {
                tail.Add(line);
            }
        }

        return tail.Snapshot();
    }
}

internal sealed record ModernMcpStandardErrorTailSnapshot(
    IReadOnlyList<string> Lines,
    bool Truncated);
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

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
    private static readonly TimeSpan StartupTimeout = TimeSpan.FromSeconds(10);

    private readonly FixtureProcess _fixture;
    private readonly string _scratchDirectory;
    private readonly JsonObject? _initialMeta;
    private readonly string? _debuggerPath;
    private readonly bool _legacyCompat;
    private readonly bool _knownNoDescendantFixture;
    private readonly List<ModernMcpRequestObservation> _requests = [];
    private readonly List<string> _standardErrorLines = [];
    private readonly object _gate = new();
    private long _lifecycleOrder;
    private bool _clientClosed;
    private bool _disposed;
    private Task? _disposeTask;

    private ModernMcpProcessDriver(FixtureProcess fixture, string scratchDirectory, McpClient client, string inertProgramPath, JsonObject? initialMeta, string? debuggerPath, bool legacyCompat, bool knownNoDescendantFixture)
    {
        _scratchDirectory = scratchDirectory;
        _fixture = fixture;
        _initialMeta = initialMeta?.DeepClone().AsObject();
        _debuggerPath = debuggerPath;
        _legacyCompat = legacyCompat;
        _knownNoDescendantFixture = knownNoDescendantFixture;
        Client = client;
        InertProgramPath = inertProgramPath;
    }

    internal McpClient Client { get; }

    /// <summary>
    /// The SDK exposes this only after stdio completion. It is intentionally absent while live.
    /// </summary>
    internal int? ProcessId { get; private set; }

    internal string InertProgramPath { get; }

    internal Process StartOwnedTarget() => _fixture.StartOwnedTarget();

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
        CancellationToken cancellationToken = default,
        TimeSpan? startupTimeout = null)
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
            using var operation = CreateBoundedCancellation(cancellationToken, startupTimeout ?? StartupTimeout);
            await File.WriteAllBytesAsync(inertProgramPath, [], operation.Token).ConfigureAwait(false);

            var environment = CandidateEnvironment(fixture, inertProgramPath, options.AdditionalEnvironment, options.InheritEnvironmentVariables);
            var debuggerPath = environment.Keys.Count(static key => string.Equals(key, "NETCOREDBG_PATH", StringComparison.OrdinalIgnoreCase)) == 1
                && environment.TryGetValue("NETCOREDBG_PATH", out var configuredDebugger) ? configuredDebugger : null;

            ModernMcpProcessDriver? driver = null;
            transport = new StdioClientTransport(new StdioClientTransportOptions
            {
                Command = candidate.Command,
                Arguments = options.HostArguments is null ? candidate.Arguments : candidate.Arguments.Concat(options.HostArguments).ToArray(),
                Name = "netcoredbg-mcp-stateless-modern-contract",
                WorkingDirectory = options.StartupDirectory ?? RepositoryLayout.Root,
                InheritEnvironmentVariables = options.InheritEnvironmentVariables,
                EnvironmentVariables = environment,
                ShutdownTimeout = TimeSpan.FromSeconds(2),
                StandardErrorLines = line => driver?.AddStandardErrorLine(line),
            });

            var clientOptions = new McpClientOptions();
            if (options.ClientProtocolVersion is not null) clientOptions.ProtocolVersion = options.ClientProtocolVersion;
            if (options.NotificationHandler is { } notificationHandler)
            {
                clientOptions.Handlers = new McpClientHandlers
                {
                    NotificationHandlers = [new(NotificationMethods.ResourceUpdatedNotification, notificationHandler)],
                };
            }
#pragma warning disable MCP9005 // The fixture exercises supported legacy roots clients.
            var rootsHandler = options.RootsHandler;
            if (rootsHandler is not null)
            {
                clientOptions.Capabilities = new ClientCapabilities { Roots = new RootsCapability() };
                clientOptions.Handlers ??= new McpClientHandlers();
                clientOptions.Handlers.RootsHandler = (_, ct) => rootsHandler(ct);
            }
#pragma warning restore MCP9005
            client = await McpClient.CreateAsync(transport, clientOptions,
                cancellationToken: operation.Token).ConfigureAwait(false);
            driver = new ModernMcpProcessDriver(
                fixture,
                scratchDirectory,
                client,
                inertProgramPath,
                options.InitialMeta ?? CurrentMeta(formElicitation: !options.DisableFormElicitation), debuggerPath,
                options.HostArguments?.Contains("--legacy-compat", StringComparer.Ordinal) == true,
                OperatingSystem.IsWindows() && debuggerPath == fixture.ExecutablePath
                    && environment.Keys.Count(static key => string.Equals(key, "CONTROLLED_DAP_OPTIONS", StringComparison.OrdinalIgnoreCase)) == 1
                    && environment.TryGetValue("CONTROLLED_DAP_OPTIONS", out var fixtureOptions)
                    && fixtureOptions == new FixtureConfiguration(SuppressLifecycleEvents: true).AsEnvironmentValue());
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

    internal static async Task<ModernMcpFirstWireDriver> StartFirstWireAsync(CancellationToken cancellationToken = default, FixtureConfiguration? fixtureConfiguration = null, ModernMcpStartOptions? options = null)
    {
        options ??= new ModernMcpStartOptions();
        var scratchDirectory = ModernMcpScratchDirectory.Create();
        FixtureProcess? fixture = null;
        StdioClientTransport? transport = null;
        ITransport? connection = null;
        try
        {
            fixture = FixtureProcess.Create(options.FixtureConfiguration ?? fixtureConfiguration ?? new FixtureConfiguration());
            var candidate = TestOutputPathResolver.ResolveProcess(Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");

            var inertProgramPath = Path.Combine(scratchDirectory, "controlled-program.dll");
            using var operation = CreateBoundedCancellation(cancellationToken);
            await File.WriteAllBytesAsync(inertProgramPath, [], operation.Token).ConfigureAwait(false);

            transport = new StdioClientTransport(new StdioClientTransportOptions
            {
                Command = candidate.Command,
                Arguments = options.HostArguments is null ? candidate.Arguments : candidate.Arguments.Concat(options.HostArguments).ToArray(),
                Name = "netcoredbg-mcp-stateless-first-wire-contract",
                WorkingDirectory = options.StartupDirectory ?? RepositoryLayout.Root,
                InheritEnvironmentVariables = options.InheritEnvironmentVariables,
                EnvironmentVariables = CandidateEnvironment(fixture, inertProgramPath, options.AdditionalEnvironment, options.InheritEnvironmentVariables),
                ShutdownTimeout = TimeSpan.FromSeconds(2),
            });
            connection = await transport.ConnectAsync(operation.Token).ConfigureAwait(false);
            var driver = new ModernMcpFirstWireDriver(fixture, scratchDirectory, connection, inertProgramPath);
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
        if (name is "start_debug" or "attach_debug" or "stop_debug" or "restart_debug" or "terminate_debug")
        {
            throw new InvalidOperationException("Lifecycle tests must use CallToolRawAsync so cleanup can bind the actual request.");
        }
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

    internal async Task StopSingleControlledSessionAsync(string sessionId)
    {
        Assert.Single(await _fixture.ReadTranscriptAsync(), static entry => entry.Kind == "startup");
        var result = RequireResult(await CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = sessionId },
            CurrentMeta(), new RequestId("controlled-cleanup"), timeout: TimeSpan.FromSeconds(10)));
        Assert.False(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("stop_debug_success", result["structuredContent"]?["kind"]?.GetValue<string>());
    }

    internal async Task<JsonRpcResponse> SendRawRequestAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default,
        TimeSpan? timeout = null)
        => await SendObservedRequestAsync(method, parameters, id, cancellationToken, timeout, disposing: false).ConfigureAwait(false);

    private async Task<JsonRpcResponse> SendObservedRequestAsync(
        string method, JsonNode? parameters, RequestId id, CancellationToken cancellationToken, TimeSpan? timeout, bool disposing)
    {
        var observation = ObserveRequest(id, method, parameters, disposing);

        using var operation = CreateBoundedCancellation(cancellationToken, timeout);
        try
        {
            var response = await Client.SendRequestAsync(
                new JsonRpcRequest
                {
                    Id = id,
                    Method = method,
                    Params = parameters?.DeepClone(),
                },
                operation.Token).ConfigureAwait(false);
            CompleteObservation(observation, response.Result as JsonObject);
            return response;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            try
            {
                await Client.SendNotificationAsync(
                    NotificationMethods.CancelledNotification,
                    new CancelledNotificationParams { RequestId = id },
                    cancellationToken: CancellationToken.None).ConfigureAwait(false);
                lock (_gate)
                {
                    observation.CancellationNotified = true;
                    if (method == "tools/call" && StringValue(parameters?["name"]) == "start_debug")
                        observation.CompletedOrder = ++_lifecycleOrder;
                }
            }
            catch (Exception)
            {
                // Preserve the cancelled request's original exception.
            }
            throw;
        }
    }

    private ModernMcpRequestObservation ObserveRequest(RequestId id, string method, JsonNode? parameters, bool disposing = false)
    {
        var observation = new ModernMcpRequestObservation(id, method, parameters?.DeepClone());
        lock (_gate)
        {
            var name = method == "tools/call" ? StringValue(parameters?["name"]) : null;
            if (_disposed && !disposing && (name is "start_debug" or "attach_debug" or "stop_debug" or "restart_debug" or "terminate_debug"))
            {
                throw new ObjectDisposedException(nameof(ModernMcpProcessDriver));
            }
            if (name is "start_debug" or "attach_debug")
            {
                _fixture.MarkAdapterStartAttempted();
            }
            if (name is "start_debug" or "attach_debug" or "stop_debug" or "restart_debug" or "terminate_debug") observation.StartedOrder = ++_lifecycleOrder;
            _requests.Add(observation);
        }
        return observation;
    }

    private void CompleteObservation(ModernMcpRequestObservation observation, JsonObject? result)
    {
        if (observation.Method != "tools/call"
            || StringValue(observation.Parameters?["name"]) is not ("start_debug" or "attach_debug" or "stop_debug" or "restart_debug" or "terminate_debug"))
        {
            return;
        }
        lock (_gate)
        {
            observation.Result = result?.DeepClone().AsObject();
            observation.CompletedOrder = ++_lifecycleOrder;
        }
    }

    private static string? StringValue(JsonNode? node) =>
        node is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;

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

    /// <summary>
    /// Confirms the existing Windows basename test's no-launch case: the host uses a
    /// non-null CreateProcess application name in RepositoryLayout.Root, so a missing
    /// extensionless basename cannot resolve through PATH. An empty transcript alone
    /// or a generic DEBUG_SESSION_NOT_FOUND result does not establish this.
    /// </summary>
    internal async Task ConfirmMissingWindowsDebuggerDidNotLaunchAsync(RequestId id)
    {
        var debuggerPath = _debuggerPath;
        if (!OperatingSystem.IsWindows() || string.IsNullOrWhiteSpace(debuggerPath)
            || !string.Equals(Path.GetFileName(debuggerPath), debuggerPath, StringComparison.Ordinal)
            || Path.HasExtension(debuggerPath))
        {
            throw new InvalidOperationException("A missing Windows debugger basename is required for no-launch evidence.");
        }
        var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
        var applicationPath = Path.Combine(RepositoryLayout.Root, debuggerPath);
        if (File.Exists(applicationPath) || Directory.Exists(applicationPath)
            || transcript.Any(static entry => entry.Kind is "startup" or "request"))
        {
            throw new InvalidOperationException("The controlled debugger launch cannot be excluded.");
        }
        lock (_gate)
        {
            var starts = _requests.Where(static request => request.Method == "tools/call"
                && StringValue(request.Parameters?["name"]) == "start_debug").ToArray();
            if (_disposed || _clientClosed || starts.Length != 1 || starts[0].Id != id
                || !IsComplete(starts[0].Result, true)
                || StringValue(starts[0].Result?["structuredContent"]?["kind"]) != "debug_session_not_found"
                || StringValue(starts[0].Result?["structuredContent"]?["error"]) != "DEBUG_SESSION_NOT_FOUND"
                || starts[0].Result?["structuredContent"] is not JsonObject content || content.ContainsKey("debugSessionId"))
            {
                throw new InvalidOperationException("The no-launch evidence does not match one completed start request.");
            }
            starts[0].NoLaunchConfirmed = true;
        }
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

    internal async Task WaitForNextRequestAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            await _fixture.WaitForNextRequestAsync(cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw new Xunit.Sdk.XunitException("step_over did not emit a DAP 'next' request before the test deadline.");
        }
    }

    internal Task WaitForFixtureEventAsync(string eventName, CancellationToken cancellationToken = default) =>
        _fixture.WaitForEventAsync(eventName, cancellationToken);

    internal Task WaitForFixtureRecordAsync(string kind, CancellationToken cancellationToken = default) =>
        _fixture.WaitForTranscriptKindAsync(kind, cancellationToken);

    internal void ReleaseThreadsResponse() => _fixture.ReleaseThreadsResponse();
    internal void ReleaseStackTraceResponse() => _fixture.ReleaseStackTraceResponse();
    internal void ReleaseNextResponse() => _fixture.ReleaseNextResponse();
    internal void ReleasePauseResponse() => _fixture.ReleasePauseResponse();
    internal void ReleasePauseStoppedEvent() => _fixture.ReleasePauseStoppedEvent();
    internal void ReleasePauseStop() => _fixture.ReleasePauseStoppedEvent();
    internal void ReleaseContinueResponse() => _fixture.ReleaseContinueResponse();
    internal void ReleaseThreadsResponse(int requestSequence) => _fixture.ReleaseThreadsResponse(requestSequence);

    internal Task WaitForPauseRequestAsync(CancellationToken cancellationToken = default) =>
        _fixture.WaitForPauseRequestAsync(cancellationToken);

    internal Task WaitForDapRequestCountAsync(string command, int count, CancellationToken cancellationToken = default) =>
        _fixture.WaitForDapRequestCountAsync(command, count, cancellationToken);

    internal Task WaitForDapResponseAsync(string command, int requestSequence, CancellationToken cancellationToken = default) =>
        _fixture.WaitForDapResponseAsync(command, requestSequence, cancellationToken);

    internal Task<IReadOnlyList<FixtureTranscriptEntry>> ReadFixtureTranscriptAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return _fixture.ReadTranscriptAsync();
    }

    internal Task<int> ReadAdapterProcessIdAsync(CancellationToken cancellationToken = default) =>
        _fixture.GetProcessIdAsync(cancellationToken);

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



    internal async Task CaptureCancelledLegacyStartupAdapterAsync(RequestId id, CancellationToken cancellationToken)
    {
        if (!_legacyCompat || !_knownNoDescendantFixture || _clientClosed)
            throw new InvalidOperationException("The controlled no-descendant Windows fixture is required.");
        var transcript = await ReadFixtureTranscriptAsync(cancellationToken).ConfigureAwait(false);
        var startup = transcript.Single(static entry => entry.Kind == "startup");
        if (!transcript.Any(static entry => entry.Kind == "function-startup-response-withheld")
            || transcript.Any(static entry => entry.Kind is "descendant" or "second-windowed-descendant-published"
                || (entry.Kind == "request" && entry.Command == "launch")))
            throw new InvalidOperationException("The fixture has not reached the required pre-launch barrier.");
        var witness = Process.GetProcessById(startup.ProcessId ?? throw new InvalidOperationException("Adapter identity is missing."));
        try
        {
            ValidateWitness(witness, startup);
            lock (_gate)
            {
                var starts = _requests.Where(static request => request.Method == "tools/call" && StringValue(request.Parameters?["name"]) == "start_debug").ToArray();
                if (_disposed || _clientClosed || starts.Length != 1 || starts[0].Id != id || starts[0].CompletedOrder != 0
                    || _requests.Any(static request => request.Method == "tools/call" && StringValue(request.Parameters?["name"]) == "stop_debug")
                    || starts[0].CancellationNotified || starts[0].CancellationAdapterWitness is not null
                    || starts[0].Parameters?["arguments"]?["pre_build"]?.GetValue<bool>() != false)
                    throw new InvalidOperationException("The exact startup request is not pending before cancellation.");
                starts[0].CancellationAdapterWitness = witness;
                witness = null!;
            }
        }
        finally { witness?.Dispose(); }
    }

    internal async Task ConfirmCancelledLegacyStartupCleanupAsync(RequestId id, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ModernMcpRequestObservation start;
        Process witness;
        lock (_gate)
        {
            var starts = _requests.Where(static request => request.Method == "tools/call" && StringValue(request.Parameters?["name"]) == "start_debug").ToArray();
            if (!_knownNoDescendantFixture || _disposed || starts.Length != 1 || starts[0].Id != id
                || _requests.Any(static request => request.Method == "tools/call" && StringValue(request.Parameters?["name"]) == "stop_debug")
                || !starts[0].CancellationNotified || starts[0].Result is not null || starts[0].CancellationAdapterWitness is not { } captured)
                throw new InvalidOperationException("Cancelled startup ownership evidence is incomplete.");
            start = starts[0];
            witness = captured;
        }
        await witness.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        var transcript = await ReadFixtureTranscriptAsync(cancellationToken).ConfigureAwait(false);
        if (transcript.Any(static entry => entry.Kind is "descendant" or "second-windowed-descendant-published"
            || (entry.Kind == "request" && entry.Command == "launch")))
            throw new InvalidOperationException("The controlled startup did not remain pre-launch.");
        await CloseClientAsync(cancellationToken).ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
        lock (_gate)
        {
            if (start.CancellationAdapterWitness != witness || !start.CancellationNotified || start.Result is not null)
                throw new InvalidOperationException("Cancelled startup evidence changed during cleanup.");
            start.CancellationCleanupConfirmed = true;
            start.CancellationAdapterWitness = null;
        }
        witness.Dispose();
    }

    /// <summary>
    /// Disposes the official client and returns its public stdio completion receipt.
    /// No SDK-internal process reflection or bespoke process launch is used.
    /// </summary>
    internal async Task<StdioClientCompletionDetails> CloseClientAsync(
        CancellationToken cancellationToken = default, Process? adapterWitness = null, Process? descendantWitness = null)
    {
        ModernMcpRequestObservation? observedStart = null;
        if (adapterWitness is not null || descendantWitness is not null)
        {
            if (_clientClosed || adapterWitness is null || descendantWitness is null)
            {
                throw new InvalidOperationException("Both live controlled-process witnesses are required before client closure.");
            }
            lock (_gate)
            {
                var starts = _requests.Where(static request => request.Method == "tools/call"
                    && StringValue(request.Parameters?["name"]) == "start_debug").ToArray();
                if (starts.Length != 1 || !IsComplete(starts[0].Result, false)
                    || StringValue(starts[0].Result?["structuredContent"]?["kind"]) != "start_debug_success"
                    || string.IsNullOrWhiteSpace(StringValue(starts[0].Result?["structuredContent"]?["debugSessionId"])))
                {
                    throw new InvalidOperationException("A single successful controlled start is required for exit evidence.");
                }
                observedStart = starts[0];
            }
            var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
            var startup = transcript.Single(static entry => entry.Kind == "startup");
            var descendant = transcript.Single(static entry => entry.Kind == "descendant");
            if (startup.SimpleControlledTree != true || transcript.Any(static entry => entry.Kind == "second-windowed-descendant-published"))
            {
                throw new InvalidOperationException("The fixture does not describe one controlled adapter and descendant.");
            }
            ValidateWitness(adapterWitness, startup);
            ValidateWitness(descendantWitness, descendant);
        }
        if (!_clientClosed)
        {
            _clientClosed = true;
            await Client.DisposeAsync().ConfigureAwait(false);
        }

        var completion = await Client.Completion.WaitAsync(cancellationToken).ConfigureAwait(false);
        var stdioCompletion = Assert.IsType<StdioClientCompletionDetails>(completion);
        ProcessId = stdioCompletion.ProcessId;
        if (observedStart is not null)
        {
            await Task.WhenAll(adapterWitness!.WaitForExitAsync(cancellationToken), descendantWitness!.WaitForExitAsync(cancellationToken)).ConfigureAwait(false);
            lock (_gate) observedStart.ControlledProcessesExited = true;
        }
        return stdioCompletion;
    }

    private static void ValidateWitness(Process process, FixtureTranscriptEntry entry)
    {
        var handle = process.SafeHandle;
        if (handle.IsInvalid || entry.ProcessId != process.Id || entry.ProcessStartTimeUtcTicks is not { } expectedStart
            || process.StartTime.ToUniversalTime().Ticks != expectedStart || process.HasExited)
        {
            throw new InvalidOperationException("The controlled process witness does not match a live fixture instance.");
        }
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

    public ValueTask DisposeAsync()
    {
        lock (_gate)
        {
            if (_disposeTask is null)
            {
                _disposed = true;
                _disposeTask = DisposeCoreAsync();
            }
            return new ValueTask(_disposeTask);
        }
    }

    private async Task DisposeCoreAsync()
    {
        var cleanupFailures = new List<Exception>();
        var cleanupConfirmed = false;
        try
        {
            cleanupConfirmed = await StopKnownSessionsAsync().ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            cleanupFailures.Add(exception);
        }

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
            cleanupFailures.Add(exception);
        }
        lock (_gate)
        {
            foreach (var request in _requests)
            {
                request.CancellationAdapterWitness?.Dispose();
                request.CancellationAdapterWitness = null;
            }
        }

        if (cleanupConfirmed)
        {
            _fixture.MarkCleanupConfirmed();
        }
        try
        {
            await _fixture.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            cleanupFailures.Add(exception);
        }

        if (cleanupConfirmed && cleanupFailures.Count == 0)
        {
            try
            {
                await ModernMcpScratchDirectory.DeleteAsync(_scratchDirectory).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                cleanupFailures.Add(exception);
            }
        }

        if (!cleanupConfirmed)
        {
            cleanupFailures.Add(new InvalidOperationException($"Modern MCP cleanup is unresolved; scratch retained at '{_scratchDirectory}'."));
        }
        if (cleanupFailures.Count > 0)
        {
            throw new AggregateException("Modern MCP cleanup failed.", cleanupFailures);
        }
    }

    private async Task<bool> StopKnownSessionsAsync()
    {
        ModernMcpRequestObservation[] history;
        lock (_gate)
        {
            history = _requests
                .Where(static request => request.Method == "tools/call"
                    && StringValue(request.Parameters?["name"]) is ("start_debug" or "attach_debug" or "stop_debug" or "restart_debug" or "terminate_debug"))
                .Select(static request => new ModernMcpRequestObservation(
                    request.Id, request.Method, request.Parameters?.DeepClone())
                {
                    Result = request.Result?.DeepClone().AsObject(),
                    ControlledProcessesExited = request.ControlledProcessesExited,
                    NoLaunchConfirmed = request.NoLaunchConfirmed,
                    CancellationCleanupConfirmed = request.CancellationCleanupConfirmed,
                    StartedOrder = request.StartedOrder,
                    CompletedOrder = request.CompletedOrder,
                }).ToArray();
        }

        var legacyStarts = history.Where(static request => StringValue(request.Parameters?["name"]) == "start_debug").ToArray();
        if (_legacyCompat) return await StopKnownLegacySessionsAsync(history).ConfigureAwait(false);

        var starts = new HashSet<string>(StringComparer.Ordinal);
        var exited = new HashSet<string>(StringComparer.Ordinal);
        var stops = new Dictionary<string, bool>(StringComparer.Ordinal);
        var unconfirmed = false;
        List<Exception>? failures = null;
        foreach (var request in history)
        {
            var name = StringValue(request.Parameters?["name"]);
            if (name == "start_debug")
            {
                var result = request.Result;
                var content = result?["structuredContent"] as JsonObject;
                var token = StringValue(content?["debugSessionId"]);
                if (IsComplete(result, false) && StringValue(content?["kind"]) == "start_debug_success"
                    && content?["state"] is JsonObject && !string.IsNullOrWhiteSpace(token))
                {
                    if (!starts.Add(token)) unconfirmed = true;
                    if (request.ControlledProcessesExited) exited.Add(token);
                }
                else if (!IsPreLaunchStartResult(result) && !IsOwnerCleanedStartFailure(result) && !request.NoLaunchConfirmed)
                {
                    unconfirmed = true;
                }
            }
            else if (name == "attach_debug")
            {
                if (!IsUnknownAttachToolResult(request.Result)) unconfirmed = true;
            }
            else if (name == "stop_debug")
            {
                var token = StringValue(request.Parameters?["arguments"]?["debugSessionId"]);
                if (!string.IsNullOrWhiteSpace(token))
                {
                    var success = IsStopSuccess(request.Result);
                    if (success || !stops.ContainsKey(token)) stops[token] = success;
                }
            }
        }

        foreach (var token in starts)
        {
            if (exited.Contains(token)) continue;
            if (stops.TryGetValue(token, out var success))
            {
                if (!success) unconfirmed = true;
                continue;
            }
            if (_clientClosed)
            {
                unconfirmed = true;
                continue;
            }

            try
            {
                var response = await SendObservedRequestAsync("tools/call", new JsonObject
                {
                    ["name"] = "stop_debug",
                    ["arguments"] = new JsonObject { ["debugSessionId"] = token },
                    ["_meta"] = _legacyCompat && legacyStarts.Length == 1
                        ? legacyStarts[0].Parameters?["_meta"]?.DeepClone()
                        : _initialMeta?.DeepClone(),
                }, new RequestId($"cleanup-{Guid.NewGuid():N}"), CancellationToken.None, null, disposing: true).ConfigureAwait(false);
                if (!IsStopSuccess(response.Result as JsonObject)) unconfirmed = true;
            }
            catch (Exception exception)
            {
                (failures ??= []).Add(exception);
                unconfirmed = true;
            }
        }
        if (failures is not null)
        {
            throw new AggregateException("Could not stop all known debug sessions.", failures);
        }
        return !unconfirmed;
    }

    private async Task<bool> StopKnownLegacySessionsAsync(ModernMcpRequestObservation[] history)
    {
        ModernMcpRequestObservation? active = null;
        long lastTransition = 0;
        foreach (var request in history)
        {
            var name = StringValue(request.Parameters?["name"]);
            var payload = LegacyPayload(request.Result);
            if (name is "start_debug" or "attach_debug")
            {
                if (name == "attach_debug" && IsUnknownAttachToolResult(request.Result))
                    continue;
                if (StringValue(payload?["error"]) == "A debug session is already active; stop it before starting another.")
                    continue;
                if (request.CancellationCleanupConfirmed && active is null && request.StartedOrder > lastTransition && request.CompletedOrder > request.StartedOrder)
                {
                    lastTransition = request.CompletedOrder;
                    continue;
                }
                if (request.StartedOrder <= lastTransition || request.CompletedOrder == 0 || active is not null)
                    return false;
                if (IsComplete(request.Result, false) && payload?["data"]?["success"]?.GetValue<bool>() == true
                    && payload["error"] is null)
                {
                    active = request;
                    lastTransition = request.CompletedOrder;
                }
                else if (name == "attach_debug" && IsComplete(request.Result, false)
                    && StringValue(payload?["error"]) is { } attachError
                    && attachError.Contains("unconfirmed", StringComparison.OrdinalIgnoreCase))
                {
                    active = request;
                    lastTransition = request.CompletedOrder;
                }
                else if (IsComplete(request.Result, false) && payload?["state"]?.GetValue<string>() == "idle"
                    && StringValue(payload["error"]) is "DAP_LAUNCH_REFUSED" or "DAP_BREAKPOINT_OPTIONS_UNSUPPORTED" or "DAP_BREAKPOINTS_PROTOCOL_ERROR")
                {
                    // These results are emitted only after owned failed-start cleanup succeeds.
                    lastTransition = request.CompletedOrder;
                }
                else if (IsComplete(request.Result, false) && payload?["state"]?.GetValue<string>() == "idle"
                    && StringValue(payload["error"]) == "START_DEBUG_BUILD_FAILED")
                {
                    // The completed build failed before an adapter could be started.
                    lastTransition = request.CompletedOrder;
                }
                else return false;
            }
            else if (name == "restart_debug")
            {
                if (!IsComplete(request.Result, false) || request.CompletedOrder == 0) return false;
                if (StringValue(payload?["error"]) is "No previous launch configuration for restart"
                    or "Cannot rebuild on restart: no build_project in saved configuration"
                    or "Debug session is owned by another agent.") continue;
                if (StringValue(payload?["error"]) == "START_DEBUG_BUILD_FAILED" && StringValue(payload?["state"]) == "idle"
                    && request.StartedOrder > lastTransition
                    && (active is null || JsonNode.DeepEquals(active.Parameters?["_meta"], request.Parameters?["_meta"])))
                {
                    active = null;
                    lastTransition = request.CompletedOrder;
                    continue;
                }
                if (payload?["data"]?["success"]?.GetValue<bool>() != true || payload["error"] is not null
                    || request.StartedOrder <= lastTransition
                    || (active is not null && !JsonNode.DeepEquals(active.Parameters?["_meta"], request.Parameters?["_meta"]))) return false;
                active = request;
                lastTransition = request.CompletedOrder;
            }
            else if (name == "terminate_debug" && (!IsComplete(request.Result, false) || request.CompletedOrder == 0))
            {
                return false;
            }
            else if (name == "terminate_debug" && IsComplete(request.Result, false)
                && StringValue(payload?["state"]) == "idle" && StringValue(payload?["data"]?["state"]) == "idle"
                && StringValue(payload?["message"]) == "Adapter does not support terminate; used disconnect instead.")
            {
                if (active is null || request.StartedOrder <= lastTransition || request.CompletedOrder == 0
                    || !JsonNode.DeepEquals(active.Parameters?["_meta"], request.Parameters?["_meta"])) return false;
                active = null;
                lastTransition = request.CompletedOrder;
            }
            else if (name == "stop_debug")
            {
                if (IsComplete(request.Result, false)
                    && StringValue(payload?["error"]) is "Debug session is owned by another agent." or "No active debug session.")
                    continue;
                if (active is not null && StringValue(active.Parameters?["name"]) == "attach_debug"
                    && IsComplete(request.Result, false) && request.StartedOrder > lastTransition
                    && request.CompletedOrder > request.StartedOrder
                    && JsonNode.DeepEquals(active.Parameters?["_meta"], request.Parameters?["_meta"])
                    && StringValue(payload?["error"]) is { } stopError
                    && stopError.Contains("unconfirmed", StringComparison.OrdinalIgnoreCase))
                {
                    lastTransition = request.CompletedOrder;
                    continue;
                }
                if (active is null && request.StartedOrder > lastTransition && request.CompletedOrder > request.StartedOrder
                    && LegacyStopped(request.Result))
                {
                    lastTransition = request.CompletedOrder;
                    continue;
                }
                if (active is null || request.StartedOrder <= lastTransition || request.CompletedOrder == 0
                    || !JsonNode.DeepEquals(active.Parameters?["_meta"], request.Parameters?["_meta"])
                    || !LegacyStopped(request.Result)) return false;
                active = null;
                lastTransition = request.CompletedOrder;
            }
        }
        return active is null || await StopKnownLegacySessionAsync(active, []).ConfigureAwait(false);
    }

    private async Task<bool> StopKnownLegacySessionAsync(ModernMcpRequestObservation start, ModernMcpRequestObservation[] history)
    {
        var startPayload = LegacyPayload(start.Result);
        var knownStarted = (startPayload?["data"]?["success"]?.GetValue<bool>() == true
            && startPayload["error"] is null)
            || (StringValue(start.Parameters?["name"]) == "attach_debug"
                && StringValue(startPayload?["error"]) is { } attachError
                && attachError.Contains("unconfirmed", StringComparison.OrdinalIgnoreCase));
        var owner = start.Parameters?["_meta"]?.DeepClone();
        var stopped = history.Any(request => StringValue(request.Parameters?["name"]) == "stop_debug"
            && JsonNode.DeepEquals(owner, request.Parameters?["_meta"])
            && LegacyStopped(request.Result));
        if (knownStarted && !stopped && !_clientClosed)
        {
            var parameters = new JsonObject { ["name"] = "stop_debug", ["arguments"] = new JsonObject() };
            if (owner is not null) parameters["_meta"] = owner.DeepClone();
            var response = await SendObservedRequestAsync("tools/call", parameters,
                new RequestId($"legacy-cleanup-{Guid.NewGuid():N}"), CancellationToken.None,
                TimeSpan.FromSeconds(10), disposing: true).ConfigureAwait(false);
            stopped = LegacyStopped(response.Result as JsonObject);
        }
        return knownStarted && stopped;
    }

    private static bool LegacyStopped(JsonObject? result)
    {
        var content = LegacyPayload(result);
        return IsComplete(result, false) && content?["state"]?.GetValue<string>() == "idle"
            && content["data"]?["success"]?.GetValue<bool>() == true && content["error"] is null;
    }

    internal static JsonObject? LegacyPayload(JsonObject? result)
    {
        if (result?["isError"]?.GetValue<bool>() == true) return null;
        if (result?["structuredContent"] is JsonObject structured) return structured;
        if (result?["content"] is not JsonArray { Count: 1 } content
            || StringValue(content[0]?["text"]) is not { } text) return null;
        try { return JsonNode.Parse(text) as JsonObject; }
        catch (JsonException) { return null; }
    }

    private static bool IsComplete(JsonObject? result, bool isError)
    {
        if (StringValue(result?["resultType"]) != "complete") return false;
        var error = result?["isError"];
        return error is null ? !isError : error is JsonValue value && value.TryGetValue<bool>(out var actual) && actual == isError;
    }

    private static bool IsStopSuccess(JsonObject? result) =>
        IsComplete(result, false) && StringValue(result?["structuredContent"]?["kind"]) == "stop_debug_success"
        && result?["structuredContent"]?["state"] is JsonObject;
    internal static bool IsUnknownAttachToolResult(JsonObject? result) =>
        IsComplete(result, true) && result is not null && !result.ContainsKey("structuredContent")
        && result["content"] is JsonArray { Count: 1 } content
        && StringValue(content[0]?["type"]) == "text"
        && StringValue(content[0]?["text"]) == "Unknown tool: attach_debug";


    internal static bool IsPreLaunchStartResult(JsonObject? result)
    {
        if (StringValue(result?["resultType"]) == "input_required"
            && result?["isError"] is null && result?["structuredContent"] is null
            && result?["inputRequests"] is JsonObject requests && requests.Count > 0)
        {
            return true;
        }
        if (!IsComplete(result, true) || result?["structuredContent"] is not JsonObject content
            || content.ContainsKey("debugSessionId")) return false;
        return (StringValue(content["kind"]), StringValue(content["error"])) switch
        {
            ("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS") => StringValue(content["tool"]) == "start_debug",
            ("start_debug_input_unavailable", "START_DEBUG_PROGRAM_REQUIRED") => true,
            ("start_debug_source_unavailable", "START_DEBUG_SOURCE_UNAVAILABLE") => true,
            ("start_debug_cwd_unavailable", "START_DEBUG_CWD_UNAVAILABLE") => true,
            ("start_debug_dotnet_unavailable", "START_DEBUG_DOTNET_UNAVAILABLE") => true,
            ("start_debug_project_unavailable", "START_DEBUG_PROJECT_UNAVAILABLE") => true,
            ("start_debug_launch_profile_invalid", "START_DEBUG_LAUNCH_PROFILE_INVALID") => true,
            ("start_debug_build_project_required", "START_DEBUG_BUILD_PROJECT_REQUIRED") => true,
            ("start_debug_build_project_invalid", "START_DEBUG_BUILD_PROJECT_INVALID") => true,
            ("start_debug_build_busy", "START_DEBUG_BUILD_BUSY") => true,
            ("start_debug_build_timeout", "START_DEBUG_BUILD_TIMEOUT") => true,
            ("start_debug_build_start_failed", "START_DEBUG_BUILD_START_FAILED") => true,
            ("start_debug_build_failed", "START_DEBUG_BUILD_FAILED") => true,
            ("start_debug_program_unavailable", "START_DEBUG_PROGRAM_UNAVAILABLE") => true,
            _ => false,
        };
    }

    internal static bool IsOwnerCleanedStartFailure(JsonObject? result)
    {
        if (!IsComplete(result, true) || result?["structuredContent"] is not JsonObject content
            || content.ContainsKey("debugSessionId")) return false;
        // These errors escape StartAsync only after its failed-start cleanup succeeds.
        return (StringValue(content["kind"]), StringValue(content["error"])) is
            ("dap_breakpoints_refused", "DAP_BREAKPOINTS_REFUSED") or
            ("dap_breakpoints_protocol_error", "DAP_BREAKPOINTS_PROTOCOL_ERROR") or
            ("dap_launch_refused", "DAP_LAUNCH_REFUSED");
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
        IReadOnlyDictionary<string, string?>? additionalEnvironment = null,
        bool inheritEnvironmentVariables = true)
    {
        var environment = inheritEnvironmentVariables
            ? new Dictionary<string, string?>()
            : StdioClientTransportOptions.GetDefaultEnvironmentVariables();
        if (!inheritEnvironmentVariables)
        {
            foreach (var name in new[]
            {
                "DOTNET_ROOT", "DOTNET_ROOT_X64", "DOTNET_ROOT_X86", "DOTNET_ROOT_ARM64", "DOTNET_HOST_PATH",
                "CONTROLLED_DAP_TRANSCRIPT", "CONTROLLED_DAP_OPTIONS", "CONTROLLED_DAP_GRACEFUL_RELEASE",
                "CONTROLLED_DAP_CONFIGURATION_DONE_CAPABILITY_DELTA_RELEASE", "CONTROLLED_DAP_SECOND_WINDOWED_DESCENDANT_RELEASE",
                "CONTROLLED_DAP_THREADS_RESPONSE_RELEASE", "CONTROLLED_DAP_STACK_TRACE_RESPONSE_RELEASE", "CONTROLLED_DAP_NEXT_RESPONSE_RELEASE",
                "CONTROLLED_DAP_WINDOWED_DESCENDANT_EXECUTABLE", "CONTROLLED_DAP_WINDOWED_DESCENDANT_ARGUMENTS",
            })
            {
                if (Environment.GetEnvironmentVariable(name) is { Length: > 0 } value)
                {
                    environment[name] = value;
                }
            }
        }

        environment["NETCOREDBG_PATH"] = fixture.ExecutablePath;
        environment["NETCOREDBG_PROGRAM_PATH"] = inertProgramPath;
        if (additionalEnvironment is not null)
        {
            foreach (var (name, value) in additionalEnvironment)
            {
                if (!inheritEnvironmentVariables && value is null)
                {
                    environment.Remove(name);
                }
                else
                {
                    environment[name] = value;
                }
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

#pragma warning disable MCP9005 // Roots options remain necessary for compatibility tests.
internal sealed record ModernMcpStartOptions(
    JsonObject? InitialMeta = null,
    bool DisableFormElicitation = false,
    string? PriorProcessToken = null,
    FixtureConfiguration? FixtureConfiguration = null,
    IReadOnlyDictionary<string, string?>? AdditionalEnvironment = null,
    bool InheritEnvironmentVariables = true,
    string? ClientProtocolVersion = null,
    IReadOnlyList<string>? HostArguments = null,
    string? StartupDirectory = null,
    Func<CancellationToken, ValueTask<ListRootsResult>>? RootsHandler = null,
    Func<JsonRpcNotification, CancellationToken, ValueTask>? NotificationHandler = null);
#pragma warning restore MCP9005

/// <summary>Kind is the controlled transcript kind: <c>startup</c> or <c>request</c>.</summary>
internal sealed record ModernNativeAction(string Kind, string? Command, string? Detail);

internal sealed record ModernMcpRequestObservation(RequestId Id, string Method, JsonNode? Parameters)
{
    internal JsonObject? Result { get; set; }
    internal bool ControlledProcessesExited { get; set; }
    internal bool NoLaunchConfirmed { get; set; }
    internal bool CancellationNotified { get; set; }
    internal bool CancellationCleanupConfirmed { get; set; }
    internal Process? CancellationAdapterWitness { get; set; }
    internal long StartedOrder { get; set; }
    internal long CompletedOrder { get; set; }
}


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

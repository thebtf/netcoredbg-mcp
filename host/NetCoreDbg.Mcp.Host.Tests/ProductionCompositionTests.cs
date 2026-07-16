using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves RelayComposition's actual production wiring: Program-equivalent upstream chain,
/// Roots + ResourceUpdates handlers once, ProgressLogging/ResourceUpdates registered once,
/// ProgressLoggingRelay.ConfigureFilters as the sole logging/progress filter pair, interleaved
/// progress/log/resource ordering, capability present/absent truth, duplicate ownership,
/// cancellation/disconnect, terminal host/session race ties, and drain completion. Every
/// fixture pairs the real <see cref="RelayComposition.Build"/> output against a real
/// McpClient/McpServer over an in-memory <see cref="DuplexChannel"/>.
/// </summary>
public sealed class ProductionCompositionTests
{
    private const string StateUri = "debug://state";
    private const string BreakpointsUri = "debug://breakpoints";
    private const string ProbeToolName = "probe";

    private sealed class EventLog
    {
        private int _sequence;

        public ConcurrentQueue<(int Sequence, string Label)> Events { get; } = new();

        public int Record(string label)
        {
            var sequence = Interlocked.Increment(ref _sequence);
            Events.Enqueue((sequence, label));
            return sequence;
        }
    }

    /// <summary>
    /// Observes true arrival order on the downstream leg (SDK notification handlers alone
    /// can fire out of wire order).
    /// </summary>
    private sealed class SequentialOrderObserverTransport(IClientTransport inner, EventLog log) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default)
        {
            var innerTransport = await inner.ConnectAsync(cancellationToken).ConfigureAwait(false);
            return new ObservingSessionTransport(innerTransport, log);
        }

        private sealed class ObservingSessionTransport : ITransport
        {
            private readonly ITransport _inner;
            private readonly EventLog _log;
            private readonly ConcurrentDictionary<string, string> _progressTokensByRequestId = new();
            private readonly Channel<JsonRpcMessage> _passthrough =
                Channel.CreateUnbounded<JsonRpcMessage>(new UnboundedChannelOptions
                {
                    SingleReader = true,
                    SingleWriter = true,
                });
            private readonly Task _pumpTask;

            public ObservingSessionTransport(ITransport inner, EventLog log)
            {
                _inner = inner;
                _log = log;
                _pumpTask = PumpAsync();
            }

            public string? SessionId => _inner.SessionId;

            public ChannelReader<JsonRpcMessage> MessageReader => _passthrough.Reader;

            public Task SendMessageAsync(JsonRpcMessage message, CancellationToken cancellationToken = default)
            {
                if (message is JsonRpcRequest { Method: RequestMethods.ToolsCall } request
                    && request.Params?.Deserialize<CallToolRequestParams>(McpJsonUtilities.DefaultOptions)?.ProgressToken is { } token)
                {
                    _progressTokensByRequestId[request.Id.ToString()] = token.ToString()!;
                    _log.Record($"request|{token}");
                }

                return _inner.SendMessageAsync(message, cancellationToken);
            }

            private async Task PumpAsync()
            {
                Exception? failure = null;
                try
                {
                    await foreach (var message in _inner.MessageReader.ReadAllAsync().ConfigureAwait(false))
                    {
                        switch (message)
                        {
                            case JsonRpcNotification { Method: NotificationMethods.ProgressNotification } progress:
                                var progressParams = progress.Params.Deserialize<ProgressNotificationParams>(McpJsonUtilities.DefaultOptions)!;
                                _log.Record($"progress|{progressParams.ProgressToken}|{progressParams.Progress.Progress}");
                                break;

                            case JsonRpcNotification { Method: NotificationMethods.LoggingMessageNotification } logging:
                                var loggingParams = logging.Params.Deserialize<LoggingMessageNotificationParams>(McpJsonUtilities.DefaultOptions)!;
                                var data = loggingParams.Data.ValueKind == JsonValueKind.String
                                    ? loggingParams.Data.GetString()
                                    : loggingParams.Data.GetRawText();
                                _log.Record($"log|{loggingParams.Logger}|{data}");
                                break;

                            case JsonRpcNotification { Method: NotificationMethods.ResourceUpdatedNotification } resource:
                                _log.Record($"resource|{resource.Params?["uri"]?.GetValue<string>()}");
                                break;

                            case JsonRpcResponse response:
                                var responseId = response.Id.ToString();
                                _log.Record(
                                    _progressTokensByRequestId.TryRemove(responseId, out var token)
                                        ? $"response|{token}"
                                        : $"response|{responseId}");
                                break;
                        }

                        await _passthrough.Writer.WriteAsync(message).ConfigureAwait(false);
                    }
                }
                catch (Exception ex)
                {
                    failure = ex;
                }
                finally
                {
                    _passthrough.Writer.TryComplete(failure);
                }
            }

            public async ValueTask DisposeAsync()
            {
                await _inner.DisposeAsync().ConfigureAwait(false);
                try
                {
                    await _pumpTask.ConfigureAwait(false);
                }
                catch
                {
                    // Pump failure already completed the passthrough channel.
                }
            }
        }
    }

    private static FakePythonServer StartFrontDoorFakePython(
        DuplexChannel channel,
        bool advertiseLogging,
        bool advertiseResources,
        bool advertiseMux,
        int probeSteps = 2,
        Func<int, CancellationToken, Task>? afterStep = null)
    {
        async ValueTask<CallToolResult> RunProbeAsync(
            RequestContext<CallToolRequestParams> context,
            CancellationToken cancellationToken)
        {
            var token = context.Params?.ProgressToken;
            for (var step = 1; step <= probeSteps; step++)
            {
                if (token is { } activeToken)
                {
                    await context.Server.SendMessageAsync(
                        new JsonRpcNotification
                        {
                            Method = NotificationMethods.ProgressNotification,
                            Params = JsonSerializer.SerializeToNode(
                                new ProgressNotificationParams
                                {
                                    ProgressToken = activeToken,
                                    Progress = new ProgressNotificationValue
                                    {
                                        Progress = step,
                                        Total = probeSteps,
                                    },
                                },
                                McpJsonUtilities.DefaultOptions),
                        },
                        cancellationToken).ConfigureAwait(false);
                }

                await context.Server.SendMessageAsync(
                    new JsonRpcNotification
                    {
                        Method = NotificationMethods.LoggingMessageNotification,
                        Params = JsonSerializer.SerializeToNode(
                            new LoggingMessageNotificationParams
                            {
                                Level = LoggingLevel.Info,
                                Logger = "fake-python",
                                Data = JsonSerializer.SerializeToElement($"log-{step}"),
                            },
                            McpJsonUtilities.DefaultOptions),
                    },
                    cancellationToken).ConfigureAwait(false);

                await context.Server.SendMessageAsync(
                    new JsonRpcNotification
                    {
                        Method = NotificationMethods.ResourceUpdatedNotification,
                        Params = new JsonObject
                        {
                            ["uri"] = step == 1 ? StateUri : BreakpointsUri,
                            ["_meta"] = new JsonObject { ["step"] = step },
                        },
                    },
                    cancellationToken).ConfigureAwait(false);

                if (afterStep is not null)
                {
                    await afterStep(step, cancellationToken).ConfigureAwait(false);
                }
            }

            return new CallToolResult { Content = [new TextContentBlock { Text = "done" }] };
        }

        var experimental = advertiseMux
            ? new Dictionary<string, object>
            {
                ["x-mux"] = JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement,
            }
            : null;

        var options = new McpServerOptions
        {
            ServerInfo = new Implementation { Name = "fake-python-frontdoor", Version = "1.0.0" },
            Capabilities = new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Resources = advertiseResources
                    ? new ResourcesCapability { Subscribe = true, ListChanged = false }
                    : null,
                Experimental = experimental,
            },
            Handlers = new McpServerHandlers
            {
                ListToolsHandler = (context, cancellationToken) => ValueTask.FromResult(new ListToolsResult
                {
                    Tools =
                    [
                        new Tool { Name = "echo", InputSchema = JsonDocument.Parse("{\"type\":\"object\"}").RootElement },
                        new Tool { Name = ProbeToolName, InputSchema = JsonDocument.Parse("{\"type\":\"object\"}").RootElement },
                    ],
                }),
                CallToolHandler = (context, cancellationToken) => context.Params?.Name == ProbeToolName
                    ? RunProbeAsync(context, cancellationToken)
                    : ValueTask.FromResult(new CallToolResult
                    {
                        Content = [new TextContentBlock { Text = "ok" }],
                    }),
                ListResourcesHandler = advertiseResources
                    ? (context, cancellationToken) => ValueTask.FromResult(new ListResourcesResult
                    {
                        Resources =
                        [
                            new Resource { Uri = StateUri, Name = "state", MimeType = "application/json" },
                            new Resource { Uri = BreakpointsUri, Name = "breakpoints", MimeType = "application/json" },
                        ],
                    })
                    : null,
                ReadResourceHandler = advertiseResources
                    ? (context, cancellationToken) => ValueTask.FromResult(new ReadResourceResult
                    {
                        Contents = [new TextResourceContents { Uri = context.Params!.Uri, Text = "{}", MimeType = "application/json" }],
                    })
                    : null,
                SubscribeToResourcesHandler = advertiseResources
                    ? (context, cancellationToken) => ValueTask.FromResult(new EmptyResult())
                    : null,
                UnsubscribeFromResourcesHandler = advertiseResources
                    ? (context, cancellationToken) => ValueTask.FromResult(new EmptyResult())
                    : null,
                SetLoggingLevelHandler = advertiseLogging
                    ? (context, cancellationToken) => ValueTask.FromResult(new EmptyResult())
                    : null,
            },
        };

        if (!advertiseLogging)
        {
            options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                    && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                    && capabilitiesNode is JsonObject capabilities)
                {
                    capabilities.Remove("logging");
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            });
        }

        return FakePythonServer.Start(channel, options);
    }

    private static CallToolRequestParams ProbeCall(string progressToken) => new()
    {
        Name = ProbeToolName,
        Meta = new JsonObject { ["progressToken"] = progressToken },
    };

    [Fact]
    public void DuplicateRouteRegistration_Throws()
    {
        var catalog = new RelayRouteCatalog();
        catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));

        var duplicate = Assert.Throws<InvalidOperationException>(() =>
            catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request)));
        Assert.Contains("Duplicate relay route ownership", duplicate.Message);

        // The same method name in the opposite direction is a distinct, allowed route.
        catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.UpstreamToDownstream, RelayRouteKind.Request));
    }

    [Fact]
    public void ProductionComposition_RegistersProgressLoggingAndResourceUpdatesExactlyOnce()
    {
        var catalog = new RelayRouteCatalog();
        var builder = new ServiceCollection();
        var mcpBuilder = builder.AddMcpServer();
        var session = new RelaySession(
            () => throw new InvalidOperationException("not started"),
            RelayComposition.RequiredUpstreamCapabilityChecks);

        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);
        ResourceUpdatesRelay.Register(mcpBuilder, catalog, session);

        var progressDuplicate = Assert.Throws<InvalidOperationException>(() =>
            ProgressLoggingRelay.Register(mcpBuilder, catalog, session));
        Assert.Contains("Duplicate relay route ownership", progressDuplicate.Message);

        var resourceDuplicate = Assert.Throws<InvalidOperationException>(() =>
            ResourceUpdatesRelay.Register(mcpBuilder, catalog, session));
        Assert.Contains("Duplicate relay route ownership", resourceDuplicate.Message);
    }

    [Fact]
    public void ResourceUpdatesHandlers_ConfiguredTwice_ThrowDuplicateOwnership()
    {
        var ordered = ResourceUpdatesRelay.CreateOrderedUpstream();
        var session = new RelaySession(
            () => throw new InvalidOperationException("not started"),
            RelayComposition.RequiredUpstreamCapabilityChecks);
        var handlers = new McpClientHandlers();
        ordered.ConfigureHandlers(handlers, session);

        var duplicate = Assert.Throws<InvalidOperationException>(() =>
            ordered.ConfigureHandlers(handlers, session));
        Assert.Contains(NotificationMethods.ResourceUpdatedNotification, duplicate.Message);
    }

    [Fact]
    public async Task ProductionComposition_AdvertisesNoTestOnlyReverseCapability()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        // The production projector default is empty ClientCapabilities, so Roots/Sampling/
        // Elicitation never reach the upstream fake Python from this composition path.
        var advertisedToPython = fixture.FakePython.Server.ClientCapabilities;
        Assert.NotNull(advertisedToPython);
        Assert.Null(advertisedToPython!.Roots);
        Assert.Null(advertisedToPython.Sampling);
        Assert.Null(advertisedToPython.Elicitation);
    }

    [Fact]
    public async Task ProductionComposition_DoesNotAdvertiseLoggingAndRejectsSetLevel()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        Assert.Null(fixture.DownstreamClient.ServerCapabilities?.Logging);

        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => fixture.DownstreamClient.SetLoggingLevelAsync(LoggingLevel.Info));
        Assert.Contains("Method not found", error.Message);
    }

    [Fact]
    public async Task ProductionComposition_AdvertisesAndServesNativePrompts()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        var promptsCapability = fixture.DownstreamClient.ServerCapabilities.Prompts;
        Assert.NotNull(promptsCapability);
        Assert.False(promptsCapability!.ListChanged);

        var prompts = await fixture.DownstreamClient.ListPromptsAsync(new ListPromptsRequestParams());
        Assert.Equal(
            new[]
            {
                "debug", "debug-gui", "debug-exception", "debug-visual", "debug-mistakes",
                "investigate", "debug-scenario", "dap-escape-hatch",
            },
            prompts.Prompts.Select(prompt => prompt.Name));

        var rendered = await fixture.DownstreamClient.GetPromptAsync("debug");
        Assert.False(string.IsNullOrWhiteSpace(rendered.Description));
        Assert.NotEmpty(rendered.Messages);
    }

    [Fact]
    public async Task ToolsListAndCallTool_ForwardUnchanged()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        var tools = await fixture.DownstreamClient.ListToolsAsync(new ListToolsRequestParams());
        Assert.Contains(tools.Tools, tool => tool.Name == "echo");

        var callResult = await fixture.DownstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "echo", Arguments = new Dictionary<string, JsonElement>() });
        Assert.True(callResult.IsError is null or false);
    }

    [Fact]
    public async Task Ping_IsAnsweredWithNoUpstreamPairedAtAll()
    {
        // ping is core SDK infrastructure, never a relay route: a bare downstream McpServer
        // built with zero RelaySession/bootstrap wiring still answers it.
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("ping-only"),
            new McpServerOptions { ServerInfo = new Implementation { Name = "ping-only", Version = "1.0.0" } });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());
        var pingResult = await client.PingAsync();

        Assert.NotNull(pingResult);
        await server.DisposeAsync();
    }

    [Fact]
    public async Task MissingUpstreamCapability_FailsBootstrapAndEndsSession()
    {
        var upstreamChannel = new DuplexChannel();
        await using var fakePython = FakePythonServer.StartWithoutTools(upstreamChannel);

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        await Assert.ThrowsAsync<McpProtocolException>(
            () => McpClient.CreateAsync(downstreamChannel.CreateClientTransport()));

        var sessionEndedFailure = await Assert.ThrowsAsync<InvalidOperationException>(
            () => session.RunUntilSessionEndedAsync(CancellationToken.None));
        Assert.Contains("do not cover a route", sessionEndedFailure.Message);

        await host.StopAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ObserveTerminalRace_HostWinsWhenAnyButSessionAlreadyFaulted_SurfacesBackendDeath()
    {
        // Simultaneous-completion tie forced deterministically: host completes first (WhenAny
        // selects it), then sessionEndedTask is already faulted when the post-host check runs.
        // Production must still observe the backend fault (never exit 0).
        var hostRunTask = Task.CompletedTask;
        var sessionFault = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        sessionFault.SetException(
            new InvalidOperationException("The Python backend ended before the downstream MCP session closed."));
        var sessionEndedTask = sessionFault.Task;
        // Ensure host is the "first" completed task from WhenAny's perspective by only
        // attaching the already-faulted session after host is known complete — same terminal
        // state as a true tie, with deterministic WhenAny selection of hostRunTask.
        Assert.True(hostRunTask.IsCompletedSuccessfully);
        Assert.True(sessionEndedTask.IsFaulted);

        var error = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            RelayComposition.ObserveTerminalRaceAsync(
                hostRunTask,
                sessionEndedTask,
                () => Task.CompletedTask));

        Assert.Contains("Python backend ended", error.Message);
    }

    [Fact]
    public async Task ObserveTerminalRace_SessionEndsFirst_StopsHostAndSurfacesBackendDeath()
    {
        var hostCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var sessionEndedTask = Task.FromException(
            new InvalidOperationException("The Python backend ended before the downstream MCP session closed."));
        var stopHostCalls = 0;

        var error = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            RelayComposition.ObserveTerminalRaceAsync(
                hostCompletion.Task,
                sessionEndedTask,
                () =>
                {
                    Interlocked.Increment(ref stopHostCalls);
                    hostCompletion.TrySetResult();
                    return Task.CompletedTask;
                }));

        Assert.Contains("Python backend ended", error.Message);
        Assert.Equal(1, stopHostCalls);
        Assert.True(hostCompletion.Task.IsCompletedSuccessfully);
    }

    [Fact]
    public async Task ObserveTerminalRace_CleanHostCompletionWithoutSessionEnd_Succeeds()
    {
        var hostRunTask = Task.CompletedTask;
        var sessionEndedTask = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously).Task;

        await RelayComposition.ObserveTerminalRaceAsync(
            hostRunTask,
            sessionEndedTask,
            () => Task.FromException(new InvalidOperationException("stop must not run on clean host-first exit")));
    }

    [Fact]
    public async Task RunPairedAsync_BackendDeathAfterBootstrap_DoesNotReportCleanExit()
    {
        var upstreamChannel = new DuplexChannel();
        await using var fakePython = FakePythonServer.StartWithEchoTool(upstreamChannel);

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());

        // RunPairedAsync owns host.RunAsync — start it before the downstream client connects.
        var runTask = RelayComposition.RunPairedAsync(host, session);
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport())
            .WaitAsync(TimeSpan.FromSeconds(10));
        Assert.NotNull(downstreamClient.ServerCapabilities?.Tools);

        upstreamChannel.SimulateServerExit();

        var error = await Assert.ThrowsAsync<InvalidOperationException>(() => runTask)
            .WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Contains("Python backend ended", error.Message);

        await session.DisposeAsync();
    }

    [Fact]
    public async Task ForwardRequestAsync_NormalizesRemoteErrorToOneHopCleanMessageAndCode()
    {
        // FD-000 RelaySession error-unwrapping correction: one hop clean message/code.
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("no-handlers"),
            new McpServerOptions { ServerInfo = new Implementation { Name = "no-handlers", Version = "1.0.0" } });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        var request = new JsonRpcRequest { Method = RequestMethods.PromptsList };
        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => RelaySession.ForwardRequestAsync(client, request, CancellationToken.None));

        Assert.DoesNotContain("Request failed (remote): Request failed (remote):", error.Message);
        Assert.False(error.Message.StartsWith("Request failed (remote): ", StringComparison.Ordinal), error.Message);
        var inner = Assert.IsType<McpProtocolException>(error.InnerException);
        Assert.StartsWith("Request failed (remote): ", inner.Message, StringComparison.Ordinal);
        Assert.Equal(inner.ErrorCode, error.ErrorCode);

        await server.DisposeAsync();
    }

    [Fact]
    public async Task ForwardRequestAsync_PreservesPrimitiveAndNestedDataEntriesAcrossNormalization()
    {
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("throws-with-data"),
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "throws-with-data", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, cancellationToken) =>
                    {
                        var ex = new McpProtocolException("boom", McpErrorCode.InvalidParams);
                        ex.Data["reason"] = "bad";
                        ex.Data["count"] = 3d;
                        ex.Data["nested"] = JsonDocument.Parse("{\"a\":[1,true]}").RootElement.Clone();
                        throw ex;
                    },
                    CallToolHandler = (context, cancellationToken) => ValueTask.FromResult(new CallToolResult()),
                },
            });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        var request = new JsonRpcRequest { Method = RequestMethods.ToolsList };
        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => RelaySession.ForwardRequestAsync(client, request, CancellationToken.None));

        Assert.Equal("boom", error.Message);
        Assert.Equal(McpErrorCode.InvalidParams, error.ErrorCode);
        Assert.Equal("bad", Assert.IsType<string>(error.Data["reason"]));
        Assert.Equal(3d, Assert.IsType<double>(error.Data["count"]));
        var nested = Assert.IsType<JsonElement>(error.Data["nested"]);
        Assert.Equal(JsonValueKind.Object, nested.ValueKind);
        Assert.Equal("{\"a\":[1,true]}", nested.GetRawText());

        await server.DisposeAsync();
    }

    [Fact]
    public async Task CapabilityTruth_LoggingResourcesAndMuxPresentOrAbsentMatchPython()
    {
        await using var absent = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: false,
                advertiseResources: false,
                advertiseMux: false));
        Assert.Null(absent.DownstreamClient.ServerCapabilities?.Logging);
        Assert.Null(absent.DownstreamClient.ServerCapabilities?.Resources);
        Assert.Null(absent.DownstreamClient.ServerCapabilities?.Experimental);

        await using var present = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: true,
                advertiseResources: true,
                advertiseMux: true));
        Assert.NotNull(present.DownstreamClient.ServerCapabilities?.Logging);
        Assert.NotNull(present.DownstreamClient.ServerCapabilities?.Resources);
        Assert.True(present.DownstreamClient.ServerCapabilities!.Resources!.Subscribe);
        Assert.False(present.DownstreamClient.ServerCapabilities.Resources.ListChanged);
        var experimental = present.DownstreamClient.ServerCapabilities.Experimental;
        Assert.NotNull(experimental);
        Assert.Equal("x-mux", Assert.Single(experimental!.Keys));
    }

    [Fact]
    public async Task InterleavedProgressLogAndResourceUpdates_PreserveProgramChainOrder()
    {
        // Program chain: ProgressLogging is the outer transport and forwards progress/log
        // synchronously before reading the next upstream message; ResourceUpdates is the
        // inner stamp/drain path and preserves resource-update wire order independently.
        // Composition must therefore prove both pipelines on one real call without forcing
        // a false cross-pipeline total order that the two independent pumps do not claim.
        var log = new EventLog();
        await using var fixture = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: true,
                advertiseResources: true,
                advertiseMux: true,
                probeSteps: 2),
            wrapDownstreamTransport: transport => new SequentialOrderObserverTransport(transport, log));

        var result = await fixture.DownstreamClient.CallToolAsync(ProbeCall("token-order"));
        Assert.True(result.IsError is null or false);

        var labels = log.Events.OrderBy(e => e.Sequence).Select(e => e.Label).ToArray();
        Assert.Contains("request|token-order", labels);
        Assert.Contains("progress|token-order|1", labels);
        Assert.Contains("log|fake-python|log-1", labels);
        Assert.Contains($"resource|{StateUri}", labels);
        Assert.Contains("progress|token-order|2", labels);
        Assert.Contains("log|fake-python|log-2", labels);
        Assert.Contains($"resource|{BreakpointsUri}", labels);
        Assert.Contains("response|token-order", labels);

        var requestSeq = log.Events.Single(e => e.Label == "request|token-order").Sequence;
        var responseSeq = log.Events.Single(e => e.Label == "response|token-order").Sequence;
        var progress1 = log.Events.Single(e => e.Label == "progress|token-order|1").Sequence;
        var log1 = log.Events.Single(e => e.Label == "log|fake-python|log-1").Sequence;
        var resource1 = log.Events.Single(e => e.Label == $"resource|{StateUri}").Sequence;
        var progress2 = log.Events.Single(e => e.Label == "progress|token-order|2").Sequence;
        var log2 = log.Events.Single(e => e.Label == "log|fake-python|log-2").Sequence;
        var resource2 = log.Events.Single(e => e.Label == $"resource|{BreakpointsUri}").Sequence;

        // ProgressLogging outer pump: progress/log stay in wire order and never trail the
        // owning call's terminal result.
        Assert.True(requestSeq < progress1);
        Assert.True(progress1 < log1);
        Assert.True(log1 < progress2);
        Assert.True(progress2 < log2);
        Assert.True(log2 < responseSeq);
        Assert.True(progress1 < responseSeq);
        Assert.True(progress2 < responseSeq);

        // ResourceUpdates inner pipeline: resource updates keep source wire order.
        Assert.True(requestSeq < resource1);
        Assert.True(resource1 < resource2);
    }

    [Fact]
    public async Task LoggingCapabilityAbsent_SuppressesLogsWhileProgressAndResourcesContinue()
    {
        var log = new EventLog();
        await using var fixture = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: false,
                advertiseResources: true,
                advertiseMux: false,
                probeSteps: 2),
            wrapDownstreamTransport: transport => new SequentialOrderObserverTransport(transport, log));

        Assert.Null(fixture.DownstreamClient.ServerCapabilities?.Logging);
        await fixture.DownstreamClient.CallToolAsync(ProbeCall("token-no-log"));

        Assert.Equal(2, log.Events.Count(e => e.Label.StartsWith("progress|token-no-log|", StringComparison.Ordinal)));
        Assert.Equal(2, log.Events.Count(e => e.Label.StartsWith("resource|", StringComparison.Ordinal)));
        Assert.DoesNotContain(log.Events, e => e.Label.StartsWith("log|", StringComparison.Ordinal));
    }

    [Fact]
    public async Task CancellationMidProbe_CleansTokenAndKeepsSessionUsable()
    {
        var firstStepStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fixture = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: true,
                advertiseResources: true,
                advertiseMux: true,
                probeSteps: 1000,
                afterStep: async (step, cancellationToken) =>
                {
                    if (step == 1)
                    {
                        firstStepStarted.TrySetResult();
                    }

                    await Task.Delay(TimeSpan.FromMilliseconds(20), cancellationToken).ConfigureAwait(false);
                }));

        using var cancellation = new CancellationTokenSource();
        var slow = fixture.DownstreamClient.CallToolAsync(ProbeCall("token-slow"), cancellationToken: cancellation.Token);
        await firstStepStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => slow.AsTask());

        var followUp = await fixture.DownstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "echo", Arguments = new Dictionary<string, JsonElement>() })
            .AsTask()
            .WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(followUp.IsError is null or false);
    }

    [Fact]
    public async Task DownstreamDisconnectDuringInFlightWork_SessionDisposesCleanly()
    {
        var firstStepStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var fixture = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: true,
                advertiseResources: true,
                advertiseMux: true,
                probeSteps: 1000,
                afterStep: async (step, cancellationToken) =>
                {
                    if (step == 1)
                    {
                        firstStepStarted.TrySetResult();
                    }

                    await Task.Delay(TimeSpan.FromMilliseconds(20), cancellationToken).ConfigureAwait(false);
                }));

        _ = fixture.DownstreamClient.CallToolAsync(ProbeCall("token-disconnect"));
        await firstStepStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));

        await fixture.DisposeAsync();
    }

    [Fact]
    public async Task SessionDispose_CompletesResourceUpdateDrainHook()
    {
        await using var fixture = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: false,
                advertiseResources: true,
                advertiseMux: false));

        await fixture.DownstreamClient.CallToolAsync(ProbeCall("token-drain"));
        await fixture.Session.DisposeAsync();
        await fixture.ResourceUpdates.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task RootsCapability_ProjectedOnlyWhenDownstreamAdvertisesRoots()
    {
        await using var withRoots = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: false,
                advertiseResources: false,
                advertiseMux: false),
            downstreamClientOptions: new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability { ListChanged = true } },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult
                    {
                        Roots = [new Root { Uri = "file:///tmp/root", Name = "root" }],
                    }),
                },
            });

        // Program-equivalent projector: same RootsRelay instance sees downstream roots
        // during bootstrap and advertises them upstream only then.
        Assert.NotNull(withRoots.FakePython.Server.ClientCapabilities?.Roots);
        Assert.Equal(true, withRoots.FakePython.Server.ClientCapabilities!.Roots!.ListChanged);

        await using var withoutRoots = await ComposedFixture.StartAsync(
            startFakePython: channel => StartFrontDoorFakePython(
                channel,
                advertiseLogging: false,
                advertiseResources: false,
                advertiseMux: false));
        Assert.Null(withoutRoots.FakePython.Server.ClientCapabilities?.Roots);
    }
}

using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves <see cref="RootsRelay"/> (FD-001, Engram #385) end to end using real SDK
/// endpoints, never mocks: capability projection (including the SDK's own
/// handler-presence-derives-capability behavior that requires per-instance gating - see
/// <see cref="RootsRelay"/>'s own remarks), the upstream <c>roots/list</c> reverse route
/// (zero/many roots, URI/name/<c>_meta</c> preservation, readiness gating, cancellation/
/// teardown, absent-capability fail-safe), and the downstream-to-upstream
/// <c>notifications/roots/list_changed</c> forward.
///
/// <see cref="RelayComposition.Build"/> is integration-owned and does not yet call
/// <see cref="RootsRelay.Register"/> (see architecture.md and the integration hook reported
/// with this change), so the <c>list_changed</c> fixture below builds its own minimal
/// test-only downstream host that mirrors <see cref="RelayComposition.Build"/> plus this
/// module's <see cref="RootsRelay.Register"/> call - a stand-in for the not-yet-wired
/// production composition, never a mock of the module logic itself. Every other fixture
/// here reaches <see cref="RootsRelay.ConfigureUpstreamHandlers"/> and
/// <see cref="RootsRelay.ProjectCapabilities"/> through the real, unmodified
/// <see cref="RelayComposition.Build"/>/<see cref="RelaySession"/>, exactly as
/// <c>ReverseRouteAndLifecycleTests</c> does for the generic FD-000 primitives.
/// </summary>
public sealed class RootsRelayTests
{
    private static (RelaySession Session, DuplexChannel Upstream, DuplexChannel Downstream) BuildRootsSession()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var rootsRelay = new RootsRelay();

        RelaySession? session = null;
        session = new RelaySession(
            upstreamChannel.CreateClientTransport,
            RelayComposition.RequiredUpstreamCapabilityChecks,
            handlers => rootsRelay.ConfigureUpstreamHandlers(handlers, session!));

        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            downstreamCapabilities => rootsRelay.ProjectCapabilities(downstreamCapabilities, new ClientCapabilities()));
        _ = host.RunAsync();

        return (session, upstreamChannel, downstreamChannel);
    }

    /// <summary>
    /// Mirrors <see cref="RelayComposition.Build"/> plus this module's own
    /// <see cref="RootsRelay.Register"/> call - the one line the integrator adds once this
    /// module is accepted (see architecture.md's "integrator adds the accepted module"
    /// rule) - so the downstream-to-upstream <c>list_changed</c> half can be exercised today
    /// without editing the integration-owned production file.
    /// </summary>
    private static IHost BuildDownstreamHostWithRootsModule(
        RelaySession session, RootsRelay rootsRelay, Stream downstreamInput, Stream downstreamOutput, RelayRouteCatalog catalog)
    {
        var notificationState = new ProgressLoggingRelay.NotificationState();
        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);

        var mcpBuilder = builder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = "roots-relay-test-host", Version = "1.0.0" };
            options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability() };
            // Same production filter pair RelayComposition uses — never a retired parallel path.
            ProgressLoggingRelay.ConfigureFilters(options.Filters, session, notificationState);
            options.Filters.Message.IncomingFilters.Add(
                session.CreateBootstrapFilter(caps => rootsRelay.ProjectCapabilities(caps, new ClientCapabilities())));
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);
        RootsRelay.Register(mcpBuilder, catalog, session);
        mcpBuilder.WithStreamServerTransport(downstreamInput, downstreamOutput);

        return builder.Build();
    }

    // ---- ProjectCapabilities: pure per-instance projection, no I/O ----

    [Fact]
    public void ProjectCapabilities_DownstreamRootsAbsent_UpstreamRootsStaysAbsent()
    {
        var upstream = new ClientCapabilities();

        var result = new RootsRelay().ProjectCapabilities(downstreamCapabilities: null, upstream);

        Assert.Same(upstream, result);
        Assert.Null(result.Roots);
    }

    [Fact]
    public void ProjectCapabilities_DownstreamHasOtherCapabilitiesButNoRoots_DoesNotSetRoots()
    {
        var downstream = new ClientCapabilities { Sampling = new SamplingCapability() };

        var result = new RootsRelay().ProjectCapabilities(downstream, new ClientCapabilities());

        Assert.Null(result.Roots);
    }

    [Fact]
    public void ProjectCapabilities_DownstreamDeclaresRootsWithListChangedTrue_ProjectsSameFlag()
    {
        var downstream = new ClientCapabilities { Roots = new RootsCapability { ListChanged = true } };

        var result = new RootsRelay().ProjectCapabilities(downstream, new ClientCapabilities());

        Assert.NotNull(result.Roots);
        Assert.Equal((bool?)true, result.Roots!.ListChanged);
    }

    [Fact]
    public void ProjectCapabilities_DownstreamListChangedFalse_PreservesFalseRatherThanDefaultingTrue()
    {
        var downstream = new ClientCapabilities { Roots = new RootsCapability { ListChanged = false } };

        var result = new RootsRelay().ProjectCapabilities(downstream, new ClientCapabilities());

        Assert.NotNull(result.Roots);
        Assert.Equal((bool?)false, result.Roots!.ListChanged);
    }

    [Fact]
    public void ProjectCapabilities_DownstreamListChangedNull_PreservesNullRatherThanDefaulting()
    {
        var downstream = new ClientCapabilities { Roots = new RootsCapability() };

        var result = new RootsRelay().ProjectCapabilities(downstream, new ClientCapabilities());

        Assert.NotNull(result.Roots);
        Assert.Null(result.Roots!.ListChanged);
    }

    // ---- ConfigureUpstreamHandlers gating: the regression this suite exists to pin down.
    // ---- SDK 1.4.1 derives ClientCapabilities.Roots from whether RootsHandler is set at
    // ---- all, independent of the Capabilities object itself, so an unconditional
    // ---- registration would silently defeat ProjectCapabilities' own gate. ----

    [Fact]
    public void ConfigureUpstreamHandlers_SkipsWiringWhenProjectCapabilitiesNeverSawRoots()
    {
        var upstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var rootsRelay = new RootsRelay();
        // ProjectCapabilities is never called with a Roots-declaring downstream: the real
        // client never advertised it.

        var handlers = new McpClientHandlers();
        rootsRelay.ConfigureUpstreamHandlers(handlers, session);

        Assert.Null(handlers.RootsHandler);
    }

    [Fact]
    public void ConfigureUpstreamHandlers_WiresHandlerOnlyAfterProjectCapabilitiesSawRoots()
    {
        var upstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var rootsRelay = new RootsRelay();
        rootsRelay.ProjectCapabilities(new ClientCapabilities { Roots = new RootsCapability() }, new ClientCapabilities());

        var handlers = new McpClientHandlers();
        rootsRelay.ConfigureUpstreamHandlers(handlers, session);

        Assert.NotNull(handlers.RootsHandler);
    }

    // ---- ConfigureUpstreamHandlers: isolated readiness/cancellation contract, no real
    // ---- downstream/upstream connection at all ----

    [Fact]
    public async Task ConfigureUpstreamHandlers_BlocksUntilDownstreamReady_ThenSessionDisposalCancelsPendingCall()
    {
        var upstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var rootsRelay = new RootsRelay();
        rootsRelay.ProjectCapabilities(new ClientCapabilities { Roots = new RootsCapability() }, new ClientCapabilities());

        var handlers = new McpClientHandlers();
        rootsRelay.ConfigureUpstreamHandlers(handlers, session);

        var pending = handlers.RootsHandler!(new ListRootsRequestParams(), CancellationToken.None).AsTask();

        await Task.Delay(TimeSpan.FromMilliseconds(200));
        Assert.False(
            pending.IsCompleted,
            "RootsHandler must await DownstreamReady before touching the downstream session - " +
            "it must not proceed just because no real downstream client has connected yet.");

        // Simulates the orchestration layer's eventual teardown (Program.cs's
        // `await using relaySession`): the pending call must be cancelled, never left
        // hanging forever, on session end - proving SessionEndingToken linking.
        await session.DisposeAsync();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pending)
            .WaitAsync(TimeSpan.FromSeconds(10));
    }

    // ---- Register: catalog bookkeeping (RelayRouteCatalog is pure bookkeeping; it does not
    // ---- itself wire any handler, so this only proves route registration/duplication) ----

    [Fact]
    public void Register_AddsBothRoutesOnce_SecondRegistrationOnSameCatalogThrows()
    {
        var catalog = new RelayRouteCatalog();
        var upstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var hostBuilder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        var mcpBuilder = hostBuilder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = "catalog-test", Version = "1.0.0" };
        });

        RootsRelay.Register(mcpBuilder, catalog, session);

        Assert.Contains(
            catalog.Routes,
            r => r.Method == RequestMethods.RootsList
                && r.Direction == RelayDirection.UpstreamToDownstream
                && r.Kind == RelayRouteKind.Request);
        Assert.Contains(
            catalog.Routes,
            r => r.Method == NotificationMethods.RootsListChangedNotification
                && r.Direction == RelayDirection.DownstreamToUpstream
                && r.Kind == RelayRouteKind.Notification);

        var duplicate = Assert.Throws<InvalidOperationException>(() => RootsRelay.Register(mcpBuilder, catalog, session));
        Assert.Contains("Duplicate relay route ownership", duplicate.Message);
    }

    // ---- End-to-end reverse roots/list: real fake-Python server + real downstream client ----

    [Fact]
    public async Task ReverseRootsList_ZeroRoots_ForwardsEmptyListUnchanged()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        var roots = await context.Server.RequestRootsAsync(new ListRootsRequestParams(), ct);
                        return new CallToolResult { Content = [new TextContentBlock { Text = roots.Roots.Count.ToString() }] };
                    },
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult { Roots = [] }),
                },
            });

        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" });

        Assert.False(result.IsError == true);
        var text = Assert.IsType<TextContentBlock>(result.Content[0]);
        Assert.Equal("0", text.Text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRootsList_ManyRoots_PreservesUriNameAndMetaExactly()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        ListRootsResult? observedAtPython = null;
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        observedAtPython = await context.Server.RequestRootsAsync(new ListRootsRequestParams(), ct);
                        return new CallToolResult
                        {
                            Content = [new TextContentBlock { Text = observedAtPython.Roots.Count.ToString() }],
                        };
                    },
                },
            });

        var rootMeta = new JsonObject { ["scope"] = "solution" };
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult
                    {
                        Roots =
                        [
                            new Root { Uri = "file:///workspace/one", Name = "one", Meta = rootMeta },
                            new Root { Uri = "file:///workspace/two", Name = "two" },
                        ],
                    }),
                },
            });

        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" });

        Assert.False(result.IsError == true);
        Assert.NotNull(observedAtPython);
        Assert.Equal(2, observedAtPython!.Roots.Count);
        Assert.Equal("file:///workspace/one", observedAtPython.Roots[0].Uri);
        Assert.Equal("one", observedAtPython.Roots[0].Name);
        Assert.Equal("solution", observedAtPython.Roots[0].Meta?["scope"]?.GetValue<string>());
        Assert.Equal("file:///workspace/two", observedAtPython.Roots[1].Uri);
        Assert.Equal("two", observedAtPython.Roots[1].Name);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRootsList_RequestMetaAndProgressToken_SurviveTheRoundTrip()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        JsonObject? observedRequestMeta = null;
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        var requestParams = new ListRootsRequestParams
                        {
                            Meta = new JsonObject { ["progressToken"] = "python-owned-token" },
                        };
                        await context.Server.RequestRootsAsync(requestParams, ct);
                        return new CallToolResult();
                    },
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = (requestParams, ct) =>
                    {
                        observedRequestMeta = requestParams?.Meta;
                        return ValueTask.FromResult(new ListRootsResult { Roots = [] });
                    },
                },
            });

        await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" });

        Assert.NotNull(observedRequestMeta);
        Assert.Equal("python-owned-token", observedRequestMeta!["progressToken"]?.GetValue<string>());

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRootsList_CapabilityAbsentDownstream_PythonFailsClosedInsteadOfReachingHandler()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        try
                        {
                            await context.Server.RequestRootsAsync(new ListRootsRequestParams(), ct);
                            return new CallToolResult { Content = [new TextContentBlock { Text = "unexpected-success" }] };
                        }
                        catch (Exception ex)
                        {
                            return new CallToolResult { IsError = true, Content = [new TextContentBlock { Text = ex.GetType().Name }] };
                        }
                    },
                },
            });

        // A real downstream client that genuinely does not declare Roots: ProjectCapabilities
        // therefore never advertises Roots upstream *and* ConfigureUpstreamHandlers never
        // wires RootsHandler at all for this session, so Python's own RequestRootsAsync must
        // fail its own local capability check instead of ever reaching this module's handler
        // - the capability gate working as designed, not a hang.
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" })
            .AsTask().WaitAsync(TimeSpan.FromSeconds(10));

        Assert.True(result.IsError);
        var text = Assert.IsType<TextContentBlock>(result.Content[0]);
        Assert.Equal(nameof(InvalidOperationException), text.Text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRootsList_DownstreamDeclaresRootsButHasNoHandler_FailsClosedWithoutHanging()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        try
                        {
                            await context.Server.RequestRootsAsync(new ListRootsRequestParams(), ct);
                            return new CallToolResult { Content = [new TextContentBlock { Text = "unexpected-success" }] };
                        }
                        catch (Exception ex)
                        {
                            return new CallToolResult { IsError = true, Content = [new TextContentBlock { Text = ex.GetType().Name }] };
                        }
                    },
                },
            });

        // Declares Roots (so this module correctly wires its reverse handler and Python's
        // own local capability check passes), but never actually implements RootsHandler -
        // a real, if non-compliant, client. Proves the *forwarding* failure mode - this
        // module reaching a downstream that cannot answer - is a clean, bounded protocol
        // error, never a hang.
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions { Capabilities = new ClientCapabilities { Roots = new RootsCapability() } });

        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" })
            .AsTask().WaitAsync(TimeSpan.FromSeconds(10));

        Assert.True(result.IsError);
        var text = Assert.IsType<TextContentBlock>(result.Content[0]);
        Assert.Equal(nameof(McpProtocolException), text.Text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRootsList_SessionDisposalWhileForwardedCallPending_CancelsRatherThanHangs()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildRootsSession();

        var reverseCallStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    CallToolHandler = async (context, ct) =>
                    {
                        reverseCallStarted.TrySetResult();
                        await context.Server.RequestRootsAsync(new ListRootsRequestParams(), ct);
                        return new CallToolResult();
                    },
                },
            });

        var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    // Never answers: proves the *forwarded, in-flight* reverse call, not just
                    // the pre-readiness wait already covered above, also unblocks on session
                    // teardown instead of hanging forever.
                    RootsHandler = async (requestParams, ct) =>
                    {
                        await Task.Delay(Timeout.Infinite, ct);
                        return new ListRootsResult { Roots = [] };
                    },
                },
            });

        var call = downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "slow" });
        await reverseCallStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));

        // Simulates the orchestration layer's eventual teardown (Program.cs's
        // `await using relaySession`) rather than the SDK's own per-request cancellation.
        await session.DisposeAsync();

        // Bounded completion is the "no hang" proof: the pending reverse call is cancelled,
        // so Python's own RequestRootsAsync throws inside CallToolHandler, and the SDK
        // reports that as an error result rather than a protocol-level failure to the caller.
        var result = await call.AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(result.IsError);

        await downstreamClient.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    // ---- notifications/roots/list_changed: downstream -> upstream ----

    [Fact]
    public async Task RootsListChanged_DownstreamPush_ReachesUpstreamPythonWithMetaPreserved()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var catalog = new RelayRouteCatalog();
        var rootsRelay = new RootsRelay();

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        using var host = BuildDownstreamHostWithRootsModule(
            session, rootsRelay, downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream, catalog);
        _ = host.RunAsync();

        var received = new TaskCompletionSource<JsonNode?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    NotificationHandlers =
                    [
                        new(NotificationMethods.RootsListChangedNotification, (notification, ct) =>
                        {
                            received.TrySetResult(notification.Params);
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability { ListChanged = true } },
            });

        var pushedParams = new RootsListChangedNotificationParams
        {
            Meta = new JsonObject { ["reason"] = "editor-save" },
        };
        await downstreamClient.SendNotificationAsync(
            NotificationMethods.RootsListChangedNotification,
            pushedParams,
            McpJsonUtilities.DefaultOptions,
            CancellationToken.None);

        var observedParams = await received.Task.WaitAsync(TimeSpan.FromSeconds(10));

        Assert.NotNull(observedParams);
        Assert.Equal("editor-save", observedParams!["_meta"]?["reason"]?.GetValue<string>());

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
        await host.StopAsync();
    }
}

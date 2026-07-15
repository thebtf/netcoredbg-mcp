using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves RelaySession's generic forwarding primitive in both directions using real SDK
/// endpoints (McpServer/McpClient), never mocks. The reverse-route vehicle is
/// <c>roots/list</c> - the only public client-side typed request slot SDK 1.4.1 exposes -
/// and a synthetic notification method for the one-way push; neither is ever wired by
/// RelayComposition, so this stays test-only per the FD-000 contract.
/// </summary>
public sealed class ReverseRouteAndLifecycleTests
{
    private const string TestNotificationMethod = "x-fd000-test/notification";

    private static (RelaySession Session, DuplexChannel Upstream, DuplexChannel Downstream) BuildReverseRouteSession(
        Action<McpClientHandlers>? extraUpstreamHandlers = null)
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        RelaySession? session = null;
        session = new RelaySession(
            upstreamChannel.CreateClientTransport,
            RelayComposition.RequiredUpstreamCapabilityChecks,
            handlers =>
            {
                handlers.RootsHandler = async (typedParams, cancellationToken) =>
                {
                    // Mirror RelaySession's documented reverse-route contract: Python must
                    // never reach the downstream client before it has actually completed
                    // notifications/initialized, so the test-only handler awaits the same
                    // readiness signal a real FD-001 module would.
                    await session!.DownstreamReady.WaitAsync(cancellationToken).ConfigureAwait(false);
                    var forwarded = new JsonRpcRequest
                    {
                        Method = RequestMethods.RootsList,
                        Params = JsonSerializer.SerializeToNode(typedParams, McpJsonUtilities.DefaultOptions),
                    };
                    var response = await RelaySession
                        .ForwardRequestAsync(session!.Downstream!, forwarded, cancellationToken)
                        .ConfigureAwait(false);
                    return response.Result!.Deserialize<ListRootsResult>(McpJsonUtilities.DefaultOptions)!;
                };
                extraUpstreamHandlers?.Invoke(handlers);
            });

        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities { Roots = new RootsCapability() });
        _ = host.RunAsync();

        return (session, upstreamChannel, downstreamChannel);
    }

    [Fact]
    public async Task ReverseRequest_NestedCallback_ForwardsThroughToDownstreamAndBack()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildReverseRouteSession();

        // The fake Python's tool handler nests a reverse roots/list call inside a downstream
        // tools/call - downstream call -> Python -> downstream roots request -> Python result.
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
                        return new CallToolResult
                        {
                            Content = [new TextContentBlock { Text = roots.Roots.Count.ToString() }],
                        };
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
                    RootsHandler = (requestParams, ct) => ValueTask.FromResult(
                        new ListRootsResult { Roots = [new Root { Uri = "file:///nested", Name = "nested" }] }),
                },
            });

        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" });
        Assert.False(result.IsError == true);
        var text = Assert.IsType<TextContentBlock>(result.Content[0]);
        Assert.Equal("1", text.Text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseNotification_OneWayPush_ReachesDownstream()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildReverseRouteSession();

        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
            });

        var received = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Handlers = new McpClientHandlers
                {
                    NotificationHandlers =
                    [
                        new(TestNotificationMethod, (notification, ct) =>
                        {
                            received.TrySetResult(notification.Params?["text"]?.GetValue<string>() ?? string.Empty);
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });

        await session.DownstreamReady;
        var payload = new JsonRpcNotification
        {
            Method = TestNotificationMethod,
            Params = System.Text.Json.Nodes.JsonNode.Parse("""{"text":"pushed"}"""),
        };
        await RelaySession.ForwardNotificationAsync(session.Downstream!, payload, CancellationToken.None);

        var text = await received.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal("pushed", text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRequest_ConcurrentDistinctProgressTokensAndSameTokenOppositeDirections_DoNotCrossCorrelate()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildReverseRouteSession();
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
                        // Reflect the caller's own progress token back so the test can
                        // confirm the reverse roots/list carried the SAME token value the
                        // downstream call used - opposite directions, same token value,
                        // no cross-correlation because each direction's SDK-assigned
                        // connection-local request ID is independent of the token.
                        var progressToken = context.Params?.ProgressToken;
                        var rootsParams = new ListRootsRequestParams();
                        if (progressToken is { } token)
                        {
                            rootsParams.Meta = new System.Text.Json.Nodes.JsonObject { ["progressToken"] = token.Token switch
                            {
                                string s => s,
                                long l => l,
                                _ => null,
                            } };
                        }

                        var roots = await context.Server.RequestRootsAsync(rootsParams, ct);
                        return new CallToolResult { Content = [new TextContentBlock { Text = roots.Roots.Count.ToString() }] };
                    },
                },
            });

        var observedUpstreamTokens = new System.Collections.Concurrent.ConcurrentBag<string?>();
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = (requestParams, ct) =>
                    {
                        observedUpstreamTokens.Add(requestParams?.ProgressToken?.ToString());
                        return ValueTask.FromResult(new ListRootsResult { Roots = [] });
                    },
                },
            });

        // Same token value ("shared-token") used by two calls whose progress reporting
        // direction differs is exactly the "equal token in opposite directions" case; two
        // further distinct tokens run concurrently alongside it.
        var callA = downstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "a", Meta = new System.Text.Json.Nodes.JsonObject { ["progressToken"] = "shared-token" } });
        var callB = downstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "b", Meta = new System.Text.Json.Nodes.JsonObject { ["progressToken"] = "distinct-token-b" } });
        var callC = downstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "c", Meta = new System.Text.Json.Nodes.JsonObject { ["progressToken"] = "distinct-token-c" } });

        await Task.WhenAll(callA.AsTask(), callB.AsTask(), callC.AsTask());

        Assert.Equal(3, observedUpstreamTokens.Count);
        Assert.Contains("shared-token", observedUpstreamTokens);
        Assert.Contains("distinct-token-b", observedUpstreamTokens);
        Assert.Contains("distinct-token-c", observedUpstreamTokens);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ReverseRequest_CancellationAndCallerDeadlinePropagate()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildReverseRouteSession();
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

        // A downstream handler that never answers roots/list until cancelled: proves a
        // caller-supplied deadline (linked cancellation token) propagates all the way across
        // the relay to the reverse leg, not just the forward one.
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                Handlers = new McpClientHandlers
                {
                    RootsHandler = async (requestParams, ct) =>
                    {
                        await Task.Delay(Timeout.Infinite, ct);
                        return new ListRootsResult { Roots = [] };
                    },
                },
            });

        using var deadline = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var call = downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "slow" }, cancellationToken: deadline.Token);
        await reverseCallStarted.Task;

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => call.AsTask());

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task DownstreamDisconnect_CancelsSessionEndingTokenAndDisposalIsIdempotent()
    {
        var (session, upstreamChannel, _) = BuildReverseRouteSession();
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions { ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" } });

        var token = session.SessionEndingToken;
        Assert.False(token.IsCancellationRequested);

        // Idempotent, one owner: concurrent disposal races must not throw or double-run.
        var disposeA = session.DisposeAsync().AsTask();
        var disposeB = session.DisposeAsync().AsTask();
        await Task.WhenAll(disposeA, disposeB);

        Assert.True(token.IsCancellationRequested);

        // A third, later disposal is still a no-op.
        await session.DisposeAsync();

        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task UpstreamSessionEnds_ReportsSessionEndedAndCleanupIsIdempotent()
    {
        var upstreamChannel = new DuplexChannel();
        var fakePython = FakePythonServer.StartWithEchoTool(upstreamChannel);

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var sessionEndedTask = session.RunUntilSessionEndedAsync(CancellationToken.None);
        Assert.False(sessionEndedTask.IsCompleted);

        // Simulate the child exiting: complete its outgoing pipe so the client transport
        // observes end-of-stream, exactly as a real process closing stdout would.
        upstreamChannel.SimulateServerExit();

        var ended = await Assert.ThrowsAsync<InvalidOperationException>(() => sessionEndedTask)
            .WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Contains("Python backend ended", ended.Message);

        // Double-terminal race: session disposal after the upstream already ended is still
        // idempotent and safe.
        await session.DisposeAsync();
        await session.DisposeAsync();
    }
}

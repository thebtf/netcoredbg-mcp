using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

public sealed class ResourceUpdatesRelayTests
{
    private const string StateUri = "debug://state";
    private const string BreakpointsUri = "debug://breakpoints";

    private static (RelaySession Session, DuplexChannel Upstream, DuplexChannel Downstream) BuildSession()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        return (session, upstream, downstream);
    }

    private static McpServerOptions SubscribablePythonOptions(
        McpRequestHandler<SubscribeRequestParams, EmptyResult>? subscribe = null,
        McpRequestHandler<UnsubscribeRequestParams, EmptyResult>? unsubscribe = null) =>
        new()
        {
            ServerInfo = new Implementation { Name = "fake-python-updates", Version = "1.0.0" },
            Capabilities = new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Resources = new ResourcesCapability { Subscribe = true, ListChanged = false },
            },
            Handlers = new McpServerHandlers
            {
                ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                SubscribeToResourcesHandler = subscribe ?? ((context, ct) => ValueTask.FromResult(new EmptyResult())),
                UnsubscribeFromResourcesHandler = unsubscribe ?? ((context, ct) => ValueTask.FromResult(new EmptyResult())),
            },
        };

    private static JsonRpcRequest SubscriptionRequest(string method, string uri, string marker) =>
        new()
        {
            Method = method,
            Params = new JsonObject
            {
                ["uri"] = uri,
                ["_meta"] = new JsonObject { ["marker"] = marker },
            },
        };

    private static async Task<ITransport> ConnectRawDownstreamAsync(DuplexChannel downstream)
    {
        var transport = await downstream.CreateClientTransport().ConnectAsync();
        await transport.SendMessageAsync(
            new JsonRpcRequest
            {
                Id = new RequestId(1),
                Method = RequestMethods.Initialize,
                Params = JsonSerializer.SerializeToNode(
                    new InitializeRequestParams
                    {
                        ProtocolVersion = "2025-06-18",
                        Capabilities = new ClientCapabilities(),
                        ClientInfo = new Implementation
                        {
                            Name = "fd006-raw-order-client",
                            Version = "1.0.0",
                        },
                    },
                    McpJsonUtilities.DefaultOptions),
            });
        var initializeResponse = await transport.MessageReader
            .ReadAsync()
            .AsTask()
            .WaitAsync(TimeSpan.FromSeconds(10));
        Assert.IsType<JsonRpcResponse>(initializeResponse);
        await transport.SendMessageAsync(
            new JsonRpcNotification
            {
                Method = NotificationMethods.InitializedNotification,
            });
        return transport;
    }

    [Fact]
    public async Task SubscribeAndUnsubscribe_ForwardRawMetaAndPreservePythonErrors()
    {
        var (session, upstream, downstream) = BuildSession();
        var subscriptions = new HashSet<string>(StringComparer.Ordinal);
        var seen = new List<(string Method, string Uri, string? Marker)>();

        await using var fakePython = FakePythonServer.Start(
            upstream,
            SubscribablePythonOptions(
                subscribe: (context, ct) =>
                {
                    var uri = context.Params!.Uri;
                    if (uri == "debug://unknown")
                    {
                        throw new McpProtocolException("Unknown resource: debug://unknown", McpErrorCode.InvalidParams);
                    }

                    subscriptions.Add(uri);
                    seen.Add((
                        context.JsonRpcRequest.Method,
                        uri,
                        context.JsonRpcRequest.Params?["_meta"]?["marker"]?.GetValue<string>()));
                    return ValueTask.FromResult(new EmptyResult());
                },
                unsubscribe: (context, ct) =>
                {
                    var uri = context.Params!.Uri;
                    subscriptions.Remove(uri);
                    seen.Add((
                        context.JsonRpcRequest.Method,
                        uri,
                        context.JsonRpcRequest.Params?["_meta"]?["marker"]?.GetValue<string>()));
                    return ValueTask.FromResult(new EmptyResult());
                }));

        await using var client = await McpClient.CreateAsync(downstream.CreateClientTransport());

        await client.SendRequestAsync(
            SubscriptionRequest(RequestMethods.ResourcesSubscribe, StateUri, "first"),
            CancellationToken.None);
        await client.SendRequestAsync(
            SubscriptionRequest(RequestMethods.ResourcesSubscribe, StateUri, "duplicate"),
            CancellationToken.None);

        Assert.Single(subscriptions);
        Assert.Equal(
            [
                (RequestMethods.ResourcesSubscribe, StateUri, "first"),
                (RequestMethods.ResourcesSubscribe, StateUri, "duplicate"),
            ],
            seen);

        await client.SendRequestAsync(
            SubscriptionRequest(RequestMethods.ResourcesUnsubscribe, StateUri, "remove"),
            CancellationToken.None);
        Assert.Empty(subscriptions);
        Assert.Equal((RequestMethods.ResourcesUnsubscribe, StateUri, "remove"), seen[^1]);

        var error = await Assert.ThrowsAsync<McpProtocolException>(async () =>
            await client.SendRequestAsync(
                SubscriptionRequest(RequestMethods.ResourcesSubscribe, "debug://unknown", "error"),
                CancellationToken.None));
        Assert.Equal(McpErrorCode.InvalidParams, error.ErrorCode);
        Assert.Contains("Unknown resource", error.Message);

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task SubscribeRequest_CancellationPropagatesToPython()
    {
        var (session, upstream, downstream) = BuildSession();
        var started = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fakePython = FakePythonServer.Start(
            upstream,
            SubscribablePythonOptions(subscribe: async (context, cancellationToken) =>
            {
                started.TrySetResult();
                await Task.Delay(Timeout.Infinite, cancellationToken);
                return new EmptyResult();
            }));
        await using var client = await McpClient.CreateAsync(downstream.CreateClientTransport());

        using var cancellation = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var call = client.SendRequestAsync(
            SubscriptionRequest(RequestMethods.ResourcesSubscribe, StateUri, "cancel"),
            cancellation.Token);
        await started.Task;

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => call);

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourceUpdatedNotifications_PreserveRawPayloadAndSourceWireOrdering()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        await session.DownstreamReady;
        var first = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://state","_meta":{"sequence":1,"opaque":"keep"}}"""),
        };
        var second = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://breakpoints","_meta":{"sequence":2}}"""),
        };

        await RelaySession.ForwardNotificationAsync(fakePython.Server, first, CancellationToken.None);
        await RelaySession.ForwardNotificationAsync(fakePython.Server, second, CancellationToken.None);

        var receivedFirst = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var receivedSecond = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(StateUri, receivedFirst.Params!["uri"]!.GetValue<string>());
        Assert.Equal(BreakpointsUri, receivedSecond.Params!["uri"]!.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(first.Params, receivedFirst.Params));
        Assert.True(JsonNode.DeepEquals(second.Params, receivedSecond.Params));

        await session.DisposeAsync();
    }

    [Fact]
    public async Task OrderedUpstreamTransport_PropagatesCompletionAndDisposesWithoutRetainedWork()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var client = await McpClient.CreateAsync(downstream.CreateClientTransport());
        await session.DownstreamReady;

        upstream.SimulateServerExit();
        var ended = session.RunUntilSessionEndedAsync(CancellationToken.None);
        var error = await Assert.ThrowsAsync<InvalidOperationException>(
            () => ended.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Contains("Python backend ended", error.Message);

        await client.DisposeAsync();
        await session.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ResourceUpdatedNotification_CancellationDisconnectAndTerminalSuppressSend()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var received = new ConcurrentQueue<JsonRpcNotification>();
        var twoReceived = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

        await using var client = await McpClient.CreateAsync(
            downstream.CreateClientTransport(),
            new McpClientOptions
            {
                Handlers = new McpClientHandlers
                {
                    NotificationHandlers =
                    [
                        new(NotificationMethods.ResourceUpdatedNotification, (notification, ct) =>
                        {
                            received.Enqueue(notification);
                            if (received.Count == 2)
                            {
                                twoReceived.TrySetResult();
                            }
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });
        await session.DownstreamReady;

        var orderedUpstream = ResourceUpdatesRelay.CreateOrderedUpstream();
        var handlers = new McpClientHandlers();
        orderedUpstream.ConfigureHandlers(handlers, session);
        var callback = Assert.Single(handlers.NotificationHandlers!).Value;
        Assert.Throws<InvalidOperationException>(() =>
            orderedUpstream.ConfigureHandlers(handlers, session));

        var first = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://state"}"""),
        };
        var second = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://breakpoints"}"""),
        };
        orderedUpstream.StampReceivedMessage(first);
        orderedUpstream.StampReceivedMessage(second);
        var secondCallback = callback(second, CancellationToken.None).AsTask();
        Assert.False(secondCallback.IsCompleted);
        var firstCallback = callback(first, CancellationToken.None).AsTask();
        await Task.WhenAll(firstCallback, secondCallback);
        await twoReceived.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(2, received.Count);

        var cancelledNotification = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://output"}"""),
        };
        orderedUpstream.StampReceivedMessage(cancelledNotification);
        using (var cancelled = new CancellationTokenSource())
        {
            cancelled.Cancel();
            await callback(cancelledNotification, cancelled.Token);
        }
        Assert.Equal(2, received.Count);

        var missingPredecessor = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://threads"}"""),
        };
        var queuedAtTerminal = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://state"}"""),
        };
        orderedUpstream.StampReceivedMessage(missingPredecessor);
        orderedUpstream.StampReceivedMessage(queuedAtTerminal);
        var queuedCallback = callback(queuedAtTerminal, CancellationToken.None).AsTask();
        Assert.False(queuedCallback.IsCompleted);

        await client.DisposeAsync();
        await session.DisposeAsync();
        await queuedCallback.WaitAsync(TimeSpan.FromSeconds(10));
        await callback(missingPredecessor, CancellationToken.None);
        Assert.Equal(2, received.Count);
    }
}

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
    public async Task ResourceUpdatedNotifications_PreserveRawPayloadAndOrdering()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var received = new ConcurrentQueue<JsonRpcNotification>();
        var receivedTwo = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

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
                                receivedTwo.TrySetResult();
                            }
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });

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
        await receivedTwo.Task.WaitAsync(TimeSpan.FromSeconds(10));

        var ordered = received.ToArray();
        Assert.Equal([StateUri, BreakpointsUri], ordered.Select(item => item.Params!["uri"]!.GetValue<string>()));
        Assert.True(JsonNode.DeepEquals(first.Params, ordered[0].Params));
        Assert.True(JsonNode.DeepEquals(second.Params, ordered[1].Params));

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourceUpdatedNotification_CancellationDisconnectAndTerminalSuppressSend()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var received = new ConcurrentQueue<JsonRpcNotification>();
        var liveReceived = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

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
                            liveReceived.TrySetResult();
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });
        await session.DownstreamReady;

        var handlers = new McpClientHandlers();
        ResourceUpdatesRelay.ConfigureUpstreamHandlers(handlers, session);
        var callback = Assert.Single(handlers.NotificationHandlers!).Value;
        Assert.Throws<InvalidOperationException>(() =>
            ResourceUpdatesRelay.ConfigureUpstreamHandlers(handlers, session));

        var live = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"debug://state"}"""),
        };
        await callback(live, CancellationToken.None);
        await liveReceived.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Single(received);

        using (var cancelled = new CancellationTokenSource())
        {
            cancelled.Cancel();
            await callback(live, cancelled.Token);
        }
        Assert.Single(received);

        await client.DisposeAsync();
        await session.DisposeAsync();
        await callback(live, CancellationToken.None);
        await Task.Delay(100);
        Assert.Single(received);
    }
}

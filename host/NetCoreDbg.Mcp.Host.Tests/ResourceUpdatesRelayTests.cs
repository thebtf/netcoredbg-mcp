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

[Collection("ResourceUpdatesRelaySerial")]
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
        // Callbacks complete immediately; ordered drain still delivers wire order.
        await callback(second, CancellationToken.None);
        await callback(first, CancellationToken.None);
        await twoReceived.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
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
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
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
        // Second of the pair is pending behind the missing predecessor callback.
        await callback(queuedAtTerminal, CancellationToken.None);
        Assert.Equal(1, orderedUpstream.PendingUriCount);

        await client.DisposeAsync();
        await session.DisposeAsync();
        await callback(missingPredecessor, CancellationToken.None);
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(2, received.Count);
        Assert.Equal(0, orderedUpstream.PendingUriCount);
    }

    [Fact]
    public async Task BlockedSubscriber_CoalescesPendingUpdatesPerUri_BoundedRetainedWork()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var received = new ConcurrentQueue<string>();
        var receivedMarkers = new ConcurrentQueue<string>();

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
                            received.Enqueue(notification.Params!["uri"]!.GetValue<string>());
                            var marker = notification.Params?["_meta"]?["marker"]?.GetValue<string>();
                            if (marker is not null)
                            {
                                receivedMarkers.Enqueue(marker);
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

        // Hold a distinct head URI at sequence 1 so later same-URI floods stay pending.
        // (A same-URI head would coalesce into the flood slot and is covered by sequence-aware tests.)
        var held = ResourceUpdated("debug://gate", marker: "held");
        orderedUpstream.StampReceivedMessage(held);

        var coalescedTasks = new List<Task>(1000);
        for (var i = 1; i <= 1000; i++)
        {
            var notification = ResourceUpdated(StateUri, marker: i.ToString());
            orderedUpstream.StampReceivedMessage(notification);
            // Callback runs before sequence 1, so drain cannot forward yet.
            coalescedTasks.Add(callback(notification, CancellationToken.None).AsTask());
        }

        // Coalesced duplicates complete immediately — no per-message waiters.
        Assert.All(coalescedTasks, task => Assert.True(task.IsCompletedSuccessfully));
        // 1000 same-URI updates while the predecessor is missing: one pending slot only.
        Assert.Equal(1, orderedUpstream.PendingUriCount);
        Assert.Equal(1, orderedUpstream.PendingSlotCount);
        Assert.True(orderedUpstream.PendingUriCount <= ResourceUpdatesRelay.MaxPendingUris);
        // Retained objects stay O(unique URIs / gap intervals), not O(messages).
        Assert.True(
            orderedUpstream.RetainedBackpressureObjectCount < 16,
            $"retained objects grew with message count: {orderedUpstream.RetainedBackpressureObjectCount}");
        Assert.True(
            orderedUpstream.AccountedIntervalCount <= 2,
            $"accounted intervals not coalesced: {orderedUpstream.AccountedIntervalCount}");

        var breakpoints = ResourceUpdated(BreakpointsUri, marker: "bp");
        orderedUpstream.StampReceivedMessage(breakpoints);
        await callback(breakpoints, CancellationToken.None);
        Assert.Equal(2, orderedUpstream.PendingUriCount);
        Assert.Equal(2, orderedUpstream.PendingSlotCount);
        Assert.True(orderedUpstream.RetainedBackpressureObjectCount < 16);

        // Release the held predecessor: ordered drain must complete and clear retained work.
        await callback(held, CancellationToken.None);
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(15));
        await WaitForAsync(
            () => orderedUpstream.PendingUriCount == 0 && orderedUpstream.ForwardAttempts >= 3,
            TimeSpan.FromSeconds(10));

        Assert.Equal(0, orderedUpstream.PendingUriCount);
        Assert.Equal(0, orderedUpstream.PendingSlotCount);
        Assert.Equal(3, orderedUpstream.ForwardAttempts);
        // Newest same-URI marker must be the one selected for forward (sequence-aware latest).
        Assert.Contains("1000", orderedUpstream.ForwardedMarkers);
        Assert.Contains("held", orderedUpstream.ForwardedMarkers);
        Assert.Contains("bp", orderedUpstream.ForwardedMarkers);
        // Coalesce means far fewer drain forwards than the 1000+ notifications issued.
        Assert.True(
            orderedUpstream.ForwardAttempts <= 3,
            $"expected coalesced drain forwards, got {orderedUpstream.ForwardAttempts}");
        // Downstream delivery is best-effort; when it lands, markers stay consistent.
        if (!received.IsEmpty)
        {
            Assert.Contains(StateUri, received);
            Assert.Contains("1000", receivedMarkers);
        }

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task Coalesce_IsSequenceAware_LateOlderCallbackCannotOverwriteNewestMarker()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var receivedMarkers = new ConcurrentQueue<string>();

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
                            var marker = notification.Params?["_meta"]?["marker"]?.GetValue<string>();
                            if (marker is not null)
                            {
                                receivedMarkers.Enqueue(marker);
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

        // Gap at sequence 1 so pending state is retained while late callbacks rearrange.
        var heldOther = ResourceUpdated(BreakpointsUri, marker: "hold");
        orderedUpstream.StampReceivedMessage(heldOther);

        var cancelledOlder = ResourceUpdated(StateUri, marker: "cancelled-late");
        var older = ResourceUpdated(StateUri, marker: "older");
        var newer = ResourceUpdated(StateUri, marker: "newest");
        orderedUpstream.StampReceivedMessage(cancelledOlder); // seq 2
        orderedUpstream.StampReceivedMessage(older); // seq 3
        orderedUpstream.StampReceivedMessage(newer); // seq 4

        // Newest registers first; later older/cancelled callbacks arrive out of order.
        await callback(newer, CancellationToken.None);
        Assert.Equal(1, orderedUpstream.PendingUriCount);

        await callback(older, CancellationToken.None);
        using (var cancelled = new CancellationTokenSource())
        {
            cancelled.Cancel();
            await callback(cancelledOlder, cancelled.Token);
        }

        // Still one pending URI; late older/cancelled must not suppress or replace newest.
        Assert.Equal(1, orderedUpstream.PendingUriCount);

        await callback(heldOther, CancellationToken.None);
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        await WaitForAsync(() => orderedUpstream.ForwardAttempts >= 2, TimeSpan.FromSeconds(10));

        Assert.Contains("hold", orderedUpstream.ForwardedMarkers);
        Assert.Contains("newest", orderedUpstream.ForwardedMarkers);
        Assert.DoesNotContain("older", orderedUpstream.ForwardedMarkers);
        Assert.DoesNotContain("cancelled-late", orderedUpstream.ForwardedMarkers);
        // Only the newest state marker is selected for debug://state.
        Assert.Equal(
            1,
            orderedUpstream.ForwardedMarkers.Count(m => m is "older" or "newest" or "cancelled-late"));

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task MissingUri_IsRejectedWithoutEnteringPendingState()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var client = await McpClient.CreateAsync(downstream.CreateClientTransport());
        await session.DownstreamReady;

        var orderedUpstream = ResourceUpdatesRelay.CreateOrderedUpstream();
        var handlers = new McpClientHandlers();
        orderedUpstream.ConfigureHandlers(handlers, session);
        var callback = Assert.Single(handlers.NotificationHandlers!).Value;

        var missingUri = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"_meta":{"marker":"no-uri"}}"""),
        };
        var emptyUri = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":"","_meta":{"marker":"empty"}}"""),
        };
        var nonStringUri = new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"uri":123,"_meta":{"marker":"num"}}"""),
        };

        // Stamp must not accept malformed URIs into the sequenced pipeline.
        orderedUpstream.StampReceivedMessage(missingUri);
        orderedUpstream.StampReceivedMessage(emptyUri);
        orderedUpstream.StampReceivedMessage(nonStringUri);
        Assert.Equal(0, orderedUpstream.PendingUriCount);
        Assert.Equal(0, orderedUpstream.PendingSlotCount);

        await Assert.ThrowsAsync<InvalidOperationException>(
            async () => await callback(missingUri, CancellationToken.None));
        await Assert.ThrowsAsync<InvalidOperationException>(
            async () => await callback(emptyUri, CancellationToken.None));
        await Assert.ThrowsAsync<InvalidOperationException>(
            async () => await callback(nonStringUri, CancellationToken.None));
        Assert.Equal(0, orderedUpstream.PendingUriCount);
        Assert.Equal(0, orderedUpstream.RetainedBackpressureObjectCount);

        // A subsequent valid update still drains normally (no sequence hole from rejects).
        var valid = ResourceUpdated(StateUri, marker: "after-reject");
        orderedUpstream.StampReceivedMessage(valid);
        await callback(valid, CancellationToken.None);
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(0, orderedUpstream.PendingUriCount);

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task DistinctUriBound_RejectionDoesNotStrandLaterValidUpdates()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        var received = new ConcurrentQueue<string>();
        var receivedMarkers = new ConcurrentQueue<string>();

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
                            received.Enqueue(notification.Params!["uri"]!.GetValue<string>());
                            var marker = notification.Params?["_meta"]?["marker"]?.GetValue<string>();
                            if (marker is not null)
                            {
                                receivedMarkers.Enqueue(marker);
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

        // Stamp head first but leave its callback unregistered so 64 later URIs stay pending.
        var held = ResourceUpdated("debug://held", marker: "held");
        orderedUpstream.StampReceivedMessage(held);

        for (var i = 0; i < ResourceUpdatesRelay.MaxPendingUris; i++)
        {
            var notification = ResourceUpdated($"debug://uri-{i}", marker: $"m{i}");
            orderedUpstream.StampReceivedMessage(notification);
            await callback(notification, CancellationToken.None);
        }

        Assert.Equal(ResourceUpdatesRelay.MaxPendingUris, orderedUpstream.PendingUriCount);

        // 65th distinct URI beyond the bound must fail closed without stranding drain.
        var overflow = ResourceUpdated("debug://overflow", marker: "overflow");
        orderedUpstream.StampReceivedMessage(overflow);
        var overflowError = await Assert.ThrowsAsync<InvalidOperationException>(
            async () => await callback(overflow, CancellationToken.None));
        Assert.Contains("pending-URI bound exceeded", overflowError.Message);
        Assert.Equal(ResourceUpdatesRelay.MaxPendingUris, orderedUpstream.PendingUriCount);

        // Progress after bound rejection: coalesce into an existing pending URI.
        var progress = ResourceUpdated("debug://uri-0", marker: "progress-after-overflow");
        orderedUpstream.StampReceivedMessage(progress);
        await callback(progress, CancellationToken.None);
        Assert.Equal(ResourceUpdatesRelay.MaxPendingUris, orderedUpstream.PendingUriCount);

        // Head-gap admission unblocks ordered drain even though the queue was at the bound.
        await callback(held, CancellationToken.None);
        await orderedUpstream.WaitForDrainAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(20));
        await WaitForAsync(
            () => orderedUpstream.PendingUriCount == 0
                && orderedUpstream.ForwardAttempts >= ResourceUpdatesRelay.MaxPendingUris + 1,
            TimeSpan.FromSeconds(15));

        Assert.Equal(0, orderedUpstream.PendingUriCount);
        Assert.Equal(ResourceUpdatesRelay.MaxPendingUris + 1, orderedUpstream.ForwardAttempts);
        Assert.Contains("held", orderedUpstream.ForwardedMarkers);
        Assert.Contains("progress-after-overflow", orderedUpstream.ForwardedMarkers);
        Assert.DoesNotContain("overflow", orderedUpstream.ForwardedMarkers);
        // Bound rejection must not strand the full drain (head + 64 accepted URIs).
        Assert.Equal(
            ResourceUpdatesRelay.MaxPendingUris + 1,
            orderedUpstream.ForwardedMarkers.Count);

        await client.DisposeAsync();
        await session.DisposeAsync();
    }

    private static JsonRpcNotification ResourceUpdated(string uri, string marker) =>
        new()
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = new JsonObject
            {
                ["uri"] = uri,
                ["_meta"] = new JsonObject { ["marker"] = marker },
            },
        };

    private static async Task WaitForAsync(Func<bool> condition, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (!condition())
        {
            if (DateTime.UtcNow >= deadline)
            {
                throw new TimeoutException("Condition was not met before timeout.");
            }

            await Task.Delay(10);
        }
    }
}

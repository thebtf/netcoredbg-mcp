using System.Collections.Concurrent;
using System.Reflection;
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

    private sealed class ResourceUpdateWriteGate
    {
        private readonly TaskCompletionSource _blockedWriteStarted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _release =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void Configure(McpServerOptions options) =>
            options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcNotification
                    {
                        Method: NotificationMethods.ResourceUpdatedNotification,
                    })
                {
                    _blockedWriteStarted.TrySetResult();
                    await _release.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            });

        public void Release() => _release.TrySetResult();

        public Task WaitUntilBlockedAsync(TimeSpan timeout) =>
            _blockedWriteStarted.Task.WaitAsync(timeout);
    }
    private sealed class TerminalWriteGate(RequestId terminalId)
    {
        private readonly TaskCompletionSource _resourceWriteStarted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _terminalWriteStarted =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _releaseResource =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource _releaseTerminal =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _resourceWriteClaimed;
        private int _terminalWriteClaimed;

        public void Configure(McpServerOptions options) =>
            options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcNotification
                    {
                        Method: NotificationMethods.ResourceUpdatedNotification,
                    }
                    && Interlocked.CompareExchange(ref _resourceWriteClaimed, 1, 0) == 0)
                {
                    _resourceWriteStarted.TrySetResult();
                    await _releaseResource.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                }

                var responseId = context.JsonRpcMessage switch
                {
                    JsonRpcResponse response => response.Id,
                    JsonRpcError error => error.Id,
                    _ => (RequestId?)null,
                };
                if (responseId is { } id
                    && id.Equals(terminalId)
                    && Interlocked.CompareExchange(ref _terminalWriteClaimed, 1, 0) == 0)
                {
                    _terminalWriteStarted.TrySetResult();
                    await _releaseTerminal.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            });

        public Task WaitUntilResourceBlockedAsync(TimeSpan timeout) =>
            _resourceWriteStarted.Task.WaitAsync(timeout);

        public Task WaitUntilTerminalBlockedAsync(TimeSpan timeout) =>
            _terminalWriteStarted.Task.WaitAsync(timeout);

        public void ReleaseResource() => _releaseResource.TrySetResult();

        public void ReleaseTerminal() => _releaseTerminal.TrySetResult();
    }


    private sealed class ReadObserverClientTransport(
        IClientTransport inner,
        Action<JsonRpcMessage> observe) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default)
        {
            var transport = await inner.ConnectAsync(cancellationToken).ConfigureAwait(false);
            return new ReadObserverTransport(transport, observe);
        }

        private sealed class ReadObserverTransport : ITransport
        {
            private readonly ITransport _inner;

            public ReadObserverTransport(ITransport inner, Action<JsonRpcMessage> observe)
            {
                _inner = inner;
                MessageReader = new ReadObserverChannelReader(inner.MessageReader, observe);
            }

            public string? SessionId => _inner.SessionId;

            public ChannelReader<JsonRpcMessage> MessageReader { get; }

            public Task SendMessageAsync(
                JsonRpcMessage message,
                CancellationToken cancellationToken = default) =>
                _inner.SendMessageAsync(message, cancellationToken);

            public ValueTask DisposeAsync() => _inner.DisposeAsync();
        }

        private sealed class ReadObserverChannelReader(
            ChannelReader<JsonRpcMessage> inner,
            Action<JsonRpcMessage> observe) : ChannelReader<JsonRpcMessage>
        {
            public override Task Completion => inner.Completion;

            public override bool TryRead(out JsonRpcMessage item)
            {
                if (!inner.TryRead(out item!))
                {
                    return false;
                }

                observe(item);
                return true;
            }

            public override ValueTask<bool> WaitToReadAsync(
                CancellationToken cancellationToken = default) =>
                inner.WaitToReadAsync(cancellationToken);
        }
    }

    private sealed class SendObserverClientTransport(
        IClientTransport inner,
        Action<JsonRpcMessage> observe) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default)
        {
            var transport = await inner.ConnectAsync(cancellationToken).ConfigureAwait(false);
            return new SendObserverTransport(transport, observe);
        }

        private sealed class SendObserverTransport(
            ITransport inner,
            Action<JsonRpcMessage> observe) : ITransport
        {
            public string? SessionId => inner.SessionId;

            public ChannelReader<JsonRpcMessage> MessageReader => inner.MessageReader;

            public Task SendMessageAsync(
                JsonRpcMessage message,
                CancellationToken cancellationToken = default)
            {
                observe(message);
                return inner.SendMessageAsync(message, cancellationToken);
            }

            public ValueTask DisposeAsync() => inner.DisposeAsync();
        }
    }

    private static McpServerOptions SubscribablePythonOptions(
        McpRequestHandler<SubscribeRequestParams, EmptyResult>? subscribe = null,
        McpRequestHandler<UnsubscribeRequestParams, EmptyResult>? unsubscribe = null,
        McpRequestHandler<CallToolRequestParams, CallToolResult>? callTool = null) =>
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
                CallToolHandler = callTool ?? ((context, ct) => ValueTask.FromResult(new CallToolResult())),
                SubscribeToResourcesHandler = subscribe ?? ((context, ct) => ValueTask.FromResult(new EmptyResult())),
                UnsubscribeFromResourcesHandler = unsubscribe ?? ((context, ct) => ValueTask.FromResult(new EmptyResult())),
            },
        };
    private static McpServerOptions ReadAheadPythonOptions(
        bool error,
        TaskCompletionSource tailSent)
    {
        var terminalArmed = 0;
        var options = SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
        {
            await context.Server.SendMessageAsync(
                ResourceUpdated(StateUri, marker: "u1"),
                cancellationToken).ConfigureAwait(false);
            Volatile.Write(ref terminalArmed, 1);

            if (error)
            {
                throw new McpProtocolException("read-ahead failure", McpErrorCode.InvalidParams);
            }

            return new CallToolResult
            {
                Content = [new TextContentBlock { Text = "mutation-complete" }],
            };
        });
        options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            var appendTail = Volatile.Read(ref terminalArmed) != 0
                && context.JsonRpcMessage is JsonRpcResponse or JsonRpcError;
            if (appendTail)
            {
                Interlocked.Exchange(ref terminalArmed, 0);
            }

            await next(context, cancellationToken).ConfigureAwait(false);
            if (appendTail)
            {
                await context.Server.SendMessageAsync(
                    ResourceUpdated(StateUri, marker: "u2"),
                    cancellationToken).ConfigureAwait(false);
                tailSent.TrySetResult();
            }
        });
        return options;
    }

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

    private static JsonRpcRequest ToolRequest(RequestId requestId, string name = "mutate") =>
        new()
        {
            Id = requestId,
            Method = RequestMethods.ToolsCall,
            Params = JsonSerializer.SerializeToNode(
                new CallToolRequestParams { Name = name },
                McpJsonUtilities.DefaultOptions),
        };

    private static async Task<ITransport> ConnectRawDownstreamAsync(
        DuplexChannel downstream,
        bool sendInitialized = true)
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
        if (sendInitialized)
        {
            await transport.SendMessageAsync(new JsonRpcNotification
            {
                Method = NotificationMethods.InitializedNotification,
            });
        }

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
    public async Task ResourceUpdatedNotification_BlocksFollowingToolResponseUntilUpdateIsDelivered()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var updateWriteGate = new ResourceUpdateWriteGate();
        await using var session = ResourcesTestComposition.CreateSession(
            upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: updateWriteGate.Configure);
        _ = host.RunAsync();

        FakePythonServer? python = null;
        var options = SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
        {
            await RelaySession.ForwardNotificationAsync(
                python!.Server,
                ResourceUpdated(StateUri, marker: "before-response"),
                cancellationToken);
            return new CallToolResult
            {
                Content = [new TextContentBlock { Text = "mutation-complete" }],
            };
        });
        await using var fakePython = FakePythonServer.Start(upstream, options);
        python = fakePython;
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        var requestId = new RequestId(2);
        await rawDownstream.SendMessageAsync(
            new JsonRpcRequest
            {
                Id = requestId,
                Method = RequestMethods.ToolsCall,
                Params = JsonSerializer.SerializeToNode(
                    new CallToolRequestParams { Name = "mutate" },
                    McpJsonUtilities.DefaultOptions),
            });
        var firstMessageTask = rawDownstream.MessageReader.ReadAsync().AsTask();

        try
        {
            await updateWriteGate.WaitUntilBlockedAsync(TimeSpan.FromSeconds(10));
            var escaped = await Task.WhenAny(firstMessageTask, Task.Delay(TimeSpan.FromSeconds(1)));
            Assert.NotSame(
                firstMessageTask,
                escaped);
        }
        finally
        {
            updateWriteGate.Release();
        }

        var firstMessage = await firstMessageTask.WaitAsync(TimeSpan.FromSeconds(10));
        var update = Assert.IsType<JsonRpcNotification>(firstMessage);
        Assert.Equal(NotificationMethods.ResourceUpdatedNotification, update.Method);
        Assert.Equal(StateUri, update.Params!["uri"]!.GetValue<string>());
        var response = Assert.IsType<JsonRpcResponse>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(requestId, response.Id);

        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }
    [Fact]
    public async Task ResourceUpdatedReadAhead_ExactTypedResponseFenceIgnoresUnrelatedLocalResponse()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var requestId = new RequestId(2L);
        var unrelatedId = new RequestId("2");
        var writeGate = new TerminalWriteGate(requestId);
        var tailSent = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: writeGate.Configure);
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            ReadAheadPythonOptions(error: false, tailSent));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        await rawDownstream.SendMessageAsync(ToolRequest(requestId));
        await tailSent.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await writeGate.WaitUntilResourceBlockedAsync(TimeSpan.FromSeconds(10));
        writeGate.ReleaseResource();

        var first = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u1", first.Params?["_meta"]?["marker"]?.GetValue<string>());
        await writeGate.WaitUntilTerminalBlockedAsync(TimeSpan.FromSeconds(10));

        await rawDownstream.SendMessageAsync(new JsonRpcRequest
        {
            Id = unrelatedId,
            Method = RequestMethods.PromptsList,
            Params = new JsonObject(),
        });
        var unrelated = Assert.IsAssignableFrom<JsonRpcMessageWithId>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(unrelatedId, unrelated.Id);

        var terminalRead = rawDownstream.MessageReader.ReadAsync().AsTask();
        Assert.NotSame(
            terminalRead,
            await Task.WhenAny(terminalRead, Task.Delay(TimeSpan.FromMilliseconds(250))));
        writeGate.ReleaseTerminal();

        var response = Assert.IsType<JsonRpcResponse>(
            await terminalRead.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(requestId, response.Id);
        var tail = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u2", tail.Params?["_meta"]?["marker"]?.GetValue<string>());

        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task DuplicateHistoricalDownstreamId_FailsClosedBeforeLocalHandlerAndWireTail()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var requestId = new RequestId("duplicate-history");
        var writeGate = new TerminalWriteGate(requestId);
        var tailSent = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var duplicateHandlerEntered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: options =>
            {
                writeGate.Configure(options);
                options.Capabilities!.Prompts = new PromptsCapability { ListChanged = false };
                options.Handlers.ListPromptsHandler = (_, _) =>
                {
                    duplicateHandlerEntered.TrySetResult();
                    return ValueTask.FromResult(new ListPromptsResult { Prompts = [] });
                };
            });
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            ReadAheadPythonOptions(error: false, tailSent));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        try
        {
            await rawDownstream.SendMessageAsync(ToolRequest(requestId, "A"));
            await tailSent.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await writeGate.WaitUntilResourceBlockedAsync(TimeSpan.FromSeconds(10));
            writeGate.ReleaseResource();

            var first = Assert.IsType<JsonRpcNotification>(
                await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.Equal("u1", first.Params?["_meta"]?["marker"]?.GetValue<string>());
            await writeGate.WaitUntilTerminalBlockedAsync(TimeSpan.FromSeconds(10));

            var sessionEnding = Task.Delay(Timeout.Infinite, session.SessionEndingToken);
            await rawDownstream.SendMessageAsync(new JsonRpcRequest
            {
                Id = requestId,
                Method = RequestMethods.PromptsList,
                Params = new JsonObject(),
            });

            var firstOutcome = await Task.WhenAny(
                sessionEnding,
                duplicateHandlerEntered.Task,
                Task.Delay(TimeSpan.FromSeconds(10)));
            Assert.Same(sessionEnding, firstOutcome);
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => sessionEnding);
            Assert.False(duplicateHandlerEntered.Task.IsCompleted);
            Assert.Contains(
                "Duplicate downstream request ID",
                Assert.IsType<InvalidOperationException>(session.ForwardingFailure).Message,
                StringComparison.Ordinal);

            await WaitForAsync(
                () => session.RetainedDownstreamForwardLegCount == 0
                    && session.RetainedUpstreamForwardLegCount == 0,
                TimeSpan.FromSeconds(10));

            writeGate.ReleaseTerminal();

            using var tailProbe = new CancellationTokenSource(TimeSpan.FromMilliseconds(250));
            var originalTerminalSeen = false;
            try
            {
                while (true)
                {
                    var message = await rawDownstream.MessageReader.ReadAsync(tailProbe.Token);
                    if (message is JsonRpcMessageWithId terminal && terminal.Id.Equals(requestId))
                    {
                        Assert.IsType<JsonRpcResponse>(terminal);
                        Assert.False(originalTerminalSeen, "More than one terminal crossed for the duplicated ID.");
                        originalTerminalSeen = true;
                    }
                    Assert.False(
                        message is JsonRpcNotification notification
                            && notification.Params?["_meta"]?["marker"]?.GetValue<string>() == "u2",
                        "Wire-later U2 crossed after duplicate-ID failure.");
                }
            }
            catch (OperationCanceledException) when (tailProbe.IsCancellationRequested)
            {
                // No duplicate terminal or U2 crossed during the bounded fail-closed observation window.
            }
            catch (ChannelClosedException)
            {
                // Session shutdown closed the wire before a duplicate terminal or U2 could cross.
            }
            Assert.False(duplicateHandlerEntered.Task.IsCompleted);
        }
        finally
        {
            writeGate.ReleaseTerminal();
            await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
        }
    }

    [Fact]
    public async Task ResourceUpdatedReadAhead_ErrorFenceCompletesOnlyAfterExactErrorSend()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var requestId = new RequestId("error-request");
        var writeGate = new TerminalWriteGate(requestId);
        var tailSent = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: writeGate.Configure);
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            ReadAheadPythonOptions(error: true, tailSent));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        await rawDownstream.SendMessageAsync(ToolRequest(requestId));
        await tailSent.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await writeGate.WaitUntilResourceBlockedAsync(TimeSpan.FromSeconds(10));
        writeGate.ReleaseResource();

        var first = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u1", first.Params?["_meta"]?["marker"]?.GetValue<string>());
        await writeGate.WaitUntilTerminalBlockedAsync(TimeSpan.FromSeconds(10));

        var terminalRead = rawDownstream.MessageReader.ReadAsync().AsTask();
        Assert.NotSame(
            terminalRead,
            await Task.WhenAny(terminalRead, Task.Delay(TimeSpan.FromMilliseconds(250))));
        writeGate.ReleaseTerminal();

        var error = Assert.IsType<JsonRpcError>(
            await terminalRead.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(requestId, error.Id);
        Assert.Equal((int)McpErrorCode.InvalidParams, error.Error.Code);
        var tail = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u2", tail.Params?["_meta"]?["marker"]?.GetValue<string>());

        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ResourceUpdatedReadAhead_CancellationAfterUpstreamTerminalDoesNotWaitForPhantomResponse()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var requestId = new RequestId("cancel-request");
        var writeGate = new TerminalWriteGate(requestId);
        var tailSent = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: writeGate.Configure);
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            ReadAheadPythonOptions(error: false, tailSent));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        await rawDownstream.SendMessageAsync(ToolRequest(requestId));
        await tailSent.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await writeGate.WaitUntilResourceBlockedAsync(TimeSpan.FromSeconds(10));
        writeGate.ReleaseResource();

        var first = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u1", first.Params?["_meta"]?["marker"]?.GetValue<string>());
        await writeGate.WaitUntilTerminalBlockedAsync(TimeSpan.FromSeconds(10));
        await rawDownstream.SendMessageAsync(new JsonRpcNotification
        {
            Method = NotificationMethods.CancelledNotification,
            Params = JsonSerializer.SerializeToNode(
                new CancelledNotificationParams { RequestId = requestId },
                McpJsonUtilities.DefaultOptions),
        });

        var tail = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal("u2", tail.Params?["_meta"]?["marker"]?.GetValue<string>());
        var phantom = rawDownstream.MessageReader.ReadAsync().AsTask();
        Assert.NotSame(
            phantom,
            await Task.WhenAny(phantom, Task.Delay(TimeSpan.FromMilliseconds(250))));

        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task CorrelationMarker_BindsAssignedUpstreamIdForNormalAndNullParamsWithoutWireMetadata()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var sentRequests = new ConcurrentQueue<JsonRpcRequest>();
        var wireItemCounts = new ConcurrentQueue<int>();
        var options = SubscribablePythonOptions(callTool: (context, cancellationToken) =>
        {
            wireItemCounts.Enqueue(context.JsonRpcRequest.Context?.Items?.Count ?? 0);
            return ValueTask.FromResult(new CallToolResult
            {
                Content = [new TextContentBlock { Text = "ok" }],
            });
        });
        options.Handlers.ListToolsHandler = (context, cancellationToken) =>
        {
            wireItemCounts.Enqueue(context.JsonRpcRequest.Context?.Items?.Count ?? 0);
            return ValueTask.FromResult(new ListToolsResult { Tools = [] });
        };

        await using var session = ResourcesTestComposition.CreateSession(
            () => new SendObserverClientTransport(
                upstream.CreateClientTransport(),
                message =>
                {
                    if (message is JsonRpcRequest
                        {
                            Method: RequestMethods.ToolsList or RequestMethods.ToolsCall,
                        } request)
                    {
                        sentRequests.Enqueue(request);
                    }
                }));
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, options);
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);

        var listId = new RequestId(20L);
        await rawDownstream.SendMessageAsync(new JsonRpcRequest
        {
            Id = listId,
            Method = RequestMethods.ToolsList,
        });
        Assert.Equal(
            listId,
            Assert.IsType<JsonRpcResponse>(
                await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10))).Id);

        var callId = new RequestId("20");
        await rawDownstream.SendMessageAsync(ToolRequest(callId));
        Assert.Equal(
            callId,
            Assert.IsType<JsonRpcResponse>(
                await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10))).Id);

        var captured = sentRequests.ToArray();
        Assert.Equal(2, captured.Length);
        Assert.NotEqual(captured[0].Id, captured[1].Id);
        Assert.IsType<JsonObject>(captured.Single(request => request.Method == RequestMethods.ToolsList).Params);
        Assert.Empty(captured.Single(request => request.Method == RequestMethods.ToolsList).Params!.AsObject());
        Assert.All(captured, request =>
        {
            Assert.Null(request.Context?.RelatedTransport);
            var marker = Assert.Single(Assert.IsAssignableFrom<IDictionary<string, object?>>(request.Context?.Items));
            Assert.IsType<RelaySession.ForwardLeg>(marker.Value);
        });
        Assert.Equal([0, 0], wireItemCounts);
        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ConcurrentCalls_OutOfOrderUpstreamTerminalsKeepExactPerLegFences()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var firstCallStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseFirstCall = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var terminalIndex = 0;
        var options = SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
        {
            if (context.Params?.Name == "A")
            {
                firstCallStarted.TrySetResult();
                await releaseFirstCall.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                return new CallToolResult { Content = [new TextContentBlock { Text = "A" }] };
            }

            await context.Server.SendMessageAsync(
                ResourceUpdated(StateUri, "u1"),
                cancellationToken).ConfigureAwait(false);
            return new CallToolResult { Content = [new TextContentBlock { Text = "B" }] };
        });
        options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            if (context.JsonRpcMessage is not JsonRpcResponse { Result: JsonObject result }
                || !result.ContainsKey("content"))
            {
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            var index = Interlocked.Increment(ref terminalIndex);
            await next(context, cancellationToken).ConfigureAwait(false);
            if (index == 1)
            {
                await context.Server.SendMessageAsync(
                    ResourceUpdated(StateUri, "u2"),
                    cancellationToken).ConfigureAwait(false);
                releaseFirstCall.TrySetResult();
            }
            else
            {
                await context.Server.SendMessageAsync(
                    ResourceUpdated(StateUri, "u3"),
                    cancellationToken).ConfigureAwait(false);
            }
        });

        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, options);
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);
        var firstId = new RequestId(30L);
        var secondId = new RequestId("30");

        await rawDownstream.SendMessageAsync(ToolRequest(firstId, "A"));
        await firstCallStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await rawDownstream.SendMessageAsync(ToolRequest(secondId, "B"));

        var u1 = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var secondResponse = Assert.IsType<JsonRpcResponse>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var u2 = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var firstResponse = Assert.IsType<JsonRpcResponse>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var u3 = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));

        Assert.Equal("u1", u1.Params?["_meta"]?["marker"]?.GetValue<string>());
        Assert.Equal(secondId, secondResponse.Id);
        Assert.Equal("u2", u2.Params?["_meta"]?["marker"]?.GetValue<string>());
        Assert.Equal(firstId, firstResponse.Id);
        Assert.Equal("u3", u3.Params?["_meta"]?["marker"]?.GetValue<string>());
        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task CancelledLeg_StaleSignalsPreserveDistinctSuccessorAndOldIdReuseFailsClosed()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var upstreamRequestIds = new ConcurrentQueue<RequestId>();
        var firstStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var secondStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var successorCancelled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var thirdStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseSecond = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var invocation = 0;
        var options = SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
        {
            switch (Interlocked.Increment(ref invocation))
            {
                case 1:
                    firstStarted.TrySetResult();
                    await Task.Delay(Timeout.Infinite, cancellationToken).ConfigureAwait(false);
                    throw new InvalidOperationException("Unreachable after infinite delay.");

                case 2:
                    using (cancellationToken.Register(() => successorCancelled.TrySetResult()))
                    {
                        secondStarted.TrySetResult();
                        await releaseSecond.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                        return new CallToolResult { Content = [new TextContentBlock { Text = "new" }] };
                    }

                default:
                    thirdStarted.TrySetResult();
                    return new CallToolResult { Content = [new TextContentBlock { Text = "third" }] };
            }
        });
        await using var session = ResourcesTestComposition.CreateSession(
            () => new SendObserverClientTransport(
                upstream.CreateClientTransport(),
                message =>
                {
                    if (message is JsonRpcRequest { Method: RequestMethods.ToolsCall } request)
                    {
                        upstreamRequestIds.Enqueue(request.Id);
                    }
                }));
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, options);
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);
        var oldDownstreamId = new RequestId("cancelled-old");
        var successorDownstreamId = new RequestId("distinct-successor");

        try
        {
            await rawDownstream.SendMessageAsync(ToolRequest(oldDownstreamId, "old"));
            await firstStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await rawDownstream.SendMessageAsync(new JsonRpcNotification
            {
                Method = NotificationMethods.CancelledNotification,
                Params = JsonSerializer.SerializeToNode(
                    new CancelledNotificationParams { RequestId = oldDownstreamId },
                    McpJsonUtilities.DefaultOptions),
            });
            await WaitForAsync(
                () => session.RetainedDownstreamForwardLegCount == 0
                    && session.RetainedUpstreamForwardLegCount == 0,
                TimeSpan.FromSeconds(10));

            await rawDownstream.SendMessageAsync(ToolRequest(successorDownstreamId, "new"));
            await secondStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await WaitForAsync(() => upstreamRequestIds.Count == 2, TimeSpan.FromSeconds(10));
            var assignedIds = upstreamRequestIds.ToArray();
            Assert.NotEqual(assignedIds[0], assignedIds[1]);
            Assert.Equal(1, session.RetainedDownstreamForwardLegCount);
            Assert.Equal(1, session.RetainedUpstreamForwardLegCount);

            await rawDownstream.SendMessageAsync(new JsonRpcNotification
            {
                Method = NotificationMethods.CancelledNotification,
                Params = JsonSerializer.SerializeToNode(
                    new CancelledNotificationParams { RequestId = oldDownstreamId },
                    McpJsonUtilities.DefaultOptions),
            });
            var cancellationBarrierId = new RequestId("stale-cancellation-barrier");
            await rawDownstream.SendMessageAsync(new JsonRpcRequest
            {
                Id = cancellationBarrierId,
                Method = RequestMethods.PromptsList,
                Params = new JsonObject(),
            });
            while (true)
            {
                var message = await rawDownstream.MessageReader.ReadAsync().AsTask()
                    .WaitAsync(TimeSpan.FromSeconds(10));
                if (message is JsonRpcMessageWithId withId && withId.Id.Equals(cancellationBarrierId))
                {
                    break;
                }
            }

            Assert.False(successorCancelled.Task.IsCompleted);
            Assert.Equal(1, session.RetainedDownstreamForwardLegCount);
            Assert.Equal(1, session.RetainedUpstreamForwardLegCount);

            await fakePython.Server.SendMessageAsync(new JsonRpcResponse
            {
                Id = assignedIds[0],
                Result = JsonSerializer.SerializeToNode(
                    new CallToolResult { Content = [new TextContentBlock { Text = "stale" }] },
                    McpJsonUtilities.DefaultOptions)!,
            });
            await fakePython.Server.SendMessageAsync(ResourceUpdated(StateUri, "after-stale"));

            while (true)
            {
                var message = await rawDownstream.MessageReader.ReadAsync().AsTask()
                    .WaitAsync(TimeSpan.FromSeconds(10));
                if (message is JsonRpcNotification notification
                    && notification.Params?["_meta"]?["marker"]?.GetValue<string>() == "after-stale")
                {
                    break;
                }
            }

            Assert.False(successorCancelled.Task.IsCompleted);
            Assert.Equal(1, session.RetainedDownstreamForwardLegCount);
            Assert.Equal(1, session.RetainedUpstreamForwardLegCount);
            releaseSecond.TrySetResult();

            JsonRpcResponse successor;
            do
            {
                successor = Assert.IsType<JsonRpcResponse>(
                    await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
            }
            while (successor.Result?["content"]?[0]?["text"]?.GetValue<string>() != "new");

            Assert.Equal(successorDownstreamId, successor.Id);
            await WaitForAsync(
                () => session.RetainedDownstreamForwardLegCount == 0
                    && session.RetainedUpstreamForwardLegCount == 0,
                TimeSpan.FromSeconds(10));

            var sessionEnding = Task.Delay(Timeout.Infinite, session.SessionEndingToken);
            await rawDownstream.SendMessageAsync(ToolRequest(oldDownstreamId, "third"));
            var firstOutcome = await Task.WhenAny(
                sessionEnding,
                thirdStarted.Task,
                Task.Delay(TimeSpan.FromSeconds(10)));
            Assert.Same(sessionEnding, firstOutcome);
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => sessionEnding);
            Assert.False(thirdStarted.Task.IsCompleted);
            Assert.Contains(
                "Duplicate downstream request ID",
                Assert.IsType<InvalidOperationException>(session.ForwardingFailure).Message,
                StringComparison.Ordinal);
            await WaitForAsync(
                () => session.RetainedDownstreamForwardLegCount == 0
                    && session.RetainedUpstreamForwardLegCount == 0,
                TimeSpan.FromSeconds(10));
        }
        finally
        {
            releaseSecond.TrySetResult();
            await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
        }
    }

    [Fact]
    public async Task UnifiedUpstreamTransport_PropagatesCompletionAndClearsRetainedLegs()
    {
        var (session, upstream, downstream) = BuildSession();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var client = await McpClient.CreateAsync(downstream.CreateClientTransport());
        await session.DownstreamReady;

        upstream.SimulateServerExit();
        var ended = session.RunUntilSessionEndedAsync(CancellationToken.None);
        var error = await Assert.ThrowsAnyAsync<Exception>(
            () => ended.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Contains("Python backend", error.Message, StringComparison.OrdinalIgnoreCase);
        Assert.Contains(
            "upstream transport",
            Assert.IsType<IOException>(session.ForwardingFailure).Message,
            StringComparison.OrdinalIgnoreCase);

        await client.DisposeAsync();
        await session.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(0, session.RetainedDownstreamForwardLegCount);
        Assert.Equal(0, session.RetainedUpstreamForwardLegCount);
    }

    [Fact]
    public async Task ContiguousResourceSegment_CoalescesLatestRawPayloadWithinUnifiedBound()
    {
        const int updateCount = 1000;
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var observedAll = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var observedResources = 0;
        await using var session = ResourcesTestComposition.CreateSession(
            () => new ReadObserverClientTransport(
                upstream.CreateClientTransport(),
                message =>
                {
                    if (message is JsonRpcNotification
                        {
                            Method: NotificationMethods.ResourceUpdatedNotification,
                        }
                        && Interlocked.Increment(ref observedResources) == updateCount + 1)
                    {
                        observedAll.TrySetResult();
                    }
                }));
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var rawDownstream = await ConnectRawDownstreamAsync(
            downstream,
            sendInitialized: false);

        JsonRpcNotification latest = null!;
        for (var i = 1; i <= updateCount; i++)
        {
            latest = i == updateCount
                ? new JsonRpcNotification
                {
                    Method = NotificationMethods.ResourceUpdatedNotification,
                    Params = JsonNode.Parse(
                        """{"uri":"debug://state","_meta":{"marker":"1000","opaque":{"keep":true}}}"""),
                }
                : ResourceUpdated(StateUri, i.ToString());
            await fakePython.Server.SendMessageAsync(latest);
        }

        await fakePython.Server.SendMessageAsync(ResourceUpdated(BreakpointsUri, "bp"));
        await observedAll.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await rawDownstream.SendMessageAsync(new JsonRpcNotification
        {
            Method = NotificationMethods.InitializedNotification,
        });

        var state = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        var breakpoints = Assert.IsType<JsonRpcNotification>(
            await rawDownstream.MessageReader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(StateUri, state.Params?["uri"]?.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(latest.Params, state.Params));
        Assert.Equal(BreakpointsUri, breakpoints.Params?["uri"]?.GetValue<string>());

        var extra = rawDownstream.MessageReader.ReadAsync().AsTask();
        Assert.NotSame(extra, await Task.WhenAny(extra, Task.Delay(TimeSpan.FromMilliseconds(250))));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task MalformedResourceUpdate_FailsClosedAndClearsRetainedState()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);
        var ended = session.RunUntilSessionEndedAsync(CancellationToken.None);

        await fakePython.Server.SendMessageAsync(new JsonRpcNotification
        {
            Method = NotificationMethods.ResourceUpdatedNotification,
            Params = JsonNode.Parse("""{"_meta":{"marker":"missing-uri"}}"""),
        });

        _ = await Assert.ThrowsAnyAsync<Exception>(
            () => ended.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Contains(
            "params.uri",
            Assert.IsType<InvalidOperationException>(session.ForwardingFailure).Message,
            StringComparison.Ordinal);
        Assert.Equal(0, session.RetainedDownstreamForwardLegCount);
        Assert.Equal(0, session.RetainedUpstreamForwardLegCount);
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ResourceSegmentBound_FailsClosedWithoutUnboundedRetention()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(upstream, SubscribablePythonOptions());
        await using var rawDownstream = await ConnectRawDownstreamAsync(
            downstream,
            sendInitialized: false);
        var ended = session.RunUntilSessionEndedAsync(CancellationToken.None);

        for (var i = 0; i <= ProgressLoggingRelay.MaxPendingMessages; i++)
        {
            await fakePython.Server.SendMessageAsync(
                ResourceUpdated($"debug://uri-{i}", $"m{i}"));
        }

        _ = await Assert.ThrowsAnyAsync<Exception>(
            () => ended.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Contains(
            "64-URI bound",
            Assert.IsType<InvalidOperationException>(session.ForwardingFailure).Message,
            StringComparison.Ordinal);
        Assert.Equal(0, session.RetainedDownstreamForwardLegCount);
        Assert.Equal(0, session.RetainedUpstreamForwardLegCount);
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task ResourceSendTimeout_FailsClosedBeforeResponseAndClearsRetainedState()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var writeGate = new ResourceUpdateWriteGate();
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream),
            configureOptions: writeGate.Configure);
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
            {
                await context.Server.SendMessageAsync(
                    ResourceUpdated(StateUri, "timeout"),
                    cancellationToken);
                return new CallToolResult
                {
                    Content = [new TextContentBlock { Text = "must-not-cross" }],
                };
            }));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);
        var ended = session.RunUntilSessionEndedAsync(CancellationToken.None);

        await rawDownstream.SendMessageAsync(ToolRequest(new RequestId("timeout-request")));
        await writeGate.WaitUntilBlockedAsync(TimeSpan.FromSeconds(10));
        var escaped = rawDownstream.MessageReader.ReadAsync().AsTask();
        _ = await Assert.ThrowsAnyAsync<Exception>(
            () => ended.WaitAsync(TimeSpan.FromSeconds(8)));
        Assert.Contains(
            "5-second bound",
            Assert.IsType<TimeoutException>(session.ForwardingFailure).Message,
            StringComparison.Ordinal);
        Assert.NotSame(
            escaped,
            await Task.WhenAny(escaped, Task.Delay(TimeSpan.FromMilliseconds(250))));
        Assert.Equal(0, session.RetainedDownstreamForwardLegCount);
        Assert.Equal(0, session.RetainedUpstreamForwardLegCount);
        writeGate.Release();
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task BeginForwardLeg_DoesNotCreateLegAfterDuplicateIdFailureWhileWaitingForGate()
    {
        var upstream = new DuplexChannel();
        var requestId = new RequestId("duplicate-race");
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        session.CheckAddDownstreamRequestId(requestId);

        var forwardLegGate = Assert.IsType<object>(
            typeof(RelaySession)
                .GetField("_forwardLegGate", BindingFlags.Instance | BindingFlags.NonPublic)!
                .GetValue(session));
        var beginForwardLeg = Assert.IsAssignableFrom<MethodInfo>(
            typeof(RelaySession).GetMethod(
                "BeginForwardLeg",
                BindingFlags.Instance | BindingFlags.NonPublic));
        var beginOutcome = new TaskCompletionSource<(object? Leg, Exception? Exception)>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var beginThread = new Thread(() =>
        {
            try
            {
                var leg = beginForwardLeg.Invoke(
                    session,
                    new object?[] { requestId, CancellationToken.None });
                beginOutcome.TrySetResult((leg, null));
            }
            catch (Exception exception)
            {
                beginOutcome.TrySetResult((null, exception));
            }
        })
        {
            IsBackground = true,
        };

        Exception? duplicateFailure = null;
        var blockedOnForwardLegGate = false;
        Monitor.Enter(forwardLegGate);
        try
        {
            beginThread.Start();
            blockedOnForwardLegGate = SpinWait.SpinUntil(
                () => (beginThread.ThreadState & ThreadState.WaitSleepJoin) != 0,
                TimeSpan.FromSeconds(10));
            if (blockedOnForwardLegGate)
            {
                try
                {
                    session.CheckAddDownstreamRequestId(requestId);
                }
                catch (Exception exception)
                {
                    duplicateFailure = exception;
                }
            }
        }
        finally
        {
            Monitor.Exit(forwardLegGate);
        }

        var (leg, beginException) = await beginOutcome.Task.WaitAsync(TimeSpan.FromSeconds(10));
        session.CompleteDownstreamRequestHandling(requestId);
        var retainedDownstream = session.RetainedDownstreamForwardLegCount;
        var retainedUpstream = session.RetainedUpstreamForwardLegCount;
        var legCreatedAfterFailure = leg is not null && beginException is null;

        Assert.True(blockedOnForwardLegGate, "BeginForwardLeg did not block on _forwardLegGate.");
        var duplicate = Assert.IsType<InvalidOperationException>(duplicateFailure);
        Assert.Same(duplicate, session.ForwardingFailure);
        Assert.False(
            legCreatedAfterFailure,
            $"legCreatedAfterFailure={legCreatedAfterFailure}; retainedDownstream={retainedDownstream}; retainedUpstream={retainedUpstream}");
        Assert.Null(leg);
        var invocation = Assert.IsType<TargetInvocationException>(beginException);
        var terminal = Assert.IsType<InvalidOperationException>(invocation.InnerException);
        Assert.Equal("Relay forwarding has terminated.", terminal.Message);
        Assert.Same(duplicate, terminal.InnerException);
        Assert.Equal(0, retainedDownstream);
        Assert.Equal(0, retainedUpstream);
    }

    [Fact]
    public async Task DuplicateLiveDownstreamId_FailsClosedWithoutOverwritingLeg()
    {
        var upstream = new DuplexChannel();
        var downstream = new DuplexChannel();
        var firstStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var session = ResourcesTestComposition.CreateSession(upstream.CreateClientTransport);
        using var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(
                downstream.ServerInputStream,
                downstream.ServerOutputStream));
        _ = host.RunAsync();
        await using var fakePython = FakePythonServer.Start(
            upstream,
            SubscribablePythonOptions(callTool: async (context, cancellationToken) =>
            {
                firstStarted.TrySetResult();
                await Task.Delay(Timeout.Infinite, cancellationToken);
                return new CallToolResult();
            }));
        await using var rawDownstream = await ConnectRawDownstreamAsync(downstream);
        var requestId = new RequestId("duplicate-live");

        await rawDownstream.SendMessageAsync(ToolRequest(requestId));
        await firstStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await rawDownstream.SendMessageAsync(ToolRequest(requestId));

        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await Task.Delay(Timeout.Infinite, session.SessionEndingToken)
                .WaitAsync(TimeSpan.FromSeconds(10)));
        await WaitForAsync(
            () => session.RetainedDownstreamForwardLegCount == 0
                && session.RetainedUpstreamForwardLegCount == 0,
            TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
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

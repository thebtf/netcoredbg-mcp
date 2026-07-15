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

/// <summary>
/// FD-004 tools-family relay proofs that sit beside FD-000's generic seam tests and
/// ProductionCompositionTests' basic forwarding check: the static-catalog claim (no
/// <c>listChanged</c> capability, no forwarded list-changed push), cursor/<c>_meta</c>
/// pass-through on <c>tools/list</c>, forward-direction (downstream <c>tools/call</c>)
/// cancellation propagating to the upstream leg and leaving the session usable afterward,
/// and a genuine upstream protocol error surviving the relay with its
/// <c>McpErrorCode</c>/message intact rather than doubly wrapped. FD-000 only proved
/// cancellation for the reverse (Python -&gt; downstream) direction; the tools family's own
/// forward direction is this slice's job. Every fixture here builds the real production
/// <see cref="RelayComposition.Build"/> output - via <see cref="ToolsRelay.Register"/> -
/// against a real (not mocked) <see cref="FakePythonServer"/> and real
/// <see cref="McpClient"/>/<see cref="McpServer"/> endpoints over in-memory
/// <see cref="DuplexChannel"/>s, exactly like every other FD-000 test in this project. The
/// full 135-tool catalog/schema equality, request/result <c>_meta</c> functional round trip
/// (mux session ownership), <c>structuredContent</c>, unknown-tool, and representative
/// error proofs against the real Python backend live in <c>tests/test_host_proxy.py</c>,
/// which is the load-bearing installed-consumer proof for this slice.
/// </summary>
public sealed class ToolsCatalogContractTests
{
    private static readonly JsonElement EmptyObjectSchema = JsonDocument.Parse("{\"type\":\"object\"}").RootElement;

    private static (RelaySession Session, DuplexChannel Upstream, DuplexChannel Downstream) BuildSession()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();
        return (session, upstreamChannel, downstreamChannel);
    }

    [Fact]
    public async Task ToolsCapability_NeverAdvertisesListChanged()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildSession();
        var fakePython = FakePythonServer.StartWithEchoTool(upstreamChannel);

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        Assert.True(downstreamClient.ServerCapabilities?.Tools?.ListChanged is null or false);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task UpstreamToolsListChangedPush_IsNeverForwardedDownstream_AndSessionStaysHealthy()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildSession();
        var fakePython = FakePythonServer.StartWithEchoTool(upstreamChannel);

        var received = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var downstreamClient = await McpClient.CreateAsync(
            downstreamChannel.CreateClientTransport(),
            new McpClientOptions
            {
                Handlers = new McpClientHandlers
                {
                    NotificationHandlers =
                    [
                        new(NotificationMethods.ToolListChangedNotification, (notification, ct) =>
                        {
                            received.TrySetResult(true);
                            return ValueTask.CompletedTask;
                        }),
                    ],
                },
            });

        // No relay module owns this method in either direction - ToolsRelay only
        // registers tools/list and tools/call requests (see RelayRouteCatalog's
        // duplicate-ownership ledger) - so even a Python that decided to push this
        // notification must never have it reach the downstream client.
        await fakePython.Server.SendNotificationAsync(NotificationMethods.ToolListChangedNotification, CancellationToken.None);

        var winner = await Task.WhenAny(received.Task, Task.Delay(TimeSpan.FromMilliseconds(500)));
        Assert.NotSame(received.Task, winner);

        // The relay/session is unharmed by the unroutable push: a normal exchange
        // still works right after it.
        var tools = await downstreamClient.ListToolsAsync(new ListToolsRequestParams());
        Assert.Contains(tools.Tools, tool => tool.Name == "echo");

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ToolsList_PreservesCursorAndMeta_AndNeverInjectsNextCursor()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildSession();

        ListToolsRequestParams? receivedParams = null;
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) =>
                    {
                        receivedParams = context.Params;
                        return ValueTask.FromResult(new ListToolsResult
                        {
                            Tools = [new Tool { Name = "echo", InputSchema = EmptyObjectSchema }],
                        });
                    },
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var result = await downstreamClient.ListToolsAsync(new ListToolsRequestParams
        {
            Cursor = "fd004-cursor-probe",
            Meta = new JsonObject { ["progressToken"] = "fd004-token" },
        });

        Assert.NotNull(receivedParams);
        Assert.Equal("fd004-cursor-probe", receivedParams!.Cursor);
        Assert.Equal("fd004-token", receivedParams.Meta?["progressToken"]?.GetValue<string>());

        // Python never returns nextCursor (its list_tools() handler ignores cursor and
        // always answers the full catalog); the host must not invent pagination state
        // Python never expressed.
        Assert.Null(result.NextCursor);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ToolsCall_DownstreamCancellation_PropagatesToUpstream_AndSessionStaysHealthyAfterward()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildSession();
        var slowCallStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

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
                        if (context.Params?.Name == "slow")
                        {
                            slowCallStarted.TrySetResult();
                            await Task.Delay(Timeout.Infinite, ct);
                        }

                        return new CallToolResult { Content = [new TextContentBlock { Text = "fast-ok" }] };
                    },
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        using var deadline = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var slowCall = downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "slow" }, cancellationToken: deadline.Token);
        await slowCallStarted.Task;

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => slowCall.AsTask());

        // The relay/session must remain fully usable after a cancelled forward call -
        // FD-000 only proved this for the reverse direction.
        var followUp = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "fast" });
        Assert.False(followUp.IsError == true);
        var text = Assert.IsType<TextContentBlock>(followUp.Content[0]);
        Assert.Equal("fast-ok", text.Text);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task ToolsCall_UpstreamProtocolError_IsNotDoubleWrapped_CodeAndMessageSurviveExactlyOnce()
    {
        // Main/FD005 cross-slice finding, reproduced here against a real (not
        // mocked) upstream McpServer: McpSession.SendRequestAsync wraps a
        // genuine remote JSON-RPC error into a thrown McpProtocolException with
        // a "Request failed (remote): " prefix. RelaySession.ForwardRequestAsync
        // - the raw, direction-agnostic primitive every relay module calls -
        // uses exactly that method for its upstream leg, so left uncorrected
        // this host doubles the prefix: once inside ForwardRequestAsync's own
        // upstream SendRequestAsync call, and once again when the downstream
        // client's own SendRequestAsync converts the propagated exception a
        // second time. The error CODE always already survived exactly regardless
        // (McpProtocolException.ErrorCode round-trips both hops unchanged); only
        // the MESSAGE doubled. This is a different, later-in-the-pipeline defect
        // than the pre-forward malformed-envelope case proven in
        // test_host_malformed_tools_call_envelope_is_rejected_and_session_stays_usable
        // (tests/test_host_proxy.py), where the request never reaches Python at
        // all - here it genuinely does, and Python's own error genuinely comes
        // back.
        var (session, upstreamChannel, downstreamChannel) = BuildSession();

        const string upstreamMessage = "simulated upstream protocol error from fake Python";
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) =>
                        throw new McpProtocolException(upstreamMessage, McpErrorCode.InvalidParams),
                    CallToolHandler = (context, ct) =>
                        throw new McpProtocolException(upstreamMessage, McpErrorCode.InvalidParams),
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var callError = await Assert.ThrowsAsync<McpProtocolException>(
            () => downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything" }).AsTask());
        Assert.Equal(McpErrorCode.InvalidParams, callError.ErrorCode);
        // Exactly one "Request failed (remote): " wrap survives - the downstream
        // leg's own SendRequestAsync-based conversion applies it once,
        // unavoidably, when this exception (already cleaned by ToolsRelay of the
        // upstream leg's own wrap) crosses back to the test's client. A second,
        // ToolsRelay-caused wrap would double it; that doubling is what this fix
        // corrects, not the single remaining wrap itself.
        Assert.Equal($"Request failed (remote): {upstreamMessage}", callError.Message);

        var listError = await Assert.ThrowsAsync<McpProtocolException>(
            () => downstreamClient.ListToolsAsync(new ListToolsRequestParams()).AsTask());
        Assert.Equal(McpErrorCode.InvalidParams, listError.ErrorCode);
        Assert.Equal($"Request failed (remote): {upstreamMessage}", listError.Message);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }
}

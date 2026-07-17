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
/// FD-007: proves every <c>tools/call</c> request preserves <c>_meta.muxSessionId</c> and
/// every sibling <c>_meta</c> field unchanged through the real (unedited)
/// <see cref="RelayComposition.Build"/>/<see cref="ToolsRelay"/>/
/// <see cref="RelaySession.ForwardRequestAsync"/> path - both through the SDK's own typed
/// <c>CallToolRequestParams.Meta</c> accessor and by inspecting the raw
/// <see cref="JsonRpcRequest.Params"/> node Python actually receives. FD-007 does not modify
/// any of this forwarding code; these tests exist because no prior FD-000 test exercised
/// <c>tools/call</c> with a populated <c>_meta</c> object, which is exactly what mux session
/// ownership arbitration depends on.
/// </summary>
public sealed class MuxMetadataForwardingTests
{
    private static JsonObject SampleMeta() => new()
    {
        ["muxSessionId"] = "agent-A",
        ["progressToken"] = "token-123",
        ["someOtherSiblingField"] = new JsonObject { ["nested"] = true, ["count"] = 3 },
    };

    [Fact]
    public async Task ToolCall_PreservesMuxSessionIdAndSiblingMetaFields_TypedAndRaw()
    {
        var upstreamChannel = new DuplexChannel();
        JsonObject? typedMetaSeen = null;
        JsonNode? rawParamsSeen = null;

        await using var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult
                    {
                        Tools = [new Tool { Name = "echo", InputSchema = JsonDocument.Parse("{\"type\":\"object\"}").RootElement }],
                    }),
                    CallToolHandler = (context, ct) =>
                    {
                        typedMetaSeen = context.Params.Meta;
                        rawParamsSeen = context.JsonRpcRequest.Params;
                        return ValueTask.FromResult(new CallToolResult { Content = [new TextContentBlock { Text = "ok" }] });
                    },
                },
            });

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var sentMeta = SampleMeta();
        var result = await downstreamClient.CallToolAsync(new CallToolRequestParams
        {
            Name = "echo",
            Arguments = new Dictionary<string, JsonElement>(),
            Meta = sentMeta,
        });

        Assert.False(result.IsError == true);

        Assert.NotNull(typedMetaSeen);
        Assert.Equal("agent-A", (string?)typedMetaSeen!["muxSessionId"]);
        Assert.Equal("token-123", (string?)typedMetaSeen["progressToken"]);
        Assert.True((bool?)typedMetaSeen["someOtherSiblingField"]!["nested"]);
        Assert.Equal(3, (int?)typedMetaSeen["someOtherSiblingField"]!["count"]);

        Assert.NotNull(rawParamsSeen);
        var rawMeta = Assert.IsType<JsonObject>(rawParamsSeen!["_meta"]);
        Assert.True(JsonNode.DeepEquals(sentMeta, rawMeta));

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ToolCall_CancelledMidFlight_StillCarriesMuxMeta_AndSessionSurvivesForANextCall()
    {
        var upstreamChannel = new DuplexChannel();
        var slowCallStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        JsonObject? slowCallMetaSeen = null;
        var secondCallCount = 0;

        await using var fakePython = FakePythonServer.Start(
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
                        if (context.Params.Name == "slow")
                        {
                            slowCallMetaSeen = context.Params.Meta;
                            slowCallStarted.TrySetResult();
                            await Task.Delay(Timeout.Infinite, ct);
                            return new CallToolResult();
                        }

                        secondCallCount++;
                        return new CallToolResult { Content = [new TextContentBlock { Text = "second-ok" }] };
                    },
                },
            });

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        using var deadline = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var slowCall = downstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "slow", Meta = SampleMeta() },
            cancellationToken: deadline.Token);
        await slowCallStarted.Task;

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => slowCall.AsTask());

        Assert.NotNull(slowCallMetaSeen);
        Assert.Equal("agent-A", (string?)slowCallMetaSeen!["muxSessionId"]);

        // The cancelled, mux-tagged call must not corrupt the shared relay/session: a
        // second, unrelated call on the same downstream connection still round-trips.
        var secondResult = await downstreamClient.CallToolAsync(new CallToolRequestParams { Name = "anything-else" });
        Assert.False(secondResult.IsError == true);
        Assert.Equal(1, secondCallCount);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        await session.DisposeAsync();
    }
}
